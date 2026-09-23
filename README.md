# 🎵 HermesMusicBot

Asistente de música **por chat, sin interfaz gráfica**: le escribes por Telegram, Discord o
HTTP y el audio sale por los **parlantes del servidor** (PC headless, Raspberry Pi, mini-PC).

La clave para que no haya anuncios: **no se usa el reproductor de YouTube**. `yt-dlp` extrae la
URL directa del stream de **solo audio** y `mpv` la reproduce localmente. No hay video, no hay
interfaz, no hay anuncios.

```
usuario: play Bohemian Rhapsody
bot:     ▶️ Reproduciendo: Bohemian Rhapsody (5:57)
usuario: pon música de Queen
bot:     ➕ En cola (#1): Don't Stop Me Now (3:29) — suena después de «Bohemian Rhapsody»
usuario: volumen 50
bot:     🔊 Volumen: 50%
```

---

## 1. Concepto

| | |
|---|---|
| **Nombre** | HermesMusicBot |
| **Qué es** | Daemon de música controlado por chat, con cola, volumen y estado |
| **Canal** | HTTP (`POST /command`) y/o Telegram (`python-telegram-bot`) — el mismo enrutador |
| **Motor de audio** | `mpv --no-video` en modo `--idle`, controlado por **IPC JSON** (socket unix) |
| **Extracción** | `yt-dlp --dump-single-json` → URL de audio directa (sin anuncios, sin video) |
| **Estado** | Cola thread-safe en memoria + hilo de eventos que avanza solo al terminar un tema |
| **Sin GUI** | Todo es CLI/daemon; el audio va a la salida por defecto del sistema |

---

## 2. Arquitectura y flujo

```
   ┌──────────────┐   texto libre    ┌──────────────────────────────────────────┐
   │ Telegram     │─────────────────▶│  handle_command(text, user)              │
   │ (opcional)   │◀─────────────────│  enrutador único de comandos             │
   └──────────────┘   respuesta      │  (play/pause/next/stop/volume/queue/…)   │
   ┌──────────────┐                  └───────────────┬──────────────────────────┘
   │ HTTP API     │─────────────────▶                │
   │ POST /command│◀─────────────────                ▼
   └──────────────┘                        ┌────────────────────────┐
                                           │  MusicPlayer           │
                                           │  cola deque + Lock     │
                                           └───────┬────────┬───────┘
                                                   │        │
                        yt-dlp --dump-single-json  │        │  IPC JSON (socket unix)
                        (búsqueda → URL de audio)  ▼        ▼
                                        ┌─────────────────────────────┐
                                        │  mpv --no-video --idle=yes  │──▶ 🔊 parlantes
                                        │  loadfile <url> replace     │
                                        └──────────┬──────────────────┘
                                                   │ eventos (end-file)
                                                   ▼
                                     al terminar el tema → siguiente de la cola
```

**Secuencia de una canción**

1. El chat manda texto → `handle_command` detecta el verbo (`play`, `pon`, …) y el argumento.
2. `MusicPlayer.resolve()` ejecuta `yt-dlp --dump-single-json --skip-download ytsearch1:<query>`
   y elige la mejor pista solo-audio del JSON (`acodec != none`, mejor `abr`).
3. Si está libre, se carga ya (`loadfile <url> replace`); si hay algo sonando, va a la cola y el
   chat recibe su posición.
4. Al terminar, mpv emite `end-file` por el socket; el hilo de eventos llama a
   `_on_track_finished()`, que saca el siguiente de la cola y lo carga.
5. `pause`/`resume`/`volume` se aplican con `set_property` por IPC: control total sin tocar teclas.

---

## 3. Estructura del proyecto

