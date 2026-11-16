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

HELP = """Soy Jared. Algunas cosas que puedo hacer por vos:

• Hablame normal (texto o audio): voy anotando ideas, gastos, tareas, intereses y personas.
• "Recordame mañana a las 9 llamar al proveedor" → te escribo a esa hora.
• "¿Qué tengo esta semana?" / "Agendá reunión el jueves 15hs".
• "¿Cuánto gasté este mes en nafta?"
• "Buscame novedades sobre impresión 3D".
• Mandame una foto de un ticket y lo anoto como gasto.
• "Ya llamé al proveedor" → cierro esa tarea. /pendientes para verlas.
• "Poneme un tope de 300 mil por mes en super" → te aviso al 80% y al 100%. /presupuesto
• /charla para pensar en voz alta conmigo, /fin para cerrar y guardar conclusiones.
• /voz si → te respondo con audios. /costo → cuánto voy gastando en Claude.
• /gmail → conecto tu correo (solo lectura).
• /minuta y me contás una reunión → armo la minuta, tus tareas y lo que deben otros. /esperando
• Mandame un PDF → lo resumo, lo guardo y después te respondo preguntas sobre él.
• Cumpleaños y vencimientos → te aviso antes. /fechas
• Hábitos → cada noche te pregunto con botones y llevo tus rachas. /habitos

Lo que hago solo: resumen a la mañana, chequeo a la tarde, ayuda-memoria antes de cada reunión, resumen semanal el domingo y, los miércoles, te rescato una idea que quedó en el aire."""


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

    extra = ""
    if await db.kv_get("minuta_next", False):
        await db.kv_set("minuta_next", False)
        extra = ("IMPORTANTE: este mensaje es el relato de una reunión. Armá la minuta con guardar_minuta "
                 "y mostrásela prolija y corta.")
    async with telegram.typing(chat_id):
        reply = await agent.run(content, extra_system=extra,
                                record_as=text if msg.get("document") else None)
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
    if cmd == "/minuta":
        await db.kv_set("minuta_next", True)
        await telegram.send_message(chat_id, "Te escucho. Contame (mejor en audio) cómo fue la reunión: quiénes estaban, qué se decidió y quién hace qué. 📝")
        return True
    if cmd == "/esperando":
        rows = await extras.open_waits()
        if not rows:
            await telegram.send_message(chat_id, "No estás esperando nada de nadie. ✨")
        else:
            lines = [f"• {r['content']} — {r['happened_on']:%d/%m}" for r in rows]
            await telegram.send_message(chat_id, "**Esperando de otros**\n" + "\n".join(lines))
        return True
    if cmd == "/fechas":
        rows = await extras.upcoming_dates(60)
        if not rows:
            await telegram.send_message(chat_id, "No hay fechas importantes en los próximos 60 días. Pasame cumpleaños y vencimientos y los cuido yo.")
        else:
            lines = [f"• {r['proxima']:%d/%m} — {r['content']}" + (" (¡hoy!)" if r["faltan"] == 0 else f" (en {r['faltan']} días)")
                     for r in rows]
            await telegram.send_message(chat_id, "**Próximas fechas**\n" + "\n".join(lines))
        return True
    if cmd == "/habitos":
        rows = await extras.habit_report()
        if not rows:
            await telegram.send_message(chat_id, "Todavía no definimos hábitos. Decime cuáles querés seguir (ej: agua, caminar, leer).")
        else:
            lines = [f"• {r['habito']}: {r['semana']}  🔥 {r['racha']}" for r in rows]
            await telegram.send_message(chat_id, "**Hábitos (últimos 7 días)**\n" + "\n".join(lines))
        return True
    if cmd == "/pendientes":
        await send_open_tasks(chat_id)
        return True
    if cmd == "/presupuesto":
        rows = await tools.budget_status()
        if not rows:
            await telegram.send_message(chat_id, "Todavía no hay topes. Decime, por ejemplo: \"tope de 300 mil por mes en super\".")
        else:
            lines = []
            for r in rows:
                icon = "🔴" if r["porcentaje"] >= 100 else "🟡" if r["porcentaje"] >= 80 else "🟢"
                lines.append(f"{icon} {r['rubro']}: {r['gastado']:,.0f} / {r['tope']:,.0f} {r['moneda']} ({r['porcentaje']}%)")
            await telegram.send_message(chat_id, f"**Presupuesto {now_local():%m/%Y}**\n" + "\n".join(lines))
        return True
    if cmd == "/costo":
        u = await usage.summary()
        await telegram.send_message(
            chat_id,
            f"**Uso de Claude (estimado)**\nHoy: US$ {u['hoy']:.2f}\nEste mes: US$ {u['mes']:.2f}"
            f"\nProyección del mes: US$ {u['proyeccion']:.2f}\nBúsquedas web del mes: {u['busquedas']}",
        )
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
    return False  # comando desconocido: que lo procese Jared como texto


async def send_open_tasks(chat_id: int) -> None:
    rows = await tools.open_tasks()
    if not rows:
        await telegram.send_message(chat_id, "No tenés tareas abiertas. Impecable. ✨")
        return
    lines = [f"{i}. {r['content']}" for i, r in enumerate(rows, 1)]
    buttons = [[(f"✅ {i}", f"task:done:{r['id']}") for i, r in list(enumerate(rows, 1))[j:j + 5]]
               for j in range(0, len(rows), 5)]
    await telegram.send_message(chat_id, "**Tareas abiertas** (tocá ✅ para cerrar)\n" + "\n".join(lines),
                                buttons=buttons)


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
