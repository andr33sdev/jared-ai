import os

import pytest
import pytest_asyncio

os.environ.setdefault("TEST_DATABASE_URL", "postgresql://postgres@/jared_test?host=/tmp&port=5433")
os.environ["TELEGRAM_BOT_TOKEN"] = "123:TEST"
os.environ["TELEGRAM_OWNER_ID"] = "42"
os.environ["ANTHROPIC_API_KEY"] = "test"
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

from jared import config, db  # noqa: E402

config.reload()


@pytest_asyncio.fixture
async def database():
    await db.init(os.environ["TEST_DATABASE_URL"])
    await db.execute("truncate messages, memories, reminders, kv, processed_updates, usage restart identity")
    yield db
    await db.close()


@pytest.fixture
def sent(monkeypatch):
    """Captura todo lo que Jared mandaría por Telegram."""
    from jared import telegram

    out = []

    async def fake_send(chat_id, text, buttons=None):
        out.append((chat_id, text))

    async def fake_call(method, **params):
        return {}

    monkeypatch.setattr(telegram, "send_message", fake_send)
    monkeypatch.setattr(telegram, "call", fake_call)
    return out
