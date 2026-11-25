"""Audio: transcripción (Whisper vía Groq) y voz de Jared (ElevenLabs con tope, o Edge TTS gratis)."""
from __future__ import annotations

import logging

import httpx

from . import config, db
from .textfmt import plain_for_speech

log = logging.getLogger("jared.voice")


async def transcribe(audio: bytes, filename: str = "audio.ogg", mime: str = "audio/ogg") -> str:
    """Pasa un audio a texto. Usa Groq (gratis) si hay clave; si no, ElevenLabs Scribe (~USD 0,20/hora)."""
    s = config.settings
    if s.groq_api_key:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {s.groq_api_key}"},
                data={"model": "whisper-large-v3-turbo", "language": "es", "response_format": "json"},
                files={"file": (filename, audio, mime)},
            )
            r.raise_for_status()
            return r.json().get("text", "").strip()
    if s.elevenlabs_api_key:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(
                "https://api.elevenlabs.io/v1/speech-to-text",
                headers={"xi-api-key": s.elevenlabs_api_key},
                data={"model_id": s.elevenlabs_stt_model, "tag_audio_events": "false"},
                files={"file": (filename, audio, mime)},
            )
            r.raise_for_status()
            return (r.json().get("text") or "").strip()
    raise RuntimeError("Para entender audios necesito ELEVENLABS_API_KEY o GROQ_API_KEY en Render")


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


# --- ElevenLabs ---------------------------------------------------------------

ELEVEN_PRICE_PER_1K = 0.04  # USD, modelos Flash/Turbo


def eleven_configured() -> bool:
    s = config.settings
    return bool(s.elevenlabs_api_key and s.elevenlabs_voice_id)


def _month() -> str:
    from .tools import now_local
    return now_local().strftime("%Y-%m")


async def eleven_used() -> int:
    data = await db.kv_get("eleven_chars", {})
    return int(data.get(_month(), 0))


async def _eleven_add(chars: int) -> int:
    month = _month()
    data = await db.kv_get("eleven_chars", {})
    total = int(data.get(month, 0)) + chars
    await db.kv_set("eleven_chars", {month: total})
    return total


async def eleven_synthesize(text: str) -> bytes:
    s = config.settings
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{s.elevenlabs_voice_id}",
            params={"output_format": "mp3_44100_64"},
            headers={"xi-api-key": s.elevenlabs_api_key, "accept": "audio/mpeg"},
            json={"text": text, "model_id": s.elevenlabs_model, "language_code": "es"},
        )
        r.raise_for_status()
        return r.content


async def speak(text: str, prefer: str = "premium") -> tuple[bytes | None, str]:
    """Devuelve (mp3, motor usado). Usa ElevenLabs si se pidió, está configurado y queda cupo."""
    clean = plain_for_speech(text)
    if not clean:
        return None, "ninguna"
    if prefer == "premium" and eleven_configured():
        limit = config.settings.elevenlabs_monthly_chars
        used = await eleven_used()
        if used + len(clean) <= limit:
            try:
                audio = await eleven_synthesize(clean)
                total = await _eleven_add(len(clean))
                if total >= limit * 0.9 and not await db.kv_get(f"eleven_warned_{_month()}", False):
                    await db.kv_set(f"eleven_warned_{_month()}", True)
                    await _warn(f"🎙️ Perdón la molestia: ya usé el 90% del tope mensual de mi voz linda "
                                f"({total:,} de {limit:,} caracteres). Al llegar al 100% sigo con la voz gratis.")
                return audio, "premium"
            except Exception as exc:
                log.warning("ElevenLabs falló, uso la voz gratis: %s", exc)
    audio = await synthesize(clean)
    return audio, "gratis"


async def _warn(text: str) -> None:
    from . import telegram
    if config.settings.owner_id:
        try:
            await telegram.send_message(config.settings.owner_id, text)
        except Exception:
            log.exception("No pude avisar el tope de voz")
