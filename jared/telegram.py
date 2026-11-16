"""Cliente mínimo de la Bot API de Telegram."""
from __future__ import annotations

import asyncio
import contextlib
import logging

import httpx

from . import config
from .textfmt import md_to_tg_html, split_text

log = logging.getLogger("jared.telegram")
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=60)
    return _client


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{config.settings.telegram_token}/{method}"


async def call(method: str, **params) -> dict:
    r = await _http().post(_api(method), json=params)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")
    return data["result"]


def keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """[[('✅ Hecho', 'rem:done:3'), ...], ...] -> inline_keyboard de Telegram."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


async def send_message(chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
    chunks = split_text(text)
    for i, chunk in enumerate(chunks):
        extra = {}
        if buttons and i == len(chunks) - 1:
            extra["reply_markup"] = keyboard(buttons)
        try:
            await call(
                "sendMessage",
                chat_id=chat_id,
                text=md_to_tg_html(chunk),
                parse_mode="HTML",
                disable_web_page_preview=True,
                **extra,
            )
        except Exception as exc:  # HTML inválido: mandamos texto plano
            log.warning("Fallo HTML, envío plano: %s", exc)
            await call("sendMessage", chat_id=chat_id, text=chunk, disable_web_page_preview=True, **extra)


async def answer_callback(callback_id: str, text: str = "") -> None:
    await call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])


async def edit_buttons(chat_id: int, message_id: int, buttons: list[list[tuple[str, str]]] | None) -> None:
    await call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
               reply_markup=keyboard(buttons or []))


async def edit_text(chat_id: int, message_id: int, text: str) -> None:
    await call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
               disable_web_page_preview=True)


async def send_voice(chat_id: int, audio: bytes, filename: str = "jared.mp3") -> None:
    files = {"voice": (filename, audio, "audio/mpeg")}
    r = await _http().post(_api("sendVoice"), data={"chat_id": str(chat_id)}, files=files)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendVoice: {data.get('description')}")


async def download_file(file_id: str) -> bytes:
    info = await call("getFile", file_id=file_id)
    url = f"https://api.telegram.org/file/bot{config.settings.telegram_token}/{info['file_path']}"
    r = await _http().get(url)
    r.raise_for_status()
    return r.content


async def set_webhook(url: str, secret: str) -> None:
    await call(
        "setWebhook",
        url=url,
        secret_token=secret,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=False,
    )


async def set_commands() -> None:
    await call(
        "setMyCommands",
        commands=[
            {"command": "resumen", "description": "Resumen del día ahora"},
            {"command": "gmail", "description": "Conectar tu Gmail (solo lectura)"},
        ],
    )


@contextlib.asynccontextmanager
async def typing(chat_id: int, action: str = "typing"):
    """Muestra 'escribiendo…' mientras dure el bloque."""

    async def loop():
        while True:
            with contextlib.suppress(Exception):
                await call("sendChatAction", chat_id=chat_id, action=action)
            await asyncio.sleep(4.5)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
