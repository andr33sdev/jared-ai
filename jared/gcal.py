"""Google Calendar mediante una cuenta de servicio con el calendario compartido."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from urllib.parse import quote

import httpx

from . import config

SCOPES = ["https://www.googleapis.com/auth/calendar"]
_creds = None


def _get_token_sync() -> str:
    global _creds
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    if _creds is None:
        info = json.loads(config.settings.google_sa_json)
        _creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    if not _creds.valid:
        _creds.refresh(Request())
    return _creds.token


async def _headers() -> dict:
    token = await asyncio.to_thread(_get_token_sync)
    return {"Authorization": f"Bearer {token}"}


def _base() -> str:
    cal = quote(config.settings.google_calendar_id, safe="")
    return f"https://www.googleapis.com/calendar/v3/calendars/{cal}/events"


def _simplify(ev: dict) -> dict:
    start = ev.get("start", {})
    end = ev.get("end", {})
    return {
        "id": ev.get("id"),
        "titulo": ev.get("summary", "(sin título)"),
        "inicio": start.get("dateTime") or start.get("date"),
        "fin": end.get("dateTime") or end.get("date"),
        "todo_el_dia": "date" in start,
        "lugar": ev.get("location"),
        "descripcion": (ev.get("description") or "")[:300] or None,
    }


async def list_events(start: datetime, end: datetime, max_results: int = 50) -> list[dict]:
    params = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": str(max_results),
        "timeZone": config.settings.tz_name,
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(_base(), params=params, headers=await _headers())
        r.raise_for_status()
        return [_simplify(e) for e in r.json().get("items", [])]


async def create_event(
    title: str,
    start: datetime | str,
    end: datetime | str,
    description: str | None = None,
    location: str | None = None,
    all_day: bool = False,
) -> dict:
    tz = config.settings.tz_name
    if all_day:
        body = {"start": {"date": str(start)}, "end": {"date": str(end)}}
    else:
        body = {
            "start": {"dateTime": start.isoformat(), "timeZone": tz},
            "end": {"dateTime": end.isoformat(), "timeZone": tz},
        }
    body["summary"] = title
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(_base(), json=body, headers=await _headers())
        r.raise_for_status()
        return _simplify(r.json())


async def delete_event(event_id: str) -> None:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.delete(f"{_base()}/{quote(event_id, safe='')}", headers=await _headers())
        if r.status_code not in (200, 204, 410):
            r.raise_for_status()