```
musicbot/
├── app.py                    # FastAPI + enrutador de comandos (handle_command)
├── music_player.py           # MusicPlayer: cola thread-safe + mpv por IPC + resolución yt-dlp
├── config.py                 # toda la configuración por variables de entorno
├── telegram_bot.py           # adaptador opcional de Telegram (asyncio, no bloquea el loop)
├── requirements.txt
├── Dockerfile                # imagen con mpv + ffmpeg + pulseaudio + yt-dlp
├── entrypoint.sh             # elige la salida de audio (host | sink nulo VPS) y arranca la API
├── docker-compose.yml        # PC/notebook CON parlantes (audio del host)
├── docker-compose.vps.yml    # VPS headless (sink nulo) + perfil `stream` con Icecast
├── .env.example
├── .dockerignore
├── tests/smoke_test.py       # prueba de humo de punta a punta contra la API
├── tests/docker_smoke.sh     # la misma prueba, apuntando a un contenedor
└── README.md
```

---

## 4. Instalación nativa (Ubuntu / Debian / Raspberry Pi OS)

### 4.1 Dependencias del sistema

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg mpv curl
```

> `mpv` reproduce, `ffmpeg` lo usa `yt-dlp` para extraer/muxear audio. `yt-dlp` se instala por pip
> (versión reciente) — ver abajo. En Raspberry Pi OS (Debian 12+) estos paquetes están en los
> repos oficiales.

### 4.2 Proyecto y entorno virtual

```bash
git clone <tu-repo> musicbot && cd musicbot      # o copia la carpeta
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env                # ajusta lo que necesites
```

### 4.3 Audio del servidor (lo más importante)

```bash
# ¿Hay salida de audio y está desmuteada?
pactl info | grep -E "Servidor|Destino por defecto"
pactl list short sinks                       # dispositivos de salida
pactl set-sink-mute @DEFAULT_SINK@ 0
pactl set-sink-volume @DEFAULT_SINK@ 70%

# Prueba directa de que suena (sin yt-dlp):
mpv --no-video https://ice1.somafm.com/groovesalad-128-mp3
```

* **PipeWire/PulseAudio** → deja `AUDIO_DEVICE=` vacío.
* **Solo ALSA (Raspberry Pi sin escritorio)** → `AUDIO_DEVICE=alsa/default` (o `alsa/hw:1,0`
  si tienes DAC USB). Verifica con `aplay -l`.
* Si el bot corre como servicio de systemd, necesita `XDG_RUNTIME_DIR=/run/user/<uid>` para
  alcanzar el socket de PulseAudio (ver 4.5).

### 4.4 Ejecutar

```bash
# API HTTP
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8080

# o directamente
.venv/bin/python app.py

# Solo Telegram (sin API HTTP)
.venv/bin/python telegram_bot.py
```

Prueba rápida:

```bash
curl -s localhost:8080/command -H 'Content-Type: application/json' \
     -d '{"text":"play Bohemian Rhapsody"}' | python3 -m json.tool
curl -s localhost:8080/state | python3 -m json.tool
```

### 4.5 Servicio systemd (arranque automático)

`/etc/systemd/system/musicbot.service`:

```ini
[Unit]
Description=HermesMusicBot (música por chat)
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/musicbot
EnvironmentFile=/home/pi/musicbot/.env
Environment=XDG_RUNTIME_DIR=/run/user/1000
ExecStart=/home/pi/musicbot/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now musicbot
journalctl -u musicbot -f          # logs
```

---

## 5. Instalación con Docker (recomendada)

El contenedor trae `mpv`, `ffmpeg`, `pulseaudio` y `yt-dlp`. Hay **dos** archivos compose según
dónde vaya a correr:

| Escenario | Archivo | Salida de audio |
|---|---|---|
| PC / notebook / mini-PC **con parlantes** | `docker-compose.yml` | la del host, por el socket de PulseAudio/PipeWire |
| **VPS headless** (sin tarjeta de sonido) | `docker-compose.vps.yml` | sink nulo propio + (opcional) streaming Icecast |

### 5.1 PC con parlantes → `docker-compose.yml`

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f
curl -s localhost:8080/command -H 'Content-Type: application/json' \
     -d '{"text":"pon música turca"}' | python3 -m json.tool
```

