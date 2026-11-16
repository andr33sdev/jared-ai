"""Gmail en modo solo lectura (OAuth con tu cuenta personal)."""
from __future__ import annotations

import base64
import html
import re
import secrets
import time
from urllib.parse import urlencode

import httpx

from . import config, db

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
_token: dict = {"value": None, "exp": 0.0}


def configured() -> bool:
    s = config.settings
    return bool(s.google_oauth_client_id and s.google_oauth_client_secret and s.public_url)


def redirect_uri() -> str:
    return f"{config.settings.public_url.rstrip('/')}/google/callback"


async def auth_link() -> str:
    state = secrets.token_urlsafe(24)
    await db.kv_set("oauth_state", {"state": state, "exp": time.time() + 900})
    params = {
        "client_id": config.settings.google_oauth_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


async def handle_callback(code: str, state: str) -> bool:
    saved = await db.kv_get("oauth_state") or {}
    if not state or saved.get("state") != state or saved.get("exp", 0) < time.time():
        return False
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": config.settings.google_oauth_client_id,
            "client_secret": config.settings.google_oauth_client_secret,
            "redirect_uri": redirect_uri(),
            "grant_type": "authorization_code",
        })
        r.raise_for_status()
        data = r.json()
    if not data.get("refresh_token"):
        return False
    await db.kv_set("gmail_refresh_token", data["refresh_token"])
    await db.kv_set("oauth_state", None)
    _token.update(value=data.get("access_token"), exp=time.time() + int(data.get("expires_in", 3000)) - 60)
    return True


async def connected() -> bool:
    return configured() and bool(await db.kv_get("gmail_refresh_token"))


async def _access_token() -> str:
    if _token["value"] and _token["exp"] > time.time():
        return _token["value"]
    refresh = await db.kv_get("gmail_refresh_token")
    if not refresh:
        raise RuntimeError("Gmail no está conectado. Mandá /gmail para conectarlo.")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://oauth2.googleapis.com/token", data={
            "client_id": config.settings.google_oauth_client_id,
            "client_secret": config.settings.google_oauth_client_secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        })
        if r.status_code == 400:
            raise RuntimeError("Google rechazó el permiso de Gmail. Mandá /gmail para reconectarlo.")
        r.raise_for_status()
        data = r.json()
    _token.update(value=data["access_token"], exp=time.time() + int(data.get("expires_in", 3000)) - 60)
    return _token["value"]


async def _get(path: str, params: dict | list | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{API}/{path}", params=params,
                        headers={"Authorization": f"Bearer {await _access_token()}"})
        r.raise_for_status()
        return r.json()


def _headers(msg: dict) -> dict:
    return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}


async def search(query: str, max_results: int = 10) -> list[dict]:
    data = await _get("messages", {"q": query, "maxResults": max(1, min(max_results, 20))})
    out = []
    for m in data.get("messages", []):
        msg = await _get(f"messages/{m['id']}", [("format", "metadata"),
                                                  ("metadataHeaders", "From"),
                                                  ("metadataHeaders", "Subject"),
                                                  ("metadataHeaders", "Date")])
        h = _headers(msg)
        out.append({
            "id": m["id"],
            "de": h.get("from"),
            "asunto": h.get("subject"),
            "fecha": h.get("date"),
            "extracto": html.unescape(msg.get("snippet", "")),
            "no_leido": "UNREAD" in msg.get("labelIds", []),
        })
    return out


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _body(payload: dict) -> str:
    plain, rich = [], []

    def walk(part: dict):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            rich.append(_decode(data))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain)
    text = re.sub(r"<(script|style).*?</\1>", " ", "\n".join(rich), flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


async def read(message_id: str) -> dict:
    msg = await _get(f"messages/{message_id}", {"format": "full"})
    h = _headers(msg)
    body = re.sub(r"\n\s*\n+", "\n\n", _body(msg.get("payload", {}))).strip()
    return {"id": message_id, "de": h.get("from"), "para": h.get("to"), "asunto": h.get("subject"),
            "fecha": h.get("date"), "texto": body[:6000]}
