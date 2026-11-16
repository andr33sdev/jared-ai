"""Servidor web de Jared: recibe los mensajes de Telegram y corre el reloj interno."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import agent, config, db, scheduler, telegram, voice
from .tools import when_label

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("jared")

HELP = """Soy Jared. Algunas cosas que puedo hacer por vos:

• Hablame normal (texto o audio): voy anotando ideas, gastos, tareas, intereses y personas.
• "Recordame mañana a las 9 llamar al proveedor" → te escribo a esa hora.
• "¿Qué tengo esta semana?" / "Agendá reunión el jueves 15hs".
• "¿Cuánto gasté este mes en nafta?"
• "Buscame novedades sobre impresión 3D".
• Mandame una foto de un ticket y lo anoto como gasto.
• /charla para pensar en voz alta conmigo, /fin para cerrar y guardar conclusiones.
• /voz si → te respondo con audios.

Además, todas las mañanas te mando un resumen y a la tarde reviso si hay algo pendiente."""


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    s = config.settings
    tasks = []
    if s.database_url:
        try:
            await db.init(s.database_url)
        except Exception:
            log.exception("No pude conectarme a la base de datos (revisá DATABASE_URL)")
    else:
        log.error("Falta DATABASE_URL")
    if s.telegram_token and s.public_url:
        try:
            await telegram.set_webhook(f"{s.public_url.rstrip('/')}/telegram", s.webhook_secret)
            await telegram.set_commands()
            log.info("Webhook configurado en %s", s.public_url)
        except Exception:
            log.exception("No pude configurar el webhook")
    if s.database_url and s.telegram_token:
        tasks.append(asyncio.create_task(scheduler.loop()))
    yield
    for t in tasks:
        t.cancel()
    await db.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root():
    return PlainTextResponse("Jared está en línea. 🫡")


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/telegram")
async def telegram_webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != config.settings.webhook_secret:
        return JSONResponse({"ok": False}, status_code=403)
    update = await request.json()
    asyncio.create_task(_safe_handle(update))
    return {"ok": True}


async def _safe_handle(update: dict) -> None:
    msg = update.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    log.info("Update %s recibido del chat %s", update.get("update_id"), chat)
    try:
        if config.settings.owner_id is not None:
            if not await db.mark_update(update.get("update_id", 0)):
                return
        await handle_update(update)
    except Exception as exc:
        log.exception("Error manejando update")
        if chat and config.settings.owner_id in (None, chat):
            with contextlib.suppress(Exception):
                await telegram.send_message(
                    chat,
                    "Perdón, se me trabó algo de mi lado.\n"
                    f"Detalle técnico: {type(exc).__name__}: {str(exc)[:300]}",
                )


async def handle_update(update: dict) -> None:
    msg = update.get("message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    user_id = (msg.get("from") or {}).get("id")
    s = config.settings

    if s.owner_id is None:
        await telegram.send_message(
            chat_id,
            f"Hola, soy Jared. Todavía no sé quién es mi jefe.\nTu ID de Telegram es `{user_id}`: "
            "cargalo en la variable TELEGRAM_OWNER_ID y reiniciame.",
        )
        return
    if user_id != s.owner_id or msg["chat"].get("type") != "private":
        await telegram.send_message(chat_id, "Disculpá, trabajo exclusivamente para una persona. 🙏")
        return

    text = (msg.get("text") or "").strip()
    if text.startswith("/"):
        if await handle_command(chat_id, text):
            return

    voice_in = False
    content: str | list
    if msg.get("voice") or msg.get("audio"):
        media = msg.get("voice") or msg.get("audio")
        async with telegram.typing(chat_id, "typing"):
            try:
                audio = await telegram.download_file(media["file_id"])
                transcript = await voice.transcribe(audio)
            except Exception as exc:
                await telegram.send_message(chat_id, f"No pude escuchar el audio ({exc}).")
                return
        if not transcript:
            await telegram.send_message(chat_id, "El audio me llegó vacío, ¿lo repetís?")
            return
        content = transcript
        voice_in = True
    elif msg.get("photo"):
        photo = msg["photo"][-1]
        data = await telegram.download_file(photo["file_id"])
        caption = msg.get("caption") or "Te mando esta foto."
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(data).decode()}},
            {"type": "text", "text": caption},
        ]
    elif text:
        content = text
    else:
        await telegram.send_message(chat_id, "Por ahora entiendo texto, audios y fotos.")
        return

    async with telegram.typing(chat_id):
        reply = await agent.run(content)
    if not reply:
        reply = "Listo."
    await telegram.send_message(chat_id, reply)

    if voice_in or await db.kv_get("voice_replies", False):
        audio = await voice.synthesize(reply)
        if audio:
            with contextlib.suppress(Exception):
                await telegram.send_voice(chat_id, audio)


async def handle_command(chat_id: int, text: str) -> bool:
    """Devuelve True si el comando se resolvió acá."""
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip().lower()

    if cmd in ("/start", "/ayuda", "/help"):
        await telegram.send_message(chat_id, HELP)
        return True
    if cmd == "/voz":
        on = arg in ("si", "sí", "on", "1", "")
        await db.kv_set("voice_replies", on)
        await telegram.send_message(chat_id, "Perfecto, te respondo con audio. 🎙️" if on else "Vuelvo al texto.")
        return True
    if cmd == "/charla":
        await db.kv_set("mode", "charla")
        async with telegram.typing(chat_id):
            reply = await agent.run(
                "(Andrés abrió una charla con /charla. Saludalo breve y preguntale de qué quiere hablar, "
                "o proponé un tema pendiente si hay alguno claro.)",
                record_as="(abrió una charla)",
            )
        await telegram.send_message(chat_id, reply)
        return True
    if cmd == "/fin":
        async with telegram.typing(chat_id):
            reply = await agent.run(
                "(Andrés cerró la charla con /fin. Hacé un cierre: resumen corto de lo conversado, "
                "conclusiones, ideas y próximos pasos concretos. Asegurate de que todo lo importante "
                "quedó guardado en memoria, y ofrecé recordatorios para los próximos pasos.)",
                record_as="(cerró la charla)",
            )
        await db.kv_set("mode", None)
        await telegram.send_message(chat_id, reply)
        return True
    if cmd == "/resumen":
        async with telegram.typing(chat_id):
            await scheduler.morning_brief()
        return True
    if cmd == "/recordatorios":
        rows = await db.fetch(
            "select id, text, due_at, recurrence from reminders where not done order by due_at limit 30"
        )
        if not rows:
            await telegram.send_message(chat_id, "No tenés recordatorios pendientes. Todo en orden. ✨")
        else:
            lines = [f"• {when_label(r['due_at'])} — {r['text']}"
                     + ("" if r["recurrence"] == "none" else " 🔁") for r in rows]
            await telegram.send_message(chat_id, "**Recordatorios pendientes**\n" + "\n".join(lines))
        return True
    if cmd == "/memoria":
        rows = await db.fetch(
            "select category, content from memories where active order by created_at desc limit 15"
        )
        if not rows:
            await telegram.send_message(chat_id, "Todavía no anoté nada. Contame cosas. 📝")
        else:
            lines = [f"• _{r['category']}_: {r['content']}" for r in rows]
            await telegram.send_message(chat_id, "**Lo último que anoté**\n" + "\n".join(lines))
        return True
    return False  # comando desconocido: que lo procese Jared como texto
