# ── HermesMusicBot · imagen con mpv + ffmpeg + yt-dlp ───────────────────────
#
# Sirve para los dos escenarios del compose:
#   · PC / notebook con parlantes  → audio del host vía PulseAudio/PipeWire
#   · VPS headless sin tarjeta     → sink nulo propio dentro del contenedor
#
# Build:  docker compose build      (o: docker build -t hermesmusicbot .)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# mpv reproduce · ffmpeg lo usa yt-dlp y el relay de streaming ·
# pulseaudio/pa-utils dan el sink nulo para VPS sin tarjeta de sonido ·
# curl es el healthcheck. --no-install-recommends mantiene la imagen chica.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      mpv \
      ffmpeg \
      pulseaudio \
      pulseaudio-utils \
      ca-certificates \
      curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Capa de dependencias primero: si no cambian, el rebuild es instantáneo.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Usuario sin privilegios, UID/GID alineados con el host por defecto (1000:1000)
# para que pueda usar el socket de PulseAudio del host sin sudo.
# Ojo: el paquete pulseaudio ya crea el grupo `audio` (GID 29), por eso el grupo
# primario del usuario se crea aparte con el GID pedido.
ARG UID=1000
ARG GID=1000
RUN set -eux; \
    groupadd -f audio; \
    if ! getent group "${GID}" >/dev/null; then groupadd -g "${GID}" musicbot; fi; \
    useradd -m -u "${UID}" -g "${GID}" -G audio appuser; \
    mkdir -p /app/logs /app/music; \
    chown -R "${UID}:${GID}" /app; \
    chmod +x /app/entrypoint.sh

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/ || exit 1

# entrypoint.sh decide la salida de audio (host / sink nulo) y luego arranca la API.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
