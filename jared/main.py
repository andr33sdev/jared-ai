"""Servidor web de Jared: recibe los mensajes de Telegram y corre el reloj interno."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from datetime import date, timedelta

from . import agent, config, db, extras, gmail, scheduler, telegram, tools, usage, voice
from .tools import now_local, when_label

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("jared")

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


@app.get("/google/callback")
async def google_callback(code: str = "", state: str = "", error: str = ""):
    ok = False
    if code and not error:
        try:
            ok = await gmail.handle_callback(code, state)
        except Exception:
            log.exception("Falló el OAuth de Gmail")
    if ok and config.settings.owner_id:
        with contextlib.suppress(Exception):
            await telegram.send_message(config.settings.owner_id, "📬 Listo, ya puedo leer tu Gmail (solo lectura).")
    msg = "Gmail conectado. Ya podés cerrar esta pestaña. 🫡" if ok else "No se pudo conectar. Pedile a Jared un link nuevo con /gmail."
    return HTMLResponse(f"<html><body style='font-family:sans-serif;padding:2em'><h2>{msg}</h2></body></html>")


@app.post("/telegram")
async def telegram_webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != config.settings.webhook_secret:
        return JSONResponse({"ok": False}, status_code=403)
    update = await request.json()
    asyncio.create_task(_safe_handle(update))
    return {"ok": True}


async def _safe_handle(update: dict) -> None:
    msg = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
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
    if update.get("callback_query"):
        await handle_callback(update["callback_query"])
        return
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
    elif msg.get("document"):
        doc = msg["document"]
        name = doc.get("file_name") or "documento"
        mime = doc.get("mime_type") or ""
        if (doc.get("file_size") or 0) > 20 * 1024 * 1024:
            await telegram.send_message(chat_id, "Ese archivo pesa más de 20 MB y Telegram no me deja bajarlo. 😔")
            return
        is_pdf = mime == "application/pdf" or name.lower().endswith(".pdf")
        is_text = mime.startswith("text/") or name.lower().endswith((".txt", ".csv", ".md"))
        if not (is_pdf or is_text):
            await telegram.send_message(chat_id, "Por ahora entiendo PDFs y archivos de texto. Si podés, exportalo a PDF.")
            return
        async with telegram.typing(chat_id):
            data = await telegram.download_file(doc["file_id"])
            doc_text = extras.extract_pdf_text(data) if is_pdf else data.decode("utf-8", errors="replace")
            doc_id = await extras.store_document(name, doc["file_id"], doc_text)
        caption = msg.get("caption") or ""
        intro = (f"Te mando el documento '{name}' (quedó guardado como documento #{doc_id}). "
                 f"{caption}").strip()
        if is_pdf and len(data) < 15 * 1024 * 1024:
            content = [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.b64encode(data).decode()}},
                {"type": "text", "text": intro},
            ]
        else:
            content = intro + "\n\nTexto del documento:\n" + doc_text[:60000]
        text = f"[documento #{doc_id}: {name}] {caption}".strip()
    elif text:
        content = text
    else:
        await telegram.send_message(chat_id, "Por ahora entiendo texto, audios y fotos.")
        return

    async with telegram.typing(chat_id):
        reply = await agent.run(content, record_as=text if msg.get("document") else None)
    if not reply:
        reply = "Listo."
    await telegram.send_message(chat_id, reply)

    if voice_in or await db.kv_get("voice_replies", False):
        audio = await voice.synthesize(reply)
        if audio:
            with contextlib.suppress(Exception):
                await telegram.send_voice(chat_id, audio)


async def handle_command(chat_id: int, text: str) -> bool:
    """Solo quedan los comandos de sistema. Todo lo demás se habla con Jared.
    Devuelve True si el comando se resolvió acá."""
    cmd = text.partition(" ")[0].split("@")[0].lower()

    if cmd == "/start":
        async with telegram.typing(chat_id):
            reply = await agent.run(
                "(Andrés abrió el chat. Saludalo breve y contale en 2 o 3 líneas, hablando natural, "
                "qué podés hacer por él. Aclarale que no hacen falta comandos: te habla como a una persona.)",
                record_as="(abrió el chat)",
            )
        await telegram.send_message(chat_id, reply)
        return True
    if cmd == "/resumen":
        async with telegram.typing(chat_id):
            await scheduler.morning_brief()
        return True
    if cmd == "/gmail":
        if not gmail.configured():
            await telegram.send_message(
                chat_id, "Para conectar Gmail primero hay que cargar GOOGLE_OAUTH_CLIENT_ID y "
                         "GOOGLE_OAUTH_CLIENT_SECRET en Render (está en el README, paso 8).")
        else:
            link = await gmail.auth_link()
            await telegram.send_message(
                chat_id, f"Abrí este link, elegí tu cuenta y aceptá el permiso de solo lectura (vence en 15 min):\n{link}")
        return True
    return False  # cualquier otra cosa con "/" la interpreta Jared como texto


async def handle_callback(cb: dict) -> None:
    s = config.settings
    if (cb.get("from") or {}).get("id") != s.owner_id:
        await telegram.answer_callback(cb["id"])
        return
    msg = cb.get("message") or {}
    chat_id = msg["chat"]["id"]
    message_id = msg["message_id"]
    original = msg.get("text") or ""
    parts = (cb.get("data") or "").split(":")
    kind, action = parts[0], parts[1] if len(parts) > 1 else ""
    ref = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    if kind == "rem":
        row = await db.fetchrow("select text from reminders where id = %s", ref)
        if action == "done" or not row:
            status = "✅ Hecho"
        else:
            if action == "1h":
                due = now_local() + timedelta(hours=1)
            else:
                hhmm = parts[3] if len(parts) > 3 else "0900"
                due = (now_local() + timedelta(days=1)).replace(
                    hour=int(hhmm[:2]), minute=int(hhmm[2:]), second=0, microsecond=0)
            await db.execute("insert into reminders (text, due_at) values (%s, %s)", row["text"], due)
            status = f"⏳ Pospuesto para {when_label(due)}"
        await telegram.answer_callback(cb["id"], status)
        await telegram.edit_text(chat_id, message_id, f"{original}\n\n{status}")
        return

    if kind == "task":
        result = await tools.completar_tarea(ref)
        await telegram.answer_callback(cb["id"], "✅ Tarea cerrada" if '"ok": true' in result else "Ya estaba cerrada")
        markup = (msg.get("reply_markup") or {}).get("inline_keyboard", [])
        rows = [[(b["text"], b["callback_data"]) for b in row if b.get("callback_data") != cb.get("data")]
                for row in markup]
        rows = [r for r in rows if r]
        await telegram.edit_buttons(chat_id, message_id, rows)
        return

    if kind == "idea":
        estados = {"go": ("en_marcha", "🚀 Vamos con esa"), "no": ("descartada", "🗑 Descartada"),
                   "later": ("pospuesta", "⏳ La vuelvo a traer en un mes")}
        estado, status = estados.get(action, ("pospuesta", "⏳ Más adelante"))
        await tools.marcar_idea(ref, estado)
        await telegram.answer_callback(cb["id"], status)
        await telegram.edit_text(chat_id, message_id, f"{original}\n\n{status}")
        if action == "go":
            row = await db.fetchrow("select content from memories where id = %s", ref)
            async with telegram.typing(chat_id):
                reply = await agent.run(
                    f"(Andrés tocó 'Avanzar' en la idea [{ref}]: {row['content'] if row else ''}. "
                    "Ayudalo a definir el primer paso concreto: hacé UNA pregunta para arrancar.)",
                    record_as="(eligió avanzar con una idea)",
                )
            await telegram.send_message(chat_id, reply)
        return

    if kind == "esp":
        row = await db.fetchrow("select content from memories where id = %s", ref)
        if action == "ok":
            await extras.cerrar_espera(ref)
            status = "✅ Llegó"
            await telegram.answer_callback(cb["id"], status)
            await telegram.edit_text(chat_id, message_id, f"{original}\n\n{status}")
            return
        nxt = now_local().date() + timedelta(days=2)
        await db.execute("update memories set happened_on = %s where id = %s", nxt, ref)
        status = f"❌ Todavía no. Te vuelvo a preguntar el {nxt:%d/%m}."
        await telegram.answer_callback(cb["id"], "Anotado")
        await telegram.edit_text(chat_id, message_id, f"{original}\n\n{status}")
        async with telegram.typing(chat_id):
            reply = await agent.run(
                f"(Andrés dice que todavía no llegó: {row['content'] if row else ''}. Redactale un mensaje corto "
                "y cordial para reclamarlo, listo para copiar y mandar por WhatsApp. Solo el mensaje y una línea de intro.)",
                record_as="(todavía no llegó algo que esperaba)",
            )
        await telegram.send_message(chat_id, reply)
        return

    if kind == "hab":
        habits = await extras.get_habits()
        idx = int(parts[1]) if parts[1].isdigit() else -1
        if not (0 <= idx < len(habits)) or len(parts) < 4:
            await telegram.answer_callback(cb["id"])
            return
        habit, done = habits[idx], parts[2] == "1"
        day = date(int(parts[3][:4]), int(parts[3][4:6]), int(parts[3][6:8]))
        await extras.log_habit(habit, day, done)
        n = await extras.streak(habit, day)
        await telegram.answer_callback(cb["id"], f"🔥 Racha de {n} días" if done else "Mañana será otro día")
        markup = (msg.get("reply_markup") or {}).get("inline_keyboard", [])
        rows = []
        for row in markup:
            datas = [b.get("callback_data", "") for b in row]
            if any(d.startswith(f"hab:{idx}:") for d in datas):
                rows.append([(f"{'✅' if done else '❌'} {habit}" + (f" · 🔥 {n}" if done else ""), "noop")])
            else:
                rows.append([(b["text"], b["callback_data"]) for b in row])
        await telegram.edit_buttons(chat_id, message_id, rows)
        return

    await telegram.answer_callback(cb["id"])
