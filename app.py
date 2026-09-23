"""HermesMusicBot — API HTTP (FastAPI) + enrutador de comandos de chat.

Un único enrutador (`handle_command`) atiende tanto HTTP como Telegram: así el bot
se comporta igual por cualquier canal y no hay lógica duplicada.

Levantar:  uvicorn app:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

import config as cfg
from music_player import MusicPlayer, MusicPlayerError

# ── logs: consola + archivo rotativo ────────────────────────────────────────
def setup_logging() -> None:
    os.makedirs(os.path.dirname(cfg.LOG_FILE) or ".", exist_ok=True)
    raiz = logging.getLogger()
    if raiz.handlers:                      # ya configurado (recarga de uvicorn)
        return
    raiz.setLevel(getattr(logging, cfg.LOG_LEVEL, logging.INFO))
    formato = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    consola = logging.StreamHandler()
    consola.setFormatter(formato)
    raiz.addHandler(consola)

    archivo = logging.handlers.RotatingFileHandler(
        cfg.LOG_FILE, maxBytes=cfg.LOG_MAX_BYTES, backupCount=cfg.LOG_BACKUPS, encoding="utf-8"
    )
    archivo.setFormatter(formato)
    raiz.addHandler(archivo)


setup_logging()
log = logging.getLogger("musicbot.app")

player = MusicPlayer(cfg)

HELP = """🎵 **HermesMusicBot** — comandos disponibles

• `play <canción o URL>` (o `pon`, `reproduce`) → busca en YouTube y reproduce/encola
• `pause` / `resume` (o `pausa`, `continúa`) → pausa o reanuda
• `next` / `skip` (o `siguiente`, `salta`) → pasa al siguiente de la cola
• `stop` (o `detente`, `para`) → detiene y limpia la cola
• `volume <0-100>` (o `volumen 50`) → ajusta el volumen
• `queue` (o `cola`) → muestra la cola
• `current` (o `sonando`) → qué está sonando
• `help` (o `ayuda`) → esta ayuda