El compose monta el socket del host en `/pulse` y define `PULSE_SERVER=unix:/pulse/native`.
Si tu usuario **no** es el `1000`, no edites el archivo: pásalo por entorno.

```bash
UID=$(id -u) GID=$(id -g) PULSE_PATH=/run/user/$(id -u)/pulse docker compose up -d
```

* Música local: dejá los archivos en `./music` (o `MUSIC_PATH=/ruta`) y dentro del bot se
  reproducen como `play /music/<carpeta>`.
* Verificación real de que el audio sale: `pactl list short sinks` debe decir `RUNNING`.
* Si el contenedor arranca pero **no suena**, casi siempre es el socket de audio del host:
  `systemctl --user status pipewire-pulse` (o `pulseaudio`) y revisa que `/run/user/$(id -u)/pulse`
  exista fuera del contenedor.

### 5.2 VPS headless sin tarjeta de sonido → `docker-compose.vps.yml`

Una VPS no tiene parlantes, y si `mpv` no encuentra salida de audio se cae y la cola se “salta”
todos los temas. Por eso este compose arranca con `AUDIO_MODE=pulse-null`: el contenedor levanta
**su propio PulseAudio con un sink nulo**, así `mpv` reproduce normalmente (la cola avanza, el
estado es real) y opcionalmente eso se retransmite.

```bash
git clone <repo> musicbot && cd musicbot
cp .env.example .env
nano .env        # define API_TOKEN (obligatorio en VPS) y, si quieres, TELEGRAM_TOKEN
docker compose -f docker-compose.vps.yml up -d --build
docker compose -f docker-compose.vps.yml logs -f
```

El archivo **se niega a arrancar sin `API_TOKEN`** a propósito: en una VPS el puerto queda expuesto
a internet. Con token, todas las llamadas llevan la cabecera `X-API-Token`:

```bash
curl -s -H "X-API-Token: TU_TOKEN" -X POST localhost:8080/command \
     -H 'Content-Type: application/json' -d '{"text":"play Queen"}'
```

> En una VPS lo más cómodo es **Telegram** como control remoto (no exponés nada): poné
> `TELEGRAM_TOKEN` y `TELEGRAM_ALLOWED_USERS` en `.env` y listo.

### 5.3 Escuchar la música de la VPS (Icecast, perfil `stream`)

El sink nulo se puede retransmitir para escucharlo desde el laptop:

```bash
# en .env — la clave TIENE que coincidir con ICECAST_SOURCE_PASSWORD
echo 'STREAM_URL=icecast://source:CAMBIAME@icecast:8000/music.mp3' >> .env

docker compose -f docker-compose.vps.yml --profile stream up -d
# en el laptop:  http://IP-DE-LA-VPS:8000/music.mp3   (VLC, mpv, navegador)
```

El relay es continuo: cuando no hay música sonando el stream emite silencio, así que se puede
dejar conectado. Cambiá `ICECAST_SOURCE_PASSWORD`/`STREAM_URL` antes de exponer el 8000, y en
producción poné la API detrás de un proxy con TLS.

### 5.4 Audio por ALSA crudo (Raspberry Pi sin PipeWire)

En `docker-compose.yml`: descomenta el volumen `- /dev/snd:/dev/snd` (y comentá el de pulse), deja
`AUDIO_DEVICE=alsa/default` en `.env` (o `alsa/hw:1,0` con DAC USB) y agrega `devices: ["/dev/snd"]`
(o corre con `--device /dev/snd`) más el grupo `audio`.

### 5.5 Modo solo-Telegram en Docker

```bash
docker compose run --rm musicbot python telegram_bot.py
```

### 5.6 Verificación dentro de Docker

```bash
# prueba de humo completa contra el contenedor (API en el puerto 8080 del host)
API_TOKEN=$(grep -E '^API_TOKEN=' .env | cut -d= -f2-) BASE_URL=http://127.0.0.1:8080 \
  .venv/bin/python tests/smoke_test.py     # o: tests/docker_smoke.sh

docker compose logs --tail=30 musicbot     # log del bot dentro del contenedor
docker compose exec musicbot pactl info    # (modo VPS) el sink nulo responde
```

