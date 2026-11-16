import copy
import json
from datetime import datetime, timedelta

import pytest
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from jared import agent, config, db, scheduler, tools
from jared.main import handle_update
from jared.textfmt import md_to_tg_html, split_text

pytestmark = pytest.mark.asyncio


# --- utilidades puras -----------------------------------------------------

async def test_markdown_to_html():
    out = md_to_tg_html("**Hola** <tú> & [link](https://x.com/a?b=1&c=2) `code`")
    assert "<b>Hola</b>" in out
    assert "&lt;tú&gt; &amp;" in out
    assert '<a href="https://x.com/a?b=1&amp;c=2">link</a>' in out
    assert "<code>code</code>" in out


async def test_split_text():
    text = "\n".join(["x" * 100] * 100)
    chunks = split_text(text, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks) == "x" * 10000


async def test_recurrences():
    tz = config.settings.tz
    due = datetime(2026, 1, 31, 9, 0, tzinfo=tz)
    assert tools.next_occurrence(due, "monthly", due).date().isoformat() == "2026-02-28"
    fri = datetime(2026, 11, 20, 9, 0, tzinfo=tz)  # viernes
    assert tools.next_occurrence(fri, "weekdays", fri).weekday() == 0
    assert tools.next_occurrence(due, "none", due) is None
    old = datetime(2026, 1, 1, 8, 0, tzinfo=tz)
    nxt = tools.next_occurrence(old, "daily", datetime(2026, 3, 1, 10, 0, tzinfo=tz))
    assert nxt == datetime(2026, 3, 2, 8, 0, tzinfo=tz)


# --- herramientas contra Postgres -------------------------------------------

async def test_memory_tools(database):
    await tools.guardar_memoria("idea", "Vender soportes impresos en 3D por Mercado Libre", tags=["impresion3d"])
    await tools.guardar_memoria("gasto", "Filamento PLA", amount=25000, tags=["impresion3d"])
    await tools.guardar_memoria("gasto", "Nafta", amount=40000, tags=["auto"])
    found = json.loads(await tools.buscar_memoria(query="impresos"))
    assert found["cantidad"] == 1
    by_tag = json.loads(await tools.buscar_memoria(query="impresion3d"))
    assert by_tag["cantidad"] == 2
    gastos = json.loads(await tools.resumen_gastos(desde=(tools.now_local() - timedelta(days=1)).isoformat()))
    assert gastos["totales"]["gasto ARS"] == 65000
    mem_id = found["resultados"][0]["id"]
    await tools.borrar_memoria(mem_id)
    assert json.loads(await tools.buscar_memoria(query="impresos"))["cantidad"] == 0


async def test_reminders_fire_and_repeat(database, sent):
    past = (tools.now_local() - timedelta(minutes=1)).isoformat()
    await db.execute(
        "insert into reminders (text, due_at, recurrence) values ('Tomar agua', %s, 'daily'), ('Llamar a Juan', %s, 'none')",
        tools.parse_dt(past), tools.parse_dt(past),
    )
    await scheduler.send_due_reminders()
    assert [t for _, t in sent] == ["⏰ **Recordatorio:** Tomar agua", "⏰ **Recordatorio:** Llamar a Juan"]
    rows = await db.fetch("select text, done, due_at from reminders order by id")
    assert rows[0]["done"] is False and rows[0]["due_at"] > datetime.now(config.settings.tz)
    assert rows[1]["done"] is True
    sent.clear()
    await scheduler.send_due_reminders()
    assert sent == []


async def test_create_reminder_rejects_past(database):
    out = json.loads(await tools.crear_recordatorio("x", "2020-01-01T10:00"))
    assert out["ok"] is False
    ok = json.loads(await tools.crear_recordatorio("Llamar proveedor", (tools.now_local() + timedelta(hours=2)).isoformat()))
    assert ok["ok"] is True


async def test_calendar_not_configured(database):
    out = json.loads(await tools.ver_agenda("2026-11-16", "2026-11-17"))
    assert "no está configurado" in out["error"]


# --- agente con un Claude simulado --------------------------------------

def _msg(content, stop):
    return Message(id="m", type="message", role="assistant", model="test", content=content,
                   stop_reason=stop, stop_sequence=None, usage=Usage(input_tokens=1, output_tokens=1))


class FakeClaude:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = self

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return self.script.pop(0)


