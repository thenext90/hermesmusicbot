"""Motor de audio de HermesMusicBot.

Responsabilidades:
  * resolver una búsqueda de YouTube a un stream de AUDIO directo con yt-dlp
    (sin anuncios, porque nunca abrimos el reproductor de YouTube: extraemos la URL
     del stream y la servimos a mpv);
  * reproducirlo en segundo plano con mpv headless (`--no-video`), controlado por
    IPC JSON (socket unix) para pausar/reanudar/volumen/saltar/parar;
  * mantener una cola de reproducción thread-safe y avanzar sola al terminar cada tema.

Diseño: un único proceso mpv en modo `--idle=yes` vive mientras vive el bot; cada
canción se carga con `loadfile ... replace`. Un hilo lector recibe los eventos
(`end-file`) y dispara el avance de la cola.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger("musicbot.player")


class MusicPlayerError(RuntimeError):
    """Error controlado y mostrable por chat (búsqueda sin resultados, mpv caído, etc.)."""


@dataclass
class Track:
    title: str
    webpage_url: str
    audio_url: Optional[str] = None
    duration: Optional[int] = None
    query: str = ""
    requested_by: str = ""

    @property
    def duration_human(self) -> str:
        if not self.duration:
            return "?"
        m, s = divmod(int(self.duration), 60)
        return f"{m}:{s:02d}"

    def as_dict(self) -> dict:
        data = asdict(self)
        data["duration_human"] = self.duration_human
        return data


class MusicPlayer:
    """Cola + mpv headless. Todos los métodos públicos son thread-safe."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.RLock()
        self._queue: deque[Track] = deque()
        self.current: Optional[Track] = None
        self.volume: int = int(cfg.DEFAULT_VOLUME)
        self._paused = False
        self._mpv: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None
        self._sock_lock = threading.Lock()
        self._resp_lock = threading.Lock()
        self._responses: dict[int, "queue.Queue[dict]"] = {}
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._halt = threading.Event()
        self._req_id = 0
        self._started = False
        self.last_error: Optional[str] = None
        # Cuántos `end-file` provocados por un `stop` nuestro hay que ignorar (evita
        # que un stop de "siguiente" se confunda con el fin natural del tema nuevo).
        self._ignorar_eof = 0

    # ── ciclo de vida ───────────────────────────────────────────────────────
    def start(self) -> None:
        """Levanta mpv en modo idle y conecta el IPC."""
        with self._lock:
            if self._started:
                return
            self._spawn_mpv()
            self._sock = self._connect(timeout=10.0)
            self._started = True
            self._halt.clear()
            # Un solo socket y un solo hilo lector: mpv cierra la conexión si el cliente
            # no drena los eventos (por eso no se puede usar un socket "solo comandos").
            for target, name in ((self._reader_loop, "ipc"), (self._dispatch_loop, "dispatch")):
                t = threading.Thread(target=target, name=f"musicbot-{name}", daemon=True)
                t.start()
                self._threads.append(t)
            self._send("set_property", "volume", self.volume)
            log.info("mpv iniciado (socket=%s, volumen=%s)", self.cfg.MPV_SOCKET, self.volume)

    def shutdown(self) -> None:
        """Cierra mpv y los hilos."""
        self._halt.set()
        try:
            self._send("quit", timeout=2.0)
        except Exception:
            pass
        if self._mpv and self._mpv.poll() is None:
            self._mpv.terminate()
            try:
                self._mpv.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._mpv.kill()
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        self._started = False
        log.info("mpv detenido")

    def _spawn_mpv(self) -> None:
        if os.path.exists(self.cfg.MPV_SOCKET):
            try:
                os.unlink(self.cfg.MPV_SOCKET)
            except OSError:
                pass

        args = [
            self.cfg.MPV_BIN,
            "--no-config",                 # no leer ~/.config/mpv (predecible en servidor)
            "--no-video",
            "--audio-display=no",
            "--force-window=no",
            "--term-status-msg=",
            "--really-quiet",
            "--idle=yes",                  # sigue vivo sin canción (clave para controlar)
            "--keep-open=no",
            f"--input-ipc-server={self.cfg.MPV_SOCKET}",
            f"--volume={self.volume}",
            "--cache=yes",
            "--cache-secs=20",
            "--ytdl-format=bestaudio/best",
            f"--script-opts=ytdl_hook-ytdl_path={self.cfg.YTDLP_BIN}",
        ]
        if self.cfg.AUDIO_DEVICE:
            args.append(f"--audio-device={self.cfg.AUDIO_DEVICE}")
        args += list(self.cfg.MPV_EXTRA_ARGS)
        log.debug("mpv: %s", " ".join(args))
        self._mpv = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _connect(self, timeout: float = 10.0) -> socket.socket:
        deadline = time.time() + timeout
        last_err: Optional[Exception] = None
        while time.time() < deadline:
            if self._mpv and self._mpv.poll() is not None:
                raise MusicPlayerError(
                    f"mpv se cerró al arrancar (revisa {self.cfg.MPV_BIN} y el dispositivo de audio)"
                )
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.cfg.MPV_SOCKET)
                return s
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                last_err = exc
                time.sleep(0.2)
        raise MusicPlayerError(f"no pude conectar al IPC de mpv: {last_err}")

    # ── IPC ─────────────────────────────────────────────────────────────────
    def _send(self, *cmd, timeout: float = 8.0) -> dict:
        """Envía un comando al IPC y espera la respuesta de ESE request_id.

        El socket es único y compartido con los eventos: el hilo `_reader_loop` es el
        único que lee, y acá se espera en una cola por request_id.
        """
        with self._sock_lock:
            if self._sock is None:
                raise MusicPlayerError("mpv no está conectado")
            self._req_id += 1
            rid = self._req_id
            cola: "queue.Queue[dict]" = queue.Queue(maxsize=1)
            with self._resp_lock:
                self._responses[rid] = cola
            try:
                payload = json.dumps({"command": list(cmd), "request_id": rid}) + "\n"
                try:
                    self._sock.sendall(payload.encode())
                except OSError as exc:
                    raise MusicPlayerError(f"mpv no responde: {exc}") from exc
                try:
                    msg = cola.get(timeout=timeout)
                except queue.Empty:
                    raise MusicPlayerError("mpv no respondió a tiempo") from None
            finally:
                with self._resp_lock:
                    self._responses.pop(rid, None)
        if msg.get("error") not in (None, "success"):
            raise MusicPlayerError(str(msg.get("error")))
        return msg

    def _reader_loop(self) -> None:
        """Único lector del socket: reparte respuestas y eventos.

        Cada hilo atiende el socket que le tocó: si `_revivir_mpv()` cambia el socket,
        este hilo termina solo y el nuevo se hace cargo.
        """
        sock = self._sock
        buf = b""
        while not self._halt.is_set() and sock is self._sock:
            try:
                data = sock.recv(65536)
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                linea, buf = buf.split(b"\n", 1)
                linea = linea.strip()
                if not linea:
                    continue
                try:
                    msg = json.loads(linea)
                except json.JSONDecodeError:
                    continue
                rid = msg.get("request_id")
                if rid is not None:
                    with self._resp_lock:
                        cola = self._responses.get(rid)
                    if cola is not None:
                        cola.put(msg)
                        continue
                if msg.get("event"):
                    self._events.put(msg)

    def _dispatch_loop(self) -> None:
        while not self._halt.is_set():
            try:
                msg = self._events.get(timeout=0.5)
            except queue.Empty:
                continue
            event = msg.get("event")
            if event == "file_error":
                self.last_error = str(msg.get("file_error") or "error de reproducción")
                log.warning("mpv no pudo reproducir: %s", self.last_error)
            elif event == "end-file":
                log.debug("end-file: %s", msg.get("reason"))
                if msg.get("reason") == "error" and not self.last_error:
                    self.last_error = "mpv no pudo abrir el stream (¿URL caducada?)"
                try:
                    self._on_track_finished()
                except Exception:
                    log.exception("error al avanzar la cola")
            elif event in ("audio-device", "idle"):
                log.debug("evento mpv: %s %s", event, msg)

    # ── búsqueda y resolución ───────────────────────────────────────────────
    def _ytdlp_base(self) -> list[str]:
        args = [self.cfg.YTDLP_BIN, "--no-playlist", "--no-warnings", "--quiet"]
        if self.cfg.YTDLP_COOKIES:
            args += ["--cookies", self.cfg.YTDLP_COOKIES]
        if self.cfg.YTDLP_PROXY:
            args += ["--proxy", self.cfg.YTDLP_PROXY]
        args += list(self.cfg.YTDLP_EXTRA_ARGS)
        return args

    def resolve(self, query: str) -> Track:
        """Busca en YouTube y devuelve un Track con la URL de audio directa."""
        query = (query or "").strip()
        if not query:
            raise MusicPlayerError("no me dijiste qué canción buscar")

        # Un enlace directo a un archivo de audio se reproduce sin pasar por yt-dlp.
        if query.startswith(("http://", "https://")) and self._es_audio_directo(query):
            nombre = os.path.basename(urlparse(query).path) or query
            return Track(title=nombre, webpage_url=query, audio_url=query, query=query)

        target = query
        if not query.startswith(("http://", "https://", "ytsearch")):
            target = f"{self.cfg.SEARCH_PREFIX}:{query}"
        cmd = self._ytdlp_base() + ["--dump-single-json", "--skip-download", target]
        log.info("yt-dlp: buscando %r", query)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired as exc:
            raise MusicPlayerError("yt-dlp se demoró demasiado buscando la canción") from exc
        if proc.returncode != 0:
            err = (proc.stderr or "").strip().splitlines()
            raise MusicPlayerError(f"yt-dlp falló: {err[-1] if err else 'error desconocido'}")

        try:
            info = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise MusicPlayerError("yt-dlp devolvió datos ilegibles") from exc

        if info.get("_type") == "playlist":
            entradas = [e for e in (info.get("entries") or []) if e]
            if not entradas:
                raise MusicPlayerError(f"sin resultados para «{query}»")
            info = entradas[0]
        if not info:
            raise MusicPlayerError(f"sin resultados para «{query}»")

        pagina = info.get("webpage_url") or ""
        if not pagina.startswith("http"):
            # Sin URL real de video no hay nada que reproducir: mejor avisar que mentir.
            raise MusicPlayerError(f"no pude identificar el video para «{query}»")

        audio_url = self._best_audio_url(info)
        return Track(
            title=info.get("title") or query,
            webpage_url=info.get("webpage_url") or info.get("original_url") or query,
            audio_url=audio_url,
            duration=info.get("duration"),
            query=query,
        )

    @staticmethod
    def _es_audio_directo(url: str) -> bool:
        """True si la URL apunta a un archivo de audio reconocible (se salta yt-dlp)."""
        extensiones = (".mp3", ".m4a", ".aac", ".opus", ".ogg", ".oga", ".wav", ".flac", ".webm")
        return urlparse(url).path.lower().endswith(extensiones)

    @staticmethod
    def es_url_de_lista(url: str) -> bool:
        """True si la URL apunta a una lista de reproducción de YouTube."""
        if not url.startswith(("http://", "https://")):
            return False
        return "list=" in url or "/playlist" in url

    def resolve_playlist(self, url: str) -> tuple[list[Track], int]:
        """Lee una lista de YouTube y devuelve sus temas (más el total original).

        Se usa `--flat-playlist` (sin bajar cada video) y el audio de cada tema se
        resuelve recién al sonar, así una lista de 90 temas parte al toque.
        """
        cmd = self._ytdlp_base() + ["--flat-playlist", "--dump-single-json", "--skip-download", url]
        log.info("yt-dlp: leyendo lista %s", url)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        except subprocess.TimeoutExpired as exc:
            raise MusicPlayerError("yt-dlp se demoró demasiado leyendo la lista") from exc
        if proc.returncode != 0:
            err = (proc.stderr or "").strip().splitlines()
            raise MusicPlayerError(f"no pude leer la lista: {err[-1] if err else 'error desconocido'}")
        try:
            info = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise MusicPlayerError("yt-dlp devolvió datos ilegibles para la lista") from exc

        entradas = [e for e in ((info or {}).get("entries") or []) if e]
        if not entradas:
            raise MusicPlayerError("la lista no tiene videos accesibles (¿es privada o un Mix de YouTube?)")

        temas: list[Track] = []
        for e in entradas[: self.cfg.MAX_PLAYLIST]:
            vid = e.get("id") or ""
            pagina = e.get("url") or ""
            if not pagina.startswith("http"):
                pagina = f"https://www.youtube.com/watch?v={vid}" if vid else ""
            if not pagina:
                continue
            temas.append(
                Track(title=e.get("title") or "(sin título)", webpage_url=pagina, duration=e.get("duration"))
            )
        if not temas:
            raise MusicPlayerError("no pude armar la lista (sin videos válidos)")
        return temas, len(entradas)

    def _audio_url_de_video(self, url: str) -> Optional[str]:
        """URL directa de audio de un video puntual (se usa al sonar temas de una lista)."""
        cmd = self._ytdlp_base() + ["-f", "bestaudio/best", "-g", url]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=150)
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode != 0:
            return None
        for linea in (proc.stdout or "").splitlines():
            linea = linea.strip()
            if linea.startswith("http"):
                return linea
        return None

    @staticmethod
    def _best_audio_url(info: dict) -> Optional[str]:
        """Elige la mejor pista solo-audio del JSON de yt-dlp (progressive si no hay DASH)."""
        formatos = [f for f in info.get("formats", []) if f.get("acodec") not in (None, "none")]
        if not formatos:
            return info.get("url")
        # 1º: solo audio (vcodec none), mejor bitrate
        solo_audio = [f for f in formatos if f.get("vcodec") in (None, "none") and f.get("url")]
        if solo_audio:
            mejor = max(solo_audio, key=lambda f: f.get("abr") or f.get("tbr") or 0)
            return mejor.get("url")
        # 2º: cualquier formato con audio (mpv ignora el video igual)
        mejor = max(formatos, key=lambda f: f.get("abr") or 0)
        return mejor.get("url")

    # ── control de reproducción ─────────────────────────────────────────────
    def play_query(self, query: str, requested_by: str = "") -> tuple[str, Track]:
        """Resuelve y reproduce (o encola). Acepta canción, URL, lista de YouTube o carpeta local."""
        # Carpeta o archivo local: no hace falta internet ni yt-dlp.
        ruta = os.path.expanduser(query)
        if os.path.isdir(ruta):
            temas = self._temas_de_carpeta(ruta)
            if not temas:
                raise MusicPlayerError(f"la carpeta «{ruta}» no tiene archivos de audio")
            return self._play_lote(temas, len(temas), requested_by, "Carpeta")
        if os.path.isfile(ruta) and self._es_audio_directo(ruta):
            tema = Track(title=os.path.splitext(os.path.basename(ruta))[0], webpage_url=ruta, audio_url=ruta)
            return self._play_lote([tema], 1, requested_by, "Archivo")

        if self.es_url_de_lista(query):
            return self._play_playlist(query, requested_by)

        track = self.resolve(query)
        track.requested_by = requested_by
        with self._lock:
            if self.current is None:
                self._start_track(track)
                return f"▶️ Reproduciendo: **{track.title}** ({track.duration_human})", track
            if len(self._queue) >= self.cfg.MAX_QUEUE:
                raise MusicPlayerError(f"la cola está llena ({self.cfg.MAX_QUEUE} temas)")
            self._queue.append(track)
            pos = len(self._queue)
            return (
                f"➕ En cola (#{pos}): **{track.title}** ({track.duration_human})"
                f" — suena después de «{self.current.title}»",
                track,
            )

    def _play_playlist(self, url: str, requested_by: str = "") -> tuple[str, Track]:
        """Agrega una lista de YouTube completa a la cola."""
        temas, total = self.resolve_playlist(url)
        return self._play_lote(temas, total, requested_by, "Lista")

    def _temas_de_carpeta(self, carpeta: str, limite: Optional[int] = None) -> list[Track]:
        """Devuelve los archivos de audio de una carpeta local, ordenados."""
        extensiones = (".mp3", ".m4a", ".aac", ".opus", ".ogg", ".oga", ".wav", ".flac")
        archivos = sorted(
            os.path.join(carpeta, f)
            for f in os.listdir(carpeta)
            if f.lower().endswith(extensiones) and os.path.isfile(os.path.join(carpeta, f))
        )
        if limite:
            archivos = archivos[:limite]
        return [
            Track(title=os.path.splitext(os.path.basename(a))[0], webpage_url=a, audio_url=a)
            for a in archivos
        ]

    def _play_lote(
        self, temas: list[Track], total: int, requested_by: str, etiqueta: str
    ) -> tuple[str, Track]:
        """Mete un lote de temas en la cola (y arranca el primero si no hay nada sonando)."""
        if not temas:
            raise MusicPlayerError("no hay temas para agregar")
        with self._lock:
            hueco = max(0, self.cfg.MAX_QUEUE - len(self._queue))
            if hueco == 0:
                raise MusicPlayerError(f"la cola está llena ({self.cfg.MAX_QUEUE} temas)")
            agregados = temas[:hueco]
            for t in agregados:
                t.requested_by = requested_by

            if self.current is None:
                primero, resto = agregados[0], agregados[1:]
                self._queue.extend(resto)
                self._start_track(primero)
                cabeza = f"▶️ Suena: **{primero.title}**"
            else:
                primero = agregados[0]
                self._queue.extend(agregados)
                cabeza = "➕ Agregado a la cola"

            recorte = ""
            if total > len(agregados):
                recorte = f" (venían {total}; entran {len(agregados)} por el tope de cola)"
            return f"🎵 **{etiqueta}**: {len(agregados)} temas{recorte} — {cabeza}", primero

    def _cargar(self, fuente: str) -> None:
        """Carga una fuente en mpv y reafirma el volumen."""
        self._send("loadfile", fuente, "replace")
        self._send("set_property", "volume", self.volume)

    def _revivir_mpv(self) -> None:
        """Relanza mpv tras una caída o socket roto, sin perder la cola."""
        log.warning("mpv no está disponible: relanzando")
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        self._sock = None
        if self._mpv and self._mpv.poll() is None:
            self._mpv.terminate()
            try:
                self._mpv.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._mpv.kill()
        self._spawn_mpv()
        self._sock = self._connect(timeout=10.0)
        nuevo = threading.Thread(target=self._reader_loop, name="musicbot-ipc", daemon=True)
        nuevo.start()
        self._threads.append(nuevo)
        self._send("set_property", "volume", self.volume)
        log.warning("mpv relanzado y reconectado")

    def _start_track(self, track: Track) -> None:
        """Carga el tema en mpv (audio directo; si no hay, deja que mpv use el hook yt-dlp)."""
        # Los temas que vienen de una lista no traen URL de audio: se resuelve recién ahora.
        if (
            not track.audio_url
            and track.webpage_url.startswith("http")
            and not self._es_audio_directo(track.webpage_url)
        ):
            track.audio_url = self._audio_url_de_video(track.webpage_url)
        fuente = track.audio_url or track.webpage_url
        self.current = track
        self._paused = False
        try:
            self._cargar(fuente)
        except MusicPlayerError as exc:
            # mpv caído o socket roto: se relanza una vez y se reintenta
            log.warning("no pude cargar «%s» (%s): relanzo mpv", track.title, exc)
            try:
                self._revivir_mpv()
                self._cargar(fuente)
            except MusicPlayerError:
                if track.audio_url and fuente != track.webpage_url:   # vía hook yt-dlp
                    self._cargar(track.webpage_url)
                else:
                    self.current = None
                    raise
        log.info("suena: %s", track.title)

    def _on_track_finished(self) -> None:
        with self._lock:
            if self._ignorar_eof > 0:        # end-file provocado por nuestro propio stop
                self._ignorar_eof -= 1
                return
            if self.current is None:         # fue un stop/skip explícito: ya se avanzó allá
                return
            self.current = None
            if not self._queue:
                log.info("cola vacía: silencio")
                return
            siguiente = self._queue.popleft()
        self._start_track(siguiente)

    def next_track(self) -> Optional[Track]:
        with self._lock:
            if not self._queue:
                raise MusicPlayerError("la cola está vacía")
            siguiente = self._queue.popleft()
            self._ignorar_eof += 1       # el stop genera un end-file que NO es fin de tema
            self.current = None
            try:
                self._send("stop")
            except MusicPlayerError:
                pass
            self.current = siguiente
        self._start_track(siguiente)
        return siguiente

    def skip(self) -> Optional[Track]:
        """Compatibilidad: saltar = siguiente."""
        return self.next_track()

    def toggle_pause(self) -> bool:
        with self._lock:
            if self.current is None:
                raise MusicPlayerError("no hay nada sonando")
            self._paused = not self._paused
            self._send("set_property", "pause", self._paused)
            return self._paused

    def pause(self) -> None:
        with self._lock:
            if self.current is None:
                raise MusicPlayerError("no hay nada sonando")
            self._paused = True
            self._send("set_property", "pause", True)

    def resume(self) -> None:
        with self._lock:
            if self.current is None:
                raise MusicPlayerError("no hay nada en pausa")
            self._paused = False
            self._send("set_property", "pause", False)

    def stop(self) -> None:
        """Detiene la reproducción y limpia la cola."""
        with self._lock:
            self._queue.clear()
            self._ignorar_eof += 1
            self.current = None
            self._paused = False
            try:
                self._send("stop")
            except MusicPlayerError:
                pass
        log.info("reproducción detenida y cola limpiada")

    def set_volume(self, valor: int) -> int:
        volumen = max(0, min(100, int(valor)))
        with self._lock:
            self.volume = volumen
            if self._started:
                self._send("set_property", "volume", volumen)
        return volumen

    # ── estado ──────────────────────────────────────────────────────────────
    def state(self) -> dict:
        with self._lock:
            return {
                "playing": self.current.as_dict() if self.current else None,
                "paused": self._paused,
                "volume": self.volume,
                "queue_length": len(self._queue),
                "queue": [t.as_dict() for t in self._queue],
                "mpv_alive": bool(self._mpv and self._mpv.poll() is None),
                "last_error": self.last_error,
            }
