"""Configuración de Jared, leída desde variables de entorno."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


@dataclass
class Settings:
    anthropic_api_key: str | None
    model: str
    telegram_token: str | None
    owner_id: int | None
    owner_name: str
    database_url: str | None
    tz_name: str
    city: str
    public_url: str | None
    webhook_secret: str
    groq_api_key: str | None
    google_sa_json: str | None
    google_calendar_id: str | None
    brief_time: str
    checkin_time: str
    reflection_time: str
    news_in_brief: bool
    tts_voice: str
    google_oauth_client_id: str | None
    google_oauth_client_secret: str | None
    weekly_time: str
    weekly_day: int
    ideas_time: str
    ideas_day: int
    meeting_prep: bool
    dates_time: str
    waits_time: str
    habits_time: str

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)

    @property
    def calendar_enabled(self) -> bool:
        return bool(self.google_sa_json and self.google_calendar_id)


def load() -> Settings:
    token = _env("TELEGRAM_BOT_TOKEN")
    owner = _env("TELEGRAM_OWNER_ID")
    secret = _env("WEBHOOK_SECRET") or (
        hashlib.sha256((token or "jared").encode()).hexdigest()[:32]
    )
    return Settings(
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        model=_env("JARED_MODEL", "claude-sonnet-5-5"),
        telegram_token=token,
        owner_id=int(owner) if owner else None,
        owner_name=_env("OWNER_NAME", "Andrés"),
        database_url=_env("DATABASE_URL"),
        tz_name=_env("TIMEZONE", "America/Argentina/Buenos_Aires"),
        city=_env("CITY", "Buenos Aires"),
        public_url=(_env("PUBLIC_URL") or _env("RENDER_EXTERNAL_URL")),
        webhook_secret=secret,
        groq_api_key=_env("GROQ_API_KEY"),
        google_sa_json=_env("GOOGLE_SERVICE_ACCOUNT_JSON"),
        google_calendar_id=_env("GOOGLE_CALENDAR_ID"),
        brief_time=_env("BRIEF_TIME", "08:00"),
        checkin_time=_env("CHECKIN_TIME", "16:00"),
        reflection_time=_env("REFLECTION_TIME", "23:30"),
        news_in_brief=(_env("NEWS_IN_BRIEF", "true").lower() == "true"),
        tts_voice=_env("TTS_VOICE", "es-AR-TomasNeural"),
        google_oauth_client_id=_env("GOOGLE_OAUTH_CLIENT_ID"),
        google_oauth_client_secret=_env("GOOGLE_OAUTH_CLIENT_SECRET"),
        weekly_time=_env("WEEKLY_TIME", "21:00"),
        weekly_day=int(_env("WEEKLY_DAY", "6")),  # 0=lunes … 6=domingo
        ideas_time=_env("IDEAS_TIME", "19:00"),
        ideas_day=int(_env("IDEAS_DAY", "2")),  # miércoles
        meeting_prep=(_env("MEETING_PREP", "true").lower() == "true"),
        dates_time=_env("DATES_TIME", "09:30"),
        waits_time=_env("WAITS_TIME", "10:00"),
        habits_time=_env("HABITS_TIME", "21:30"),
    )


settings = load()


def reload() -> Settings:
    global settings
    settings = load()
    return settings
