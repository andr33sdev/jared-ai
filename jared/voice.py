"""Audio: transcripción (Whisper vía Groq) y voz de Jared (Edge TTS)."""
from __future__ import annotations

import logging

import httpx

from . import config
from .textfmt import plain_for_speech

log = logging.getLogger("jared.voice")


async def transcribe(audio: bytes, filename: str = "audio.ogg") -> str:
    key = config.settings.groq_api_key
    if not key:
        raise RuntimeError("Falta GROQ_API_KEY para entender audios")
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            data={"model": "whisper-large-v3-turbo", "language": "es", "response_format": "json"},
            files={"file": (filename, audio, "audio/ogg")},
        )
        r.raise_for_status()
        return r.json().get("text", "").strip()


async def synthesize(text: str) -> bytes | None:
    """Devuelve MP3 con la voz de Jared, o None si falla."""
    clean = plain_for_speech(text)
    if not clean:
        return None
    try:
        import edge_tts

        communicate = edge_tts.Communicate(clean, config.settings.tts_voice, rate="+5%")
        audio = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
        return bytes(audio) or None
    except Exception as exc:
        log.warning("TTS falló: %s", exc)
        return None