### 5.7 Fallos típicos en Docker

| Síntoma | Causa | Solución |
|---|---|---|
| `unhealthy` y el bot no responde | uvicorn no arrancó | `docker compose logs musicbot` |
| Arranca pero no suena (modo host) | socket de audio mal montado o uid distinto | `PULSE_PATH=/run/user/$(id -u)/pulse UID=$(id -u) GID=$(id -g)` |
| `Permission denied` al abrir el socket de pulse | el uid del contenedor no es el dueño del socket | correr con `UID=$(id -u) GID=$(id -g)` |
| `mpv` muere / los temas se saltan solos | sin salida de audio (típico en VPS) | usar `docker-compose.vps.yml` (`AUDIO_MODE=pulse-null`) o `AUDIO_MODE=null` |
| El stream de Icecast no suena | clave distinta entre `STREAM_URL` e `ICECAST_SOURCE_PASSWORD` | igualarlas y `docker compose ... up -d` de nuevo |
| `Sign in to confirm you're not a bot` | YouTube pide verificación | montar `cookies.txt` o reconstruir la imagen (yt-dlp nuevo) |

**Actualizar yt-dlp**: `docker compose build --no-cache && docker compose up -d`.

---

## 6. Comandos del bot

| Comando | Alias aceptados | Qué hace |
|---|---|---|
| `play <canción o URL>` | `pon`, `reproduce`, `toca` | Busca en YouTube y reproduce o encola |
| `play <URL de lista>` | — | Carga la **lista completa** de YouTube a la cola (tope: `MAX_QUEUE`). El audio de cada tema se resuelve recién al sonar, así una lista de 90 temas parte al toque |
| `pause` | `pausa` | Pausa lo que suena |
| `resume` | `continúa`, `sigue`, `reanuda` | Reanuda |
| `next` / `skip` | `siguiente`, `salta` | Pasa al siguiente de la cola |
| `stop` | `detente`, `para`, `basta` | Detiene **y limpia la cola** |
| `volume <0-100>` | `volumen 50` | Volumen de mpv (0-100) |
| `queue` | `cola`, `lista` | Muestra la cola |
| `current` | `sonando`, `actual` | Qué se está reproduciendo y el volumen |
| `help` | `ayuda` | Lista de comandos |

Cualquier texto que no sea un comando se interpreta como búsqueda directa
(`Bohemian Rhapsody` ≡ `play Bohemian Rhapsody`).

**API HTTP**: `POST /command {"text": "...", "user": "..."}`, `GET /state`, `GET /queue`,
`POST /play?q=...`, `POST /pause|/resume|/next|/stop`, `POST /volume?value=50`.
Si defines `API_TOKEN`, agrega la cabecera `X-API-Token`.

---

## 7. Configuración (`.env`)

| Variable | Por defecto | Para qué |
|---|---|---|
| `HOST` / `PORT` | `0.0.0.0` / `8080` | API HTTP |
| `API_TOKEN` | vacío | Si se define, exige `X-API-Token` |
| `TELEGRAM_TOKEN` | vacío | Activa el bot de Telegram (@BotFather) |
| `TELEGRAM_ALLOWED_USERS` | vacío | Lista blanca de IDs separados por coma |
| `AUDIO_DEVICE` | vacío | `pulse`, `alsa/default`, `alsa/hw:1,0`, … |
| `DEFAULT_VOLUME` | `70` | Volumen inicial de mpv |
| `MPV_SOCKET` | `/tmp/musicbot-mpv.sock` | Socket IPC |
| `YTDLP_COOKIES` | vacío | `cookies.txt` para sorteos de verificación de bot |
| `YTDLP_PROXY` | vacío | Proxy para redes que bloquean YouTube |
| `SEARCH_PREFIX` | `ytsearch1` | `ytsearch3` = revisa 3 resultados |
| `MAX_QUEUE` | `50` | Tamaño máximo de la cola |
| `LOG_FILE` / `LOG_LEVEL` | `logs/musicbot.log` / `INFO` | Logs rotativos (2 MB × 3) |

