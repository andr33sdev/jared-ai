"""Llamada por voz con Jared desde el navegador."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
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


FILLERS = ["Mmm, a ver.", "A ver...", "Ajá...", "Bueno...", "Mmm..."]
_filler_cache: dict[str, list[str]] = {}


async def fillers(prefer: str) -> list[str]:
    """Muletillas cortas en audio (base64), generadas una vez por voz."""
    if prefer not in _filler_cache:
        out = []
        for f in FILLERS:
            audio, _ = await voice.speak(f, prefer=prefer)
            if audio:
                out.append(base64.b64encode(audio).decode())
        _filler_cache[prefer] = out
    return _filler_cache[prefer]


class SentenceSplitter:
    """Junta el texto que va llegando y devuelve oraciones completas para decir en voz.

    La primera oración sale apenas termina (aunque sea corta) para que Jared empiece
    a hablar cuanto antes; las siguientes se agrupan si son muy cortas."""

    BOUND = re.compile(r"[.!?…]+(?=\s)|\n")

    def __init__(self, min_len: int = 25):
        self.buf = ""
        self.min_len = min_len
        self.started = False

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        out = []
        while True:
            cut = None
            for m in self.BOUND.finditer(self.buf):
                piece = self.buf[: m.end()].strip()
                if piece and (len(piece) >= self.min_len or not self.started):
                    cut = m.end()
                    break
            if cut is None:
                break
            out.append(self.buf[:cut].strip())
            self.buf = self.buf[cut:]
            self.started = True
        return out

    def flush(self) -> list[str]:
        rest, self.buf = self.buf.strip(), ""
        return [rest] if rest else []


async def _stream_reply(text: str, prefer: str):
    """Genera NDJSON: un evento por oración (texto + audio) apenas está listo."""
    extra = CALL_MODE.format(name=config.settings.owner_name)
    queue: asyncio.Queue = asyncio.Queue()
    splitter = SentenceSplitter()

    async def tts(sentence: str):
        audio, engine = await voice.speak(sentence, prefer=prefer)
        return sentence, audio, engine

    async def on_text(delta: str):
        for sentence in splitter.feed(delta):
            queue.put_nowait(asyncio.create_task(tts(sentence)))

    async def runner():
        try:
            reply = await agent.run(text, extra_system=extra, smart=False, light=True, on_text=on_text)
            for sentence in splitter.flush():
                queue.put_nowait(asyncio.create_task(tts(sentence)))
            queue.put_nowait(("done", reply))
        except Exception as exc:
            log.exception("Falló el turno de la llamada")
            queue.put_nowait(("error", str(exc)))

    asyncio.create_task(runner())
    while True:
        item = await queue.get()
        if isinstance(item, tuple):
            kind, payload = item
            yield json.dumps({"type": kind, "texto": payload}, ensure_ascii=False) + "\n"
            return
        sentence, audio, engine = await item
        yield json.dumps({
            "type": "chunk", "texto": sentence, "voz": engine,
            "audio": base64.b64encode(audio).decode() if audio else None,
        }, ensure_ascii=False) + "\n"


async def _reply(text: str, prefer: str, light: bool = True) -> dict:
    extra = CALL_MODE.format(name=config.settings.owner_name)
    reply = await agent.run(text, extra_system=extra, smart=False, light=light) or "Perdón, me quedé en blanco. ¿Me repetís?"
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
    greeting, muletillas = await asyncio.gather(
        _reply("(Andrés te acaba de llamar por voz. Atendé con un saludo cortito, a lo Jared, "
               "y preguntale en qué lo ayudás.)", body.voz, light=True),
        fillers(body.voz),
    )
    return {**greeting, "muletillas": muletillas, "pausa_ms": config.settings.call_pause_ms}


@router.post("/api/llamada/turno")
async def turn(body: TurnIn, x_call_token: str | None = Header(None)):
    await _check(x_call_token)
    text = body.texto.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Vacío")
    return StreamingResponse(_stream_reply(text, body.voz), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


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
