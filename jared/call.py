"""Llamada por voz con Jared desde el navegador."""
from __future__ import annotations

import base64
import logging
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import agent, config, db, telegram, voice

log = logging.getLogger("jared.call")
router = APIRouter()
PAGE = Path(__file__).parent / "static" / "llamada.html"

CALL_MODE = """## Llamada de voz (activa)
{name} te está hablando en una llamada: lo que escribís se convierte en voz.
- Respondé como en una charla telefónica: 1 a 3 frases cortas, naturales. Si el tema da para más, decí lo principal y preguntá si quiere que sigas.
- Nada de markdown, viñetas, emojis, links ni números largos: todo tiene que sonar bien leído en voz alta.
- Igual que siempre, usá tus herramientas (anotar, recordatorios, agenda) cuando corresponda, y confirmalo en pocas palabras.
- Mantené tu personalidad, pero con anécdotas muy breves y poco frecuentes: en voz, la brevedad es respeto."""

END_PROMPT = """[La llamada terminó. Esta es la transcripción:]

{transcript}

Escribí un resumen breve para mandarle por Telegram: de qué hablaron, decisiones, ideas y próximos pasos (viñetas cortas). Antes, asegurate de que todo lo importante quedó guardado en memoria y de crear los recordatorios que haya pedido."""


async def call_token() -> str:
    token = await db.kv_get("call_token")
    if not token:
        token = secrets.token_urlsafe(32)
        await db.kv_set("call_token", token)
    return token


async def call_link() -> str | None:
    base = config.settings.public_url
    if not base:
        return None
    return f"{base.rstrip('/')}/llamada?k={await call_token()}"


async def _check(token: str | None) -> None:
    expected = await db.kv_get("call_token")
    if not token or not expected or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Link de llamada inválido")


async def _reply(text: str, prefer: str) -> dict:
    extra = CALL_MODE.format(name=config.settings.owner_name)
    reply = await agent.run(text, extra_system=extra, smart=False) or "Perdón, me quedé en blanco. ¿Me repetís?"
    audio, engine = await voice.speak(reply, prefer=prefer)
    return {
        "texto": reply,
        "audio": base64.b64encode(audio).decode() if audio else None,
        "voz": engine,
    }


class TurnIn(BaseModel):
    texto: str
    voz: str = "premium"


class StartIn(BaseModel):
    voz: str = "premium"


@router.get("/llamada")
async def page():
    return HTMLResponse(PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@router.post("/api/llamada/inicio")
async def start(body: StartIn, x_call_token: str | None = Header(None)):
    await _check(x_call_token)
    await db.kv_set("call_started", datetime.now(config.settings.tz).isoformat())
    return await _reply(
        "(Andrés te acaba de llamar por voz. Atendé con un saludo cortito, a lo Jared, y preguntale en qué lo ayudás.)",
        body.voz,
    )


@router.post("/api/llamada/turno")
async def turn(body: TurnIn, x_call_token: str | None = Header(None)):
    await _check(x_call_token)
    text = body.texto.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Vacío")
    return await _reply(text, body.voz)


@router.post("/api/llamada/audio")
async def turn_audio(audio: UploadFile = File(...), voz: str = "premium",
                     x_call_token: str | None = Header(None)):
    """Para navegadores sin reconocimiento de voz: se transcribe acá (Groq)."""
    await _check(x_call_token)
    data = await audio.read()
    mime = audio.content_type or "audio/webm"
    ext = {"audio/mp4": "m4a", "audio/aac": "m4a", "audio/ogg": "ogg", "audio/mpeg": "mp3"}.get(mime.split(";")[0], "webm")
    try:
        text = await voice.transcribe(data, f"llamada.{ext}", mime.split(";")[0])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"No pude transcribir: {exc}")
    if not text:
        return {"oido": "", "texto": "", "audio": None, "voz": "ninguna"}
    return {"oido": text, **(await _reply(text, voz))}


@router.post("/api/llamada/fin")
async def end(x_call_token: str | None = Header(None)):
    await _check(x_call_token)
    started = await db.kv_get("call_started")
    await db.kv_set("call_started", None)
    if not started:
        return {"ok": True}
    rows = await db.fetch(
        "select role, content from messages where created_at >= %s order by id",
        datetime.fromisoformat(started),
    )
    if len(rows) <= 2:  # solo el saludo
        return {"ok": True}
    who = {"user": config.settings.owner_name, "assistant": "Jared"}
    transcript = "\n".join(f"{who.get(r['role'], r['role'])}: {r['content']}" for r in rows)[-40000:]
    summary = await agent.run(
        END_PROMPT.format(transcript=transcript), use_history=False, save=False,
    )
    if summary and config.settings.owner_id:
        await db.add_message("assistant", f"📞 Resumen de la llamada\n{summary}")
        await telegram.send_message(config.settings.owner_id, f"📞 **Resumen de la llamada**\n{summary}")
    return {"ok": True}