Ejemplos: `play Bohemian Rhapsody`, `pon música de Queen`, `volumen 50`, `siguiente`"""

# Palabras clave aceptadas por comando (español e inglés).
PLAY_WORDS = {"play", "p", "pon", "poner", "reproduce", "reproducir", "toca", "suena", "musica", "música"}
PAUSE_WORDS = {"pause", "pausa", "pausar", "deten", "parar_temporal"}
RESUME_WORDS = {"resume", "continuar", "continua", "continúa", "reanuda", "reanudar", "sigue"}
NEXT_WORDS = {"next", "skip", "siguiente", "salta", "salta_tema", "cambia"}
STOP_WORDS = {"stop", "detente", "detener", "parar", "para", "basta", "silencio"}
QUEUE_WORDS = {"queue", "cola", "lista"}
CURRENT_WORDS = {"current", "sonando", "actual", "now", "nsonando"}
VOLUME_WORDS = {"volume", "volumen", "vol"}
HELP_WORDS = {"help", "ayuda", "comandos", "?", "h"}


def handle_command(text: str, user: str = "") -> str:
    """Traduce un mensaje de chat a una acción del reproductor y devuelve la respuesta."""
    texto = (text or "").strip()
    if not texto:
        return HELP

    partes = texto.split(maxsplit=1)
    verbo = partes[0].lower().lstrip("/").strip("¡!¿?:,.")
    arg = partes[1].strip() if len(partes) > 1 else ""

    try:
        if verbo in HELP_WORDS:
            return HELP

        if verbo in PLAY_WORDS:
            # "pon música de Queen" → la palabra música es ruido, el resto es la búsqueda
            if verbo in {"musica", "música"} and arg:
                pass
            if not arg and verbo in {"musica", "música"}:
                raise MusicPlayerError("dime qué quieres escuchar: `play <canción o URL>`")
            if not arg:
                raise MusicPlayerError("dime qué canción: `play <nombre o URL>`")
            mensaje, _ = player.play_query(arg, requested_by=user)
            return mensaje

        if verbo in PAUSE_WORDS:
            player.pause()
            return "⏸️ En pausa"

        if verbo in RESUME_WORDS:
            player.resume()
            return "▶️ Reanudado"

        if verbo in NEXT_WORDS:
            pista = player.next_track()
            return f"⏭️ Ahora: **{pista.title}** ({pista.duration_human})"

        if verbo in STOP_WORDS:
            player.stop()
            return "⏹️ Detenido y cola vaciada"

        if verbo in QUEUE_WORDS:
            estado = player.state()
            if not estado["queue"]:
                return "La cola está vacía." + (
                    f" Sonando: **{estado['playing']['title']}**" if estado["playing"] else ""
                )
            lineas = [f"{i}. {t['title']} ({t['duration_human']})" for i, t in enumerate(estado["queue"], 1)]
            return "📋 **Cola** (" + str(len(lineas)) + "):\n" + "\n".join(lineas)

        if verbo in CURRENT_WORDS:
            estado = player.state()
            if not estado["playing"]:
                return "No hay nada sonando."
            pausa = " (en pausa)" if estado["paused"] else ""
            return f"🎧 **{estado['playing']['title']}**{pausa} — volumen {estado['volume']}%"

        if verbo in VOLUME_WORDS:
            if not arg:
                return f"🔊 Volumen actual: {player.volume}%"
            numero = "".join(ch for ch in arg if ch.isdigit())
            if not numero:
                raise MusicPlayerError("usa `volume <0-100>`, por ejemplo `volumen 50`")
            nuevo = player.set_volume(int(numero))
            return f"🔊 Volumen: {nuevo}%"

        # Si no es un verbo conocido, se asume búsqueda directa ("Bohemian Rhapsody")
        mensaje, _ = player.play_query(texto, requested_by=user)
        return mensaje

    except MusicPlayerError as exc:
        return f"⚠️ {exc}"
    except Exception as exc:  # nunca reventar el chat por un error inesperado
        log.exception("error procesando %r", texto)
        return f"💥 Error inesperado: {exc}"


# ── FastAPI ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI):
    player.start()
    tarea_telegram = None
    if cfg.TELEGRAM_TOKEN:
        try:
            from telegram_bot import iniciar_telegram
            tarea_telegram = asyncio.create_task(iniciar_telegram())
            log.info("bot de Telegram activado")
        except Exception:
            log.exception("no pude arrancar Telegram (sigo solo con la API HTTP)")
    log.info("HermesMusicBot listo en http://%s:%s", cfg.HOST, cfg.PORT)
    try:
        yield
    finally:
        if tarea_telegram:
            tarea_telegram.cancel()
        player.shutdown()


app = FastAPI(title="HermesMusicBot", version="1.0.0", lifespan=lifespan)


class CommandIn(BaseModel):
    text: str
    user: str | None = None


def require_token(x_api_token: str | None = Header(default=None, alias="X-API-Token")) -> None:
    """Si API_TOKEN está definido, se exige en la cabecera X-API-Token."""
    if cfg.API_TOKEN and x_api_token != cfg.API_TOKEN:
        raise HTTPException(status_code=401, detail="token inválido o faltante")


@app.get("/")
def raiz():
    return {"servicio": "HermesMusicBot", "estado": "ok", "comandos": sorted(PLAY_WORDS | STOP_WORDS)}


@app.post("/command", dependencies=[Depends(require_token)])
def command(body: CommandIn):
    """Punto de entrada genérico: el chat manda texto libre y recibe la respuesta."""
    respuesta = handle_command(body.text, user=body.user or "http")
    return {"ok": True, "reply": respuesta, "state": player.state()}


@app.get("/state", dependencies=[Depends(require_token)])
def state():
    return player.state()


@app.get("/queue", dependencies=[Depends(require_token)])
def queue():
    estado = player.state()
    return {"playing": estado["playing"], "queue": estado["queue"]}


@app.post("/play", dependencies=[Depends(require_token)])
def play(q: str, user: str = "http"):
    mensaje, pista = player.play_query(q, requested_by=user)
    return {"ok": True, "reply": mensaje, "track": pista.as_dict()}


@app.post("/pause", dependencies=[Depends(require_token)])
def pause():
    player.pause()
    return {"ok": True, "reply": "⏸️ En pausa"}


@app.post("/resume", dependencies=[Depends(require_token)])
def resume():
    player.resume()
    return {"ok": True, "reply": "▶️ Reanudado"}


@app.post("/next", dependencies=[Depends(require_token)])
def next_track():
    pista = player.next_track()
    return {"ok": True, "reply": f"⏭️ Ahora: {pista.title}", "track": pista.as_dict()}


@app.post("/stop", dependencies=[Depends(require_token)])
def stop():
    player.stop()
    return {"ok": True, "reply": "⏹️ Detenido y cola vaciada"}


@app.post("/volume", dependencies=[Depends(require_token)])
def volume(value: int):
    return {"ok": True, "volume": player.set_volume(value)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=cfg.HOST, port=cfg.PORT)