async def test_agent_saves_memory_and_history(database, monkeypatch):
    fake = FakeClaude([
        _msg([ToolUseBlock(id="t1", type="tool_use", name="guardar_memoria",
                           input={"category": "interes", "content": "A Andrés le interesa la impresión 3D"})], "tool_use"),
        _msg([TextBlock(type="text", text="Anotado, Andrés. Qué lindo hobby.")], "end_turn"),
    ])
    monkeypatch.setattr(agent, "client", lambda: fake)
    reply = await agent.run("Me encanta la impresión 3D")
    assert reply == "Anotado, Andrés. Qué lindo hobby."
    mem = await db.fetch("select category, content from memories")
    assert mem == [{"category": "interes", "content": "A Andrés le interesa la impresión 3D"}]
    hist = await db.recent_messages()
    assert [h["role"] for h in hist] == ["user", "assistant"]
    # el segundo llamado incluye el resultado de la herramienta
    last = fake.calls[1]["messages"][-1]["content"][0]
    assert last["type"] == "tool_result" and last["tool_use_id"] == "t1"
    # el contexto del segundo turno ya muestra la memoria
    fake2 = FakeClaude([_msg([TextBlock(type="text", text="Sí")], "end_turn")])
    monkeypatch.setattr(agent, "client", lambda: fake2)
    await agent.run("¿Te acordás?")
    system_text = fake2.calls[0]["system"][1]["text"]
    assert "impresión 3D" in system_text
    assert fake2.calls[0]["messages"][0]["content"] == "Me encanta la impresión 3D"


async def test_handle_update_owner_and_stranger(database, sent, monkeypatch):
    fake = FakeClaude([_msg([TextBlock(type="text", text="¡Buen día, Andrés!")], "end_turn")])
    monkeypatch.setattr(agent, "client", lambda: fake)
    await handle_update({"update_id": 1, "message": {"chat": {"id": 42, "type": "private"},
                                                      "from": {"id": 42}, "text": "hola"}})
    assert sent[-1] == (42, "¡Buen día, Andrés!")
    await handle_update({"update_id": 2, "message": {"chat": {"id": 7, "type": "private"},
                                                      "from": {"id": 7}, "text": "hola"}})
    assert sent[-1][0] == 7 and "exclusivamente" in sent[-1][1]
    await handle_update({"update_id": 3, "message": {"chat": {"id": 42, "type": "private"},
                                                      "from": {"id": 42}, "text": "/recordatorios"}})
    assert "No tenés recordatorios" in sent[-1][1]


async def test_checkin_nada_is_silent(database, sent, monkeypatch):
    fake = FakeClaude([_msg([TextBlock(type="text", text="NADA")], "end_turn")])
    monkeypatch.setattr(agent, "client", lambda: fake)
    await scheduler.checkin()
    assert sent == []
    assert await db.recent_messages() == []


async def test_daily_jobs_run_once(database, monkeypatch):
    calls = []

    async def fake_job():
        calls.append(1)

    now = tools.now_local()
    monkeypatch.setattr(config.settings, "brief_time", f"{now.hour:02d}:{now.minute:02d}")
    monkeypatch.setattr(config.settings, "checkin_time", "00:00" if now.hour >= 4 else "23:59")
    monkeypatch.setattr(config.settings, "reflection_time", "23:59" if now.hour < 20 else "00:00")
    monkeypatch.setattr(scheduler, "JOBS", [("brief", "brief_time", fake_job)])
    await scheduler.daily_jobs()
    await scheduler.daily_jobs()
    assert calls == [1]


async def test_late_reminder_is_flagged(database, sent):
    late = tools.now_local() - timedelta(hours=1)
    await db.execute("insert into reminders (text, due_at) values ('Llamar a Juan', %s)", late)
    await scheduler.send_due_reminders()
    assert "perdón la demora" in sent[0][1] and f"{late:%H:%M}" in sent[0][1]


async def test_missed_daily_job_is_skipped(database, monkeypatch):
    calls = []

    async def fake_job():
        calls.append(1)

    past = tools.now_local() - timedelta(hours=1)
    if past.date() != tools.now_local().date():
        pytest.skip("cerca de medianoche")
    monkeypatch.setattr(config.settings, "brief_time", f"{past.hour:02d}:{past.minute:02d}")
    monkeypatch.setattr(scheduler, "JOBS", [("brief", "brief_time", fake_job)])
    await scheduler.daily_jobs()
    assert calls == []


async def test_when_label():
    now = tools.now_local().replace(hour=10, minute=0)
    assert tools.when_label(now) == "hoy 10:00"
    assert tools.when_label(now + timedelta(days=1)) == "mañana 10:00"
    assert tools.when_label(now + timedelta(days=3)).endswith(f"{(now + timedelta(days=3)):%d/%m} 10:00")
