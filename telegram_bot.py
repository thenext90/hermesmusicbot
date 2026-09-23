"""Adaptador opcional de Telegram (python-telegram-bot ≥ 21, asyncio).

Solo se activa si TELEGRAM_TOKEN está definido en el entorno. Reutiliza el mismo
enrutador de comandos que la API HTTP (`handle_command`), y saca la reproducción
del event loop con `asyncio.to_thread` para no bloquearlo con subprocess.
"""

from __future__ import annotations

import asyncio
import logging

import config as cfg

log = logging.getLogger("musicbot.telegram")


def _autorizado(user_id: int) -> bool:
    """Lista blanca opcional: si TELEGRAM_ALLOWED_USERS está vacía, permite a todos."""
    return not cfg.TELEGRAM_ALLOWED_USERS or user_id in cfg.TELEGRAM_ALLOWED_USERS


async def iniciar_telegram() -> None:
    """Arranca el polling de Telegram y se queda viva hasta que la cancelen."""
    if not cfg.TELEGRAM_TOKEN:
        log.info("TELEGRAM_TOKEN vacío: adaptador de Telegram desactivado")
        return

    from telegram import Update
    from telegram.constants import ParseMode
    from telegram.error import BadRequest
    from telegram.ext import Application, ContextTypes, MessageHandler, filters

    from app import handle_command

    async def responder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        mensaje = update.effective_message
        if mensaje is None or not mensaje.text:
            return
        usuario = update.effective_user
        if not _autorizado(usuario.id if usuario else 0):
            await mensaje.reply_text("⛔ No estás autorizado a usar este bot.")
            log.warning("bloqueado: %s (%s)", usuario.id if usuario else "?", usuario.full_name if usuario else "?")
            return

        nombre = usuario.full_name if usuario else "telegram"
        try:
            respuesta = await asyncio.to_thread(handle_command, mensaje.text, nombre)
        except Exception as exc:                     # nunca dejar el chat sin respuesta
            log.exception("fallo respondiendo a %s", nombre)
            respuesta = f"💥 Error: {exc}"

        try:                                          # intenta Markdown y cae a texto plano
            await mensaje.reply_text(respuesta, parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            await mensaje.reply_text(respuesta)

    app = Application.builder().token(cfg.TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT, responder))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    log.info("Telegram escuchando (usuarios permitidos: %s)",
             cfg.TELEGRAM_ALLOWED_USERS or "todos")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        log.info("Telegram detenido")


if __name__ == "__main__":          # modo solo-Telegram (sin API HTTP)
    from music_player import MusicPlayer
    import app as api

    api.player.start()
    try:
        asyncio.run(iniciar_telegram())
    finally:
        api.player.shutdown()
