"""Configuración central de HermesMusicBot.

Todo se controla por variables de entorno (ver .env.example).
No hay nada hardcodeado: tokens, rutas de binarios, dispositivo de audio y límites
se leen acá una sola vez y el resto del código importa este módulo.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # dotenv es opcional: en Docker usamos env_file
    pass

BASE_DIR = Path(__file__).resolve().parent


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on", "si", "sí"}


# ── HTTP API ────────────────────────────────────────────────────────────────
HOST: str = _env("HOST", "0.0.0.0")
PORT: int = _env_int("PORT", 8080)
API_TOKEN: str = _env("API_TOKEN")          # vacío = sin autenticación (solo LAN)

# ── Telegram (opcional) ─────────────────────────────────────────────────────
TELEGRAM_TOKEN: str = _env("TELEGRAM_TOKEN")
TELEGRAM_ALLOWED_USERS: list[int] = [
    int(x) for x in _env("TELEGRAM_ALLOWED_USERS").replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()
]

# ── Reproducción ────────────────────────────────────────────────────────────
MPV_BIN: str = _env("MPV_BIN", "mpv")
YTDLP_BIN: str = _env("YTDLP_BIN", "yt-dlp")
FFMPEG_BIN: str = _env("FFMPEG_BIN", "ffmpeg")
# Vacío = dispositivo por defecto del sistema (PulseAudio/PipeWire/ALSA).
# En el servidor de jp: "pulse/alsa_output.pci-0000_00_1f.3.analog-stereo"
AUDIO_DEVICE: str = _env("AUDIO_DEVICE")
MPV_SOCKET: str = _env("MPV_SOCKET", "/tmp/musicbot-mpv.sock")
MPV_EXTRA_ARGS: list[str] = shlex.split(_env("MPV_EXTRA_ARGS"))
DEFAULT_VOLUME: int = max(0, min(100, _env_int("DEFAULT_VOLUME", 70)))
MAX_QUEUE: int = _env_int("MAX_QUEUE", 50)
MAX_PLAYLIST: int = _env_int("MAX_PLAYLIST", 50)   # tope de temas al pegar una lista larga
MAX_STREAM_HEIGHT: str = _env("YTDLP_FORMAT", "bestaudio/best")

# yt-dlp: cookies y proxy ayudan cuando YouTube pide verificación de bot.
YTDLP_COOKIES: str = _env("YTDLP_COOKIES")            # ruta a cookies.txt
YTDLP_PROXY: str = _env("YTDLP_PROXY")                # http(s)://host:puerto
YTDLP_EXTRA_ARGS: list[str] = shlex.split(_env("YTDLP_EXTRA_ARGS"))
SEARCH_PREFIX: str = _env("SEARCH_PREFIX", "ytsearch1")  # ytsearchN = trae N resultados

# ── Datos y logs ────────────────────────────────────────────────────────────
LOG_LEVEL: str = _env("LOG_LEVEL", "INFO").upper()
LOG_FILE: str = _env("LOG_FILE", str(BASE_DIR / "logs" / "musicbot.log"))
LOG_MAX_BYTES: int = _env_int("LOG_MAX_BYTES", 2_000_000)
LOG_BACKUPS: int = _env_int("LOG_BACKUPS", 3)

__all__ = [
    "HOST", "PORT", "API_TOKEN", "TELEGRAM_TOKEN", "TELEGRAM_ALLOWED_USERS",
    "MPV_BIN", "YTDLP_BIN", "FFMPEG_BIN", "AUDIO_DEVICE", "MPV_SOCKET",
    "MPV_EXTRA_ARGS", "DEFAULT_VOLUME", "MAX_QUEUE", "MAX_STREAM_HEIGHT",
    "YTDLP_COOKIES", "YTDLP_PROXY", "YTDLP_EXTRA_ARGS", "SEARCH_PREFIX",
    "LOG_LEVEL", "LOG_FILE", "LOG_MAX_BYTES", "LOG_BACKUPS", "BASE_DIR",
]
