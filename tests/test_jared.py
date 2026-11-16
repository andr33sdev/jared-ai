import copy
import io
from datetime import date
import json
from datetime import datetime, timedelta

import pytest
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from jared import agent, config, db, extras, scheduler, tools
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

    def stream(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        msg = self.script.pop(0)
        return FakeStream(msg)


class FakeStream:
    """Imita client.messages.stream: emite el texto de a pedacitos."""

    def __init__(self, msg):
        self.msg = msg

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            from types import SimpleNamespace
            for b in self.msg.content:
                if b.type == "text":
                    for i in range(0, len(b.text), 7):
                        yield SimpleNamespace(type="text", text=b.text[i:i + 7])
        return gen()

    async def get_final_message(self):
        return self.msg


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
    # un "comando" viejo ya no existe: lo interpreta Jared como texto
    fake2 = FakeClaude([_msg([TextBlock(type="text", text="No tenés recordatorios.")], "end_turn")])
    monkeypatch.setattr(agent, "client", lambda: fake2)
    await handle_update({"update_id": 3, "message": {"chat": {"id": 42, "type": "private"},
                                                      "from": {"id": 42}, "text": "/recordatorios"}})
    assert fake2.calls and sent[-1][1] == "No tenés recordatorios."


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
    monkeypatch.setattr(scheduler, "JOBS", [("brief", "brief_time", fake_job, None)])
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
    monkeypatch.setattr(scheduler, "JOBS", [("brief", "brief_time", fake_job, None)])
    await scheduler.daily_jobs()
    assert calls == []


async def test_when_label():
    now = tools.now_local().replace(hour=10, minute=0)
    assert tools.when_label(now) == "hoy 10:00"
    assert tools.when_label(now + timedelta(days=1)) == "mañana 10:00"
    assert tools.when_label(now + timedelta(days=3)).endswith(f"{(now + timedelta(days=3)):%d/%m} 10:00")


# --- features nuevas --------------------------------------------------------

def _cb(data, text="⏰ Recordatorio: Llamar a Juan", markup=None):
    msg = {"chat": {"id": 42}, "message_id": 9, "text": text}
    if markup:
        msg["reply_markup"] = markup
    return {"update_id": 100, "callback_query": {"id": "cb1", "from": {"id": 42}, "data": data, "message": msg}}


@pytest.fixture
def tg_calls(monkeypatch):
    from jared import telegram
    calls = []

    async def fake_call(method, **params):
        calls.append((method, params))
        return {}

    monkeypatch.setattr(telegram, "call", fake_call)
    return calls


async def test_reminder_buttons(database, tg_calls):
    from jared import telegram
    sent = []

    async def fake_send(chat_id, text, buttons=None):
        sent.append((text, buttons))

    telegram.send_message, old = fake_send, telegram.send_message
    try:
        await db.execute("insert into reminders (text, due_at) values ('Llamar a Juan', now())")
        await scheduler.send_due_reminders()
        assert sent[0][1][0][0] == ("✅ Hecho", "rem:done:1")
        tom = sent[0][1][0][2][1]
        await handle_update(_cb("rem:1h:1"))
        await handle_update(_cb(tom))
        rows = await db.fetch("select text, due_at from reminders where not done order by due_at")
        assert [r["text"] for r in rows] == ["Llamar a Juan", "Llamar a Juan"]
        assert rows[0]["due_at"] < rows[1]["due_at"]
        assert any(m == "editMessageText" and "Pospuesto" in p["text"] for m, p in tg_calls)
    finally:
        telegram.send_message = old


async def test_tasks_and_button(database, tg_calls):
    await tools.guardar_memoria("tarea", "Llamar al proveedor de resina")
    await tools.guardar_memoria("tarea", "Pedir presupuesto de moldes")
    await extras.mostrar_pendientes()
    send = [p for m, p in tg_calls if m == "sendMessage"][-1]
    assert "Llamar al proveedor" in send["text"]
    kb = send["reply_markup"]["inline_keyboard"]
    await handle_update(_cb(kb[0][0]["callback_data"], markup=send["reply_markup"]))
    assert len(await tools.open_tasks()) == 1
    edit = [p for m, p in tg_calls if m == "editMessageReplyMarkup"][-1]
    assert len(edit["reply_markup"]["inline_keyboard"][0]) == 1


async def test_budget_alerts_once(database):
    await tools.definir_presupuesto("super", 100000)
    r1 = json.loads(await tools.guardar_memoria("gasto", "Coto", amount=50000, tags=["super"]))
    assert "alerta_presupuesto" not in r1
    r2 = json.loads(await tools.guardar_memoria("gasto", "Día", amount=35000, tags=["super"]))
    assert "cerca del tope" in r2["alerta_presupuesto"]
    r3 = json.loads(await tools.guardar_memoria("gasto", "Chino", amount=1000, tags=["super"]))
    assert "alerta_presupuesto" not in r3
    r4 = json.loads(await tools.guardar_memoria("gasto", "Coto", amount=20000, tags=["super"]))
    assert "Se pasó" in r4["alerta_presupuesto"]
    status = await tools.budget_status()
    assert status[0]["porcentaje"] == 106


async def test_usage_cost(database):
    from jared import usage
    from anthropic.types import Usage, ServerToolUsage
    await usage.record("claude-sonnet-5-5", Usage(input_tokens=1_000_000, output_tokens=100_000,
                       cache_creation_input_tokens=0, cache_read_input_tokens=0,
                       server_tool_use=ServerToolUsage(web_search_requests=3, web_fetch_requests=0)))
    s = await usage.summary()
    assert abs(s["mes"] - (2.0 + 1.0 + 0.03)) < 1e-6


async def test_idea_review(database, monkeypatch, tg_calls):
    await tools.guardar_memoria("idea", "Vender topes de estacionamiento impresos en 3D")
    await db.execute("update memories set created_at = now() - interval '10 days'")
    fake = FakeClaude([_msg([TextBlock(type="text", text="¿Avanzamos con los topes?")], "end_turn")])
    monkeypatch.setattr(agent, "client", lambda: fake)
    await scheduler.idea_review()
    send = [p for m, p in tg_calls if m == "sendMessage"][-1]
    assert send["reply_markup"]["inline_keyboard"][0][1]["callback_data"] == "idea:no:1"
    await handle_update(_cb("idea:no:1", text=send["text"]))
    row = await db.fetchrow("select status from memories where id = 1")
    assert row["status"] == "descartada"
    tg_calls.clear()
    await scheduler.idea_review()  # ya no hay ideas para repasar
    assert tg_calls == []


async def test_meeting_prep(database, monkeypatch, sent):
    from jared import gcal
    start = tools.now_local() + timedelta(minutes=20)
    ev = {"id": "e1", "titulo": "Reunión ISO", "inicio": start.isoformat(), "fin": None,
          "todo_el_dia": False, "lugar": None, "descripcion": None}

    async def fake_list(a, b, max_results=50):
        return [ev]

    monkeypatch.setattr(gcal, "list_events", fake_list)
    monkeypatch.setattr(config.settings, "google_sa_json", "{}")
    monkeypatch.setattr(config.settings, "google_calendar_id", "x@gmail.com")
    fake = FakeClaude([_msg([TextBlock(type="text", text="• Llevá el manual de calidad")], "end_turn")])
    monkeypatch.setattr(agent, "client", lambda: fake)
    await scheduler.calendar_alerts()
    await scheduler.calendar_alerts()
    assert len(sent) == 1
    assert "Reunión ISO" in sent[0][1] and "manual de calidad" in sent[0][1]


async def test_gmail_not_connected(database):
    out = json.loads(await tools.buscar_mails())
    assert "no está conectado" in out["error"]


# --- minutas, esperas, documentos, fechas, hábitos ------------------------

from jared import extras  # noqa: E402


async def test_minuta_creates_tasks_and_waits(database):
    out = json.loads(await extras.guardar_minuta(
        titulo="Reunión ISO", resumen="Se revisó el plan de auditoría.",
        decisiones=["Auditoría interna el 1/12"],
        participantes=["Marta", "Juan"],
        acciones=[
            {"tarea": "Actualizar el manual de calidad", "es_mio": True,
             "fecha": (tools.now_local() + timedelta(days=2)).isoformat()},
            {"tarea": "Mandar el procedimiento de compras", "responsable": "Juan", "es_mio": False,
             "fecha": (tools.now_local() + timedelta(days=1)).date().isoformat()},
        ]))
    assert out["tareas"] and out["esperas"] and out["recordatorios"]
    assert len(await tools.open_tasks()) == 1
    waits = await extras.open_waits()
    assert "Juan tiene que: Mandar el procedimiento" in waits[0]["content"]


async def test_wait_followup_buttons(database, tg_calls, monkeypatch):
    await extras.registrar_espera("mandar el presupuesto", "Juan", tools.now_local().date().isoformat())
    await scheduler.waiting_followups()
    send = [p for m, p in tg_calls if m == "sendMessage"][-1]
    assert "¿Llegó?" in send["text"]
    await scheduler.waiting_followups()  # no repite el mismo día
    assert len([m for m, _ in tg_calls if m == "sendMessage"]) == 1
    fake = FakeClaude([_msg([TextBlock(type="text", text="Hola Juan, ¿cómo va el presupuesto?")], "end_turn")])
    monkeypatch.setattr(agent, "client", lambda: fake)
    await handle_update(_cb("esp:no:1", text=send["text"]))
    row = await db.fetchrow("select happened_on, status from memories where id = 1")
    assert row["happened_on"] == tools.now_local().date() + timedelta(days=2) and row["status"] is None
    await handle_update(_cb("esp:ok:1", text=send["text"]))
    assert await extras.open_waits() == []


async def test_pdf_document_flow(database, tg_calls, monkeypatch):
    from pypdf import PdfWriter
    from jared import telegram
    buf = io.BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.write(buf)
    pdf = buf.getvalue()

    async def fake_download(file_id):
        return pdf

    monkeypatch.setattr(telegram, "download_file", fake_download)
    fake = FakeClaude([
        _msg([ToolUseBlock(id="t1", type="tool_use", name="describir_documento",
                           input={"id": 1, "titulo": "Póliza del auto", "resumen": "Vence el 10/03/2027"})], "tool_use"),
        _msg([TextBlock(type="text", text="Es la póliza del auto, vence el 10/03.")], "end_turn"),
    ])
    monkeypatch.setattr(agent, "client", lambda: fake)
    await handle_update({"update_id": 77, "message": {
        "chat": {"id": 42, "type": "private"}, "from": {"id": 42},
        "document": {"file_id": "F1", "file_name": "poliza.pdf", "mime_type": "application/pdf", "file_size": len(pdf)}}})
    first_call = fake.calls[0]["messages"][-1]["content"]
    assert first_call[0]["type"] == "document" and "documento #1" in first_call[1]["text"]
    found = json.loads(await extras.buscar_documentos("póliza"))
    assert found["documentos"][0]["title"] == "Póliza del auto"
    hist = await db.recent_messages()
    assert hist[0]["content"].startswith("[documento #1: poliza.pdf]")


async def test_important_dates(database, monkeypatch, sent):
    today = tools.now_local().date()
    await extras.guardar_fecha("Cumpleaños de Juan", (today + timedelta(days=7)).replace(year=1990).isoformat(), True, "cumpleaños")
    await extras.guardar_fecha("Vence el seguro", (today + timedelta(days=1)).isoformat(), False, "vencimiento")
    await extras.guardar_fecha("Algo lejano", (today + timedelta(days=20)).isoformat(), False, "otro")
    up = await extras.upcoming_dates(30)
    assert [d["faltan"] for d in up] == [1, 7, 20]
    fake = FakeClaude([_msg([TextBlock(type="text", text="Mañana vence el seguro. En una semana cumple Juan.")], "end_turn")])
    monkeypatch.setattr(agent, "client", lambda: fake)
    await scheduler.important_dates()
    prompt = fake.calls[0]["messages"][-1]["content"][-1]["text"]
    assert "Cumpleaños de Juan" in prompt and "Algo lejano" not in prompt
    assert sent and "seguro" in sent[0][1]


async def test_feb29_birthday():
    assert extras.next_date(date(2000, 2, 29), True, date(2027, 1, 1)) == date(2027, 2, 28)
    assert extras.next_date(date(2026, 1, 1), False, date(2027, 1, 1)) is None


async def test_habits(database, tg_calls):
    await extras.definir_habitos(["Agua", "Caminar"])
    await scheduler.habits_check()
    send = [p for m, p in tg_calls if m == "sendMessage"][-1]
    kb = send["reply_markup"]
    yesterday = (tools.now_local().date() - timedelta(days=1))
    await extras.log_habit("Agua", yesterday, True)
    await handle_update(_cb(kb["inline_keyboard"][0][0]["callback_data"], markup=kb))
    edit = [p for m, p in tg_calls if m == "editMessageReplyMarkup"][-1]
    assert edit["reply_markup"]["inline_keyboard"][0][0]["text"] == "✅ Agua · 🔥 2"
    assert edit["reply_markup"]["inline_keyboard"][1][0]["text"] == "✅ Caminar"
    report = await extras.habit_report()
    assert report[0]["racha"] == 2 and report[0]["semana"].endswith("✅✅")


async def test_natural_language_modes(database):
    await extras.modo_charla(True)
    assert await db.kv_get("mode") == "charla"
    await extras.modo_charla(False)
    assert await db.kv_get("mode") is None
    await extras.respuestas_en_audio(True)
    assert await db.kv_get("voice_replies") is True
    assert "mes_usd" in json.loads(await extras.ver_costo())


async def test_model_routing_and_cache(database, monkeypatch):
    fake = FakeClaude([
        _msg([ToolUseBlock(id="t1", type="tool_use", name="listar_recordatorios", input={})], "tool_use"),
        _msg([TextBlock(type="text", text="Nada pendiente.")], "end_turn"),
        _msg([TextBlock(type="text", text="Contame.")], "end_turn"),
    ])
    monkeypatch.setattr(agent, "client", lambda: fake)
    await agent.run("¿Qué recordatorios tengo?")
    assert fake.calls[0]["model"] == config.settings.model == "claude-haiku-5-5"
    # una sola marca de caché en los mensajes, siempre en el último
    for call in fake.calls[:2]:
        marks = [b for m in call["messages"] if isinstance(m["content"], list)
                 for b in m["content"] if isinstance(b, dict) and "cache_control" in b]
        assert len(marks) == 1 and "cache_control" in call["messages"][-1]["content"][-1]
    await extras.modo_charla(True)
    await agent.run("Quiero pensar en voz alta")
    assert fake.calls[2]["model"] == "claude-sonnet-5-5"


# --- llamada por voz --------------------------------------------------------

async def test_voice_cap_and_fallback(database, monkeypatch, sent):
    from jared import voice
    monkeypatch.setattr(config.settings, "elevenlabs_api_key", "k")
    monkeypatch.setattr(config.settings, "elevenlabs_voice_id", "v")
    monkeypatch.setattr(config.settings, "elevenlabs_monthly_chars", 100)

    async def fake_eleven(text):
        return b"PREMIUM"

    async def fake_free(text):
        return b"GRATIS"

    monkeypatch.setattr(voice, "eleven_synthesize", fake_eleven)
    monkeypatch.setattr(voice, "synthesize", fake_free)
    audio, engine = await voice.speak("x" * 60)
    assert (audio, engine) == (b"PREMIUM", "premium") and sent == []
    audio, engine = await voice.speak("y" * 35)  # 95 -> pasa el 90%
    assert engine == "premium" and "90%" in sent[0][1]
    audio, engine = await voice.speak("z" * 10)  # superaría el tope
    assert (audio, engine) == (b"GRATIS", "gratis")
    audio, engine = await voice.speak("hola", prefer="gratis")
    assert engine == "gratis"
    assert await voice.eleven_used() == 95


async def test_call_flow(database, monkeypatch, sent):
    import httpx
    from jared import call, voice
    from jared.main import app

    async def fake_speak(text, prefer="premium"):
        return b"MP3", "gratis"

    monkeypatch.setattr(voice, "speak", fake_speak)
    fake = FakeClaude([
        _msg([TextBlock(type="text", text="Hola Andrés, te escucho.")], "end_turn"),
        _msg([TextBlock(type="text", text="Anotado. Mañana a las nueve te lo recuerdo sin falta, Andrés.")], "end_turn"),
        _msg([TextBlock(type="text", text="• Pediste recordar llamar al proveedor")], "end_turn"),
    ])
    monkeypatch.setattr(agent, "client", lambda: fake)
    link = await call.call_link() if config.settings.public_url else None
    token = await call.call_token()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/llamada")).status_code == 200
        assert (await c.post("/api/llamada/inicio", json={})).status_code == 403
        h = {"X-Call-Token": token}
        r = await c.post("/api/llamada/inicio", json={"voz": "gratis"}, headers=h)
        assert r.json()["texto"] == "Hola Andrés, te escucho." and r.json()["audio"] == "TVAz"
        assert len(r.json()["muletillas"]) == 5 and r.json()["pausa_ms"] == 900
        r = await c.post("/api/llamada/turno", json={"texto": "Recordame llamar al proveedor"}, headers=h)
        events = [json.loads(line) for line in r.text.strip().split("\n")]
        assert [e["type"] for e in events] == ["chunk", "chunk", "done"]
        assert events[0]["texto"] == "Anotado." and events[1]["texto"].startswith("Mañana a las nueve")
        assert "Llamada de voz" in fake.calls[1]["system"][1]["text"]
        assert fake.calls[1]["model"] == config.settings.model
        names = {t["name"] for t in fake.calls[1]["tools"]}
        assert "guardar_memoria" in names and "definir_habitos" not in names
        r = await c.post("/api/llamada/fin", headers=h)
        assert r.json()["ok"]
    assert "Resumen de la llamada" in sent[-1][1]
    assert "Recordame llamar al proveedor" in fake.calls[2]["messages"][-1]["content"][-1]["text"]


async def test_sentence_splitter():
    from jared.call import SentenceSplitter
    s = SentenceSplitter()
    out = []
    for ch in ["Hola, An", "drés. Te es", "cucho. Sí, ", "claro, mañana a las 9 te lo recuerdo. ¿A", "lgo más?"]:
        out += s.feed(ch)
    assert out == ["Hola, Andrés.", "Te escucho. Sí, claro, mañana a las 9 te lo recuerdo."]
    assert s.flush() == ["¿Algo más?"]