---

## 8. Pruebas

```bash
# Con el bot corriendo:
.venv/bin/python tests/smoke_test.py
# Contra otra máquina:
BASE_URL=http://192.168.3.10:8080 API_TOKEN=secreto .venv/bin/python tests/smoke_test.py
```

La prueba de humo verifica: servicio arriba → ayuda → `play` suena → segundo `play` se encola →
`cola` la lista → `pausa`/`continúa` → `volumen 40` → `siguiente` salta y vacía la cola →
`detente` deja todo limpio.

**Prueba manual del audio** (sin yt-dlp, aísla el problema):

```bash
mpv --no-video https://ice1.somafm.com/groovesalad-128-mp3   # ¿suena? el audio está OK
.venv/bin/yt-dlp -f bestaudio -g "ytsearch1:bohemian rhapsody" | head -1   # ¿devuelve URL?
```

---

## 9. Fallos típicos y cómo resolverlos

| Síntoma | Causa | Solución |
|---|---|---|
| Silencio total pero `state` dice “playing” | sink muteado / volumen 0 / contenedor sin audio | `pactl get-sink-mute @DEFAULT_SINK@`, subir volumen, revisar `AUDIO_DEVICE` y el montaje de `/pulse` o `/dev/snd` |
| `mpv` muere al arrancar | `AUDIO_DEVICE` inválido o sin permisos en `/dev/snd` | probar `mpv --no-video <url>` a mano; en Docker `group_add: [audio]` |
| `yt-dlp falló: Sign in to confirm you're not a bot` | YouTube pide verificación | actualizar `yt-dlp`, exportar `cookies.txt` del navegador (`YTDLP_COOKIES`), o `YTDLP_PROXY` |
| “sin resultados” | búsqueda muy específica o error de red | probar `SEARCH_PREFIX=ytsearch3`, o pegar la URL directa |
| Se corta a los pocos segundos | URL de stream caducada o red lenta | subir `--cache-secs` en `MPV_EXTRA_ARGS`, o dejar que mpv use su hook: borrar `audio_url` de la carga |
| `no pude conectar al IPC de mpv` | socket previo en uso o mpv no arrancó | `rm /tmp/musicbot-mpv.sock`, revisar `journalctl`/logs |
| En Telegram no responde | token inválido o usuario no autorizado | revisar logs, `TELEGRAM_ALLOWED_USERS` |
| Latencia alta al pedir canción | `yt-dlp` tarda (2-6 s normal) | aceptar; o usar URLs directas de Free Music Archive/Jamendo |

**Mantenimiento**: `yt-dlp` se rompe cada cierto tiempo porque YouTube cambia. Programa una
actualización semanal: `.venv/bin/pip install -U yt-dlp` (o en Docker, reconstruir la imagen).

---

## 10. ⚠️ Advertencia legal

Este software es **educativo y para uso personal**. Reproducir audio de YouTube sin anuncios
puede **violar los Términos de Servicio de YouTube**; el usuario es el único responsable de
cumplir las leyes y términos aplicables en su país.

**Alternativas legales y libres de anuncios** que puedes enchufar al mismo reproductor:

* **Jamendo** (https://www.jamendo.com) — música de artistas independientes con licencias libres; API pública.
* **Free Music Archive** (https://freemusicarchive.org) — catálogo Creative Commons con descarga directa.
* **Internet Archive / Live Music Archive** (https://archive.org) — conciertos y grabaciones de dominio público.
* **SomaFM / Radio Paradise** — radios por internet sin publicidad que ya funcionan pegando la URL
  del stream (`https://ice1.somafm.com/groovesalad-128-mp3`): `play <esa URL>` funciona igual.

Como el reproductor acepta cualquier URL, cambiar de fuente es cuestión de cambiar la búsqueda,
no el motor.
