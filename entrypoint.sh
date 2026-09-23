#!/usr/bin/env bash
# ── HermesMusicBot · entrypoint ─────────────────────────────────────────────
# Prepara la salida de audio y después entrega el control a la API (uvicorn).
#
#   AUDIO_MODE=host        (default) usa el audio del host vía PULSE_SERVER
#   AUDIO_MODE=pulse-null  levanta un PulseAudio propio con sink nulo (VPS sin tarjeta)
#   AUDIO_MODE=null        mpv sin salida (--ao=null): la cola avanza, pero no suena
#
# Opcional: STREAM_URL=icecast://source:clave@host:8000/music.mp3
# retransmite el sink nulo a un Icecast para escuchar la música desde el laptop.
set -euo pipefail

AUDIO_MODE="${AUDIO_MODE:-host}"
SINK_NAME="${SINK_NAME:-musicbot}"

log() { printf '[entrypoint] %s\n' "$*" >&2; }

# El log va a un volumen; si por lo que sea su carpeta no es escribible (por ejemplo
# un bind mount creado por docker como root), el bot no debe morir por eso: se cae
# a /tmp y los logs siguen saliendo por stdout (docker compose logs).
asegurar_dir_log() {
    local archivo="${LOG_FILE:-/app/logs/musicbot.log}"
    local dir
    dir="$(dirname "$archivo")"
    if mkdir -p "$dir" 2>/dev/null && [ -w "$dir" ]; then
        return 0
    fi
    log "aviso: '$dir' no es escribible por uid $(id -u); los logs van a /tmp y a stdout"
    export LOG_FILE=/tmp/musicbot.log
}

asegurar_dir_log

# ── PulseAudio propio + sink nulo (VPS headless) ────────────────────────────
arrancar_sink_nulo() {
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/pulse-runtime}"
    mkdir -p "$XDG_RUNTIME_DIR"
    chmod 700 "$XDG_RUNTIME_DIR"

    if ! pulseaudio --check 2>/dev/null; then
        log "levantando PulseAudio interno (sink nulo: ${SINK_NAME})"
        pulseaudio --start \
            --exit-idle-time=-1 \
            --disallow-exit \
            --log-target=stderr \
            --load="module-null-sink sink_name=${SINK_NAME} sink_properties=device.description=MusicBot" \
            || log "aviso: pulseaudio --start devolvió error (sigo igual)"
    fi

    # esperar a que el servidor responda (hasta ~10 s)
    for _ in $(seq 1 20); do
        pactl info >/dev/null 2>&1 && break
        sleep 0.5
    done

    if pactl info >/dev/null 2>&1; then
        pactl set-default-sink "$SINK_NAME" 2>/dev/null || true
        pactl set-sink-volume "$SINK_NAME" 100% 2>/dev/null || true
        # mpv ya arranca al 70% por su cuenta; el sink se deja a tope.
        : "${AUDIO_DEVICE:=pulse/${SINK_NAME}}"
        export AUDIO_DEVICE
        log "audio listo → AUDIO_DEVICE=${AUDIO_DEVICE}"
    else
        log "ERROR: PulseAudio interno no responde; caigo a --ao=null"
        export MPV_EXTRA_ARGS="${MPV_EXTRA_ARGS:---ao=null}"
    fi
}

# ── Relay opcional al Icecast (escuchar la música desde el laptop) ──────────
arrancar_relay() {
    local url="$1" bitrate="${STREAM_BITRATE:-128}"
    log "relay activo → ${url%%:*}:…@${url##*@}"
    (
        while true; do
            parec --device="${SINK_NAME}.monitor" --format=s16le --rate=44100 --channels=2 2>/dev/null \
              | ffmpeg -hide_banner -loglevel warning \
                    -f s16le -ar 44100 -ac 2 -i - \
                    -c:a libmp3lame -b:a "${bitrate}k" \
                    -f mp3 -content_type audio/mpeg \
                    "$url" \
              || log "relay cortado, reintento en 5 s"
            sleep 5
        done
    ) &
}

case "$AUDIO_MODE" in
    host)
        log "modo host: uso el audio del host (PULSE_SERVER=${PULSE_SERVER:-<default>})"
        ;;
    pulse-null)
        arrancar_sink_nulo
        [[ -n "${STREAM_URL:-}" ]] && arrancar_relay "$STREAM_URL"
        ;;
    null)
        log "modo null: mpv sin salida de audio (--ao=null)"
        export MPV_EXTRA_ARGS="${MPV_EXTRA_ARGS:---ao=null}"
        ;;
    *)
        log "ERROR: AUDIO_MODE='${AUDIO_MODE}' no existe (host | pulse-null | null)"
        exit 2
        ;;
esac

log "arrancando: $*"
exec "$@"
