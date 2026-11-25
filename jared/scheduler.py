"""Lo que Jared hace solo: recordatorios, avisos de agenda y mensajes programados."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from . import agent, config, db, extras, gcal, persona, proactive, telegram
from .tools import next_occurrence, now_local

log = logging.getLogger("jared.scheduler")
_lock = asyncio.Lock()

# Si el servidor estuvo dormido y se despierta tarde, las tareas diarias
# (resumen, chequeo, reflexión) se saltean en vez de llegar "de la nada".
JOB_GRACE_MINUTES = 20
# Un recordatorio que sale más tarde que esto avisa que llegó con demora.
LATE_REMINDER_MINUTES = 5

BRIEF_PROMPT = """[Tarea automática: resumen de la mañana. {name} no escribió nada; vos iniciás la conversación.]
Armá un mensaje de buenos días breve y útil:
1. Agenda de hoy (ver_agenda de hoy 00:00 a 23:59), si el calendario está conectado.
2. Recordatorios y tareas de hoy.
2b. Si Gmail está conectado, revisá los no leídos de las últimas 24 hs (buscar_mails) y mencioná SOLO los que pidan una acción o respuesta suya (máx. 3, una línea cada uno).
{news}
4. Cerrá con una sola sugerencia o pregunta concreta para avanzar algo que le importe (un proyecto, una idea pendiente).
Si no hay nada en algún punto, salteálo sin comentarlo. Nada de relleno."""

NEWS = "3. Buscá en internet 2 o 3 novedades de las últimas 24-48 hs sobre sus intereses (ver memorias tipo 'interes'); una línea cada una, con link. Si no tiene intereses anotados, preguntale cuáles son."

CHECKIN_PROMPT = """[Tarea automática: chequeo de la tarde. {name} no escribió nada.]
Revisá memorias recientes (ideas, tareas, objetivos, proyectos) y recordatorios. Si hay algo genuinamente útil para decirle AHORA (un seguimiento de una idea que quedó en el aire, una tarea que se está por vencer, una pregunta que destrabe un proyecto), escribile un mensaje corto y natural.
Si no hay nada que valga la pena, respondé exactamente: NADA"""

REFLECTION_PROMPT = """[Tarea automática nocturna, {name} no la ve.]
Esta es la conversación de las últimas 24 horas:

{transcript}

1. Revisá si quedó algo importante sin guardar en memoria (ideas, gastos, tareas, intereses, personas, decisiones). Buscá antes para no duplicar y guardá lo que falte.
2. Si aprendiste algo nuevo y relevante sobre quién es {name}, reescribí su perfil con actualizar_perfil (integrando lo que ya dice).
Respondé solo "OK" al terminar."""


MEETING_PROMPT = """[Tarea automática: {name} tiene este evento en ~30 minutos: {evento}]
Prepará un ayuda-memoria MUY breve (máx. 5 viñetas): qué sabés del tema y de la gente involucrada (buscá en la memoria por palabras del título), tareas o ideas pendientes relacionadas, y qué conviene llevar o preguntar.
No repitas el título ni la hora (ya se los muestro). Si no encontrás nada útil, respondé exactamente: NADA"""

WEEKLY_PROMPT = """[Tarea automática: resumen semanal. {name} no escribió nada; vos iniciás.]
Armá un resumen de la semana, corto y fácil de leer en el celular:
1. Gastos de los últimos 7 días (resumen_gastos) por rubro, y cómo viene el mes contra los topes (ver_presupuesto), si hay.
2. Ideas y pensamientos que anotó esta semana (buscar_memoria con since de hace 7 días).
3. Tareas que siguen abiertas.
4. Agenda de la semana que viene, si el calendario está conectado.
5. Una sola sugerencia concreta para la semana.
Salteá lo que esté vacío sin comentarlo."""

IDEA_PROMPT = """[Tarea automática: repaso de ideas. {name} no escribió nada.]
Esta es una idea que anotó hace un tiempo y quedó en el aire:
[{id}] {content} (anotada el {fecha})
Recordásela en una o dos líneas y preguntale qué quiere hacer: avanzar, descartarla o dejarla para más adelante. Si se te ocurre un primer paso concreto y chico, sugerilo. Nada más: abajo le muestro botones."""


def _parse_hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


async def send_due_reminders() -> None:
    owner = config.settings.owner_id
    if not owner:
        return
    rows = await db.fetch(
        "select id, text, due_at, recurrence from reminders where not done and due_at <= now() order by due_at"
    )
    for r in rows:
        due_local = r["due_at"].astimezone(config.settings.tz)
        late = now_local() - due_local > timedelta(minutes=LATE_REMINDER_MINUTES)
        text = f"⏰ **Recordatorio:** {r['text']}\n{persona.line(persona.REMINDER_LINES)}"
        if late:
            text += f"\n(Era para las {due_local:%H:%M}, perdón la demora.)"
        buttons = [[("✅ Hecho", f"rem:done:{r['id']}"),
                    ("+1 hora", f"rem:1h:{r['id']}"),
                    ("Mañana", f"rem:tom:{r['id']}:{due_local:%H%M}")]]
        await telegram.send_message(owner, text, buttons=buttons)
        nxt = next_occurrence(r["due_at"], r["recurrence"], now_local())
        if nxt:
            await db.execute("update reminders set due_at = %s where id = %s", nxt, r["id"])
        else:
            await db.execute("update reminders set done = true where id = %s", r["id"])
        await db.add_message("assistant", f"(Recordatorio enviado) {text}")


async def calendar_alerts(minutes_before: int = 30) -> None:
    owner = config.settings.owner_id
    if not (owner and config.settings.calendar_enabled):
        return
    now = now_local()
    events = await gcal.list_events(now, now + timedelta(minutes=minutes_before))
    alerted: list[str] = await db.kv_get("alerted_events", [])
    changed = False
    for ev in events:
        if ev["todo_el_dia"] or ev["id"] in alerted:
            continue
        start = datetime.fromisoformat(ev["inicio"]).astimezone(config.settings.tz)
        if start < now:
            continue
        mins = max(1, round((start - now).total_seconds() / 60))
        where = f" en {ev['lugar']}" if ev.get("lugar") else ""
        header = f"📅 En {mins} min: **{ev['titulo']}**{where} ({start:%H:%M})"
        alerted.append(ev["id"])
        await db.kv_set("alerted_events", alerted[-200:])
        prep = ""
        if config.settings.meeting_prep:
            try:
                prep = await agent.run(
                    MEETING_PROMPT.format(name=config.settings.owner_name,
                                          evento=json.dumps(ev, ensure_ascii=False)),
                    save=False,
                )
            except Exception:
                log.exception("Falló la preparación de la reunión")
        if prep.strip().upper().rstrip(".") == "NADA":
            prep = ""
        message = header + ("\n\n" + prep if prep else "\n" + persona.line(persona.CALENDAR_LINES))
        await telegram.send_message(owner, message)
        await db.add_message("assistant", f"(Aviso automático de reunión) {message}")


async def morning_brief() -> None:
    news = NEWS if config.settings.news_in_brief else ""
    text = await agent.run(
        BRIEF_PROMPT.format(name=config.settings.owner_name, news=news),
        record_as="(mensaje automático: resumen de la mañana)",
    )
    if text:
        await telegram.send_message(config.settings.owner_id, text)


async def checkin() -> None:
    text = await agent.run(
        CHECKIN_PROMPT.format(name=config.settings.owner_name),
        save=False,
    )
    if text and text.strip().upper().rstrip(".") != "NADA":
        await db.add_message("user", "(mensaje automático: Jared inicia un chequeo)")
        await db.add_message("assistant", text)
        await telegram.send_message(config.settings.owner_id, text)


async def reflection() -> None:
    rows = await db.fetch(
        "select role, content from messages where created_at >= now() - interval '24 hours' order by id"
    )
    if not rows:
        return
    who = {"user": config.settings.owner_name, "assistant": "Jared"}
    transcript = "\n".join(f"{who.get(r['role'], r['role'])}: {r['content']}" for r in rows)[-60000:]
    await agent.run(
        REFLECTION_PROMPT.format(name=config.settings.owner_name, transcript=transcript),
        use_history=False,
        save=False,
    )


async def weekly_summary() -> None:
    text = await agent.run(
        WEEKLY_PROMPT.format(name=config.settings.owner_name),
        record_as="(mensaje automático: resumen semanal)",
    )
    if text:
        await telegram.send_message(config.settings.owner_id, text)


async def idea_review() -> None:
    row = await db.fetchrow(
        "select id, content, created_at from memories where active and category = 'idea' "
        "and status is null and created_at < now() - interval '5 days' "
        "and (reviewed_at is null or reviewed_at < now() - interval '30 days') "
        "order by coalesce(reviewed_at, created_at) limit 1"
    )
    if not row:
        return
    await db.execute("update memories set reviewed_at = now() where id = %s", row["id"])
    text = await agent.run(
        IDEA_PROMPT.format(name=config.settings.owner_name, id=row["id"], content=row["content"],
                           fecha=row["created_at"].astimezone(config.settings.tz).strftime("%d/%m")),
        save=False,
    )
    if not text:
        return
    await db.add_message("user", "(mensaje automático: repaso de una idea)")
    await db.add_message("assistant", text)
    await telegram.send_message(
        config.settings.owner_id, text,
        buttons=[[("🚀 Avanzar", f"idea:go:{row['id']}"),
                  ("🗑 Descartar", f"idea:no:{row['id']}"),
                  ("⏳ Más adelante", f"idea:later:{row['id']}")]],
    )


DATES_PROMPT = """[Tarea automática: fechas importantes. {name} no escribió nada.]
Estas fechas se acercan:
{items}
Avisale en un mensaje breve. Para cumpleaños y aniversarios que faltan 7 días, sugerí 3 ideas de regalo concretas según lo que sabés de esa persona (buscá en la memoria; si no sabés nada, ideas generales buenas). Para vencimientos, recordale qué tiene que hacer. Una línea por fecha salvo las ideas de regalo."""


async def important_dates() -> None:
    items = [d for d in await extras.upcoming_dates(7) if d["faltan"] in (7, 1, 0)]
    if not items:
        return
    when = {0: "HOY", 1: "mañana", 7: "en una semana"}
    lines = "\n".join(f"- {d['content']} ({d['tipo']}): {when[d['faltan']]}, {d['proxima']:%d/%m}" for d in items)
    text = await agent.run(DATES_PROMPT.format(name=config.settings.owner_name, items=lines), save=False)
    if text:
        await db.add_message("user", "(mensaje automático: fechas importantes)")
        await db.add_message("assistant", text)
        await telegram.send_message(config.settings.owner_id, text)


async def waiting_followups() -> None:
    for w in await extras.due_waits():
        await db.execute("update memories set reviewed_at = now() where id = %s", w["id"])
        await telegram.send_message(
            config.settings.owner_id,
            f"⏳ {config.settings.owner_name}, perdón que pregunte: **¿llegó esto?**\n{w['content']}",
            buttons=[[("✅ Llegó", f"esp:ok:{w['id']}"), ("❌ Todavía no", f"esp:no:{w['id']}")]],
        )


async def habits_check() -> None:
    habits = await extras.get_habits()
    if not habits:
        return
    day = now_local().strftime("%Y%m%d")
    rows = [[(f"✅ {h}", f"hab:{i}:1:{day}"), ("❌", f"hab:{i}:0:{day}")] for i, h in enumerate(habits)]
    await telegram.send_message(config.settings.owner_id, f"🌙 {config.settings.owner_name}, ¿me ayudás a completar la planilla de hábitos de hoy? Es mi momento favorito del día.", buttons=rows)


# (clave, horario, función, día de la semana o None = todos los días)
JOBS = [
    ("brief", "brief_time", morning_brief, None),
    ("checkin", "checkin_time", checkin, None),
    ("reflection", "reflection_time", reflection, None),
    ("weekly", "weekly_time", weekly_summary, "weekly_day"),
    ("ideas", "ideas_time", idea_review, "ideas_day"),
    ("dates", "dates_time", important_dates, None),
    ("waits", "waits_time", waiting_followups, None),
    ("habits", "habits_time", habits_check, None),
    ("discovery", "discovery_time", proactive.discovery, "discovery_days"),
    ("improve", "improve_time", proactive.improvement, "improve_days"),
]


async def daily_jobs() -> None:
    now = now_local()
    today = now.date().isoformat()
    last_runs: dict = await db.kv_get("last_runs", {})
    for key, attr, fn, day_attr in JOBS:
        if day_attr is not None:
            days = getattr(config.settings, day_attr)
            if now.weekday() not in (days if isinstance(days, list) else [days]):
                continue
        h, m = _parse_hhmm(getattr(config.settings, attr))
        scheduled = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if last_runs.get(key) == today or not (scheduled <= now <= scheduled + timedelta(minutes=JOB_GRACE_MINUTES)):
            continue
        last_runs[key] = today
        await db.kv_set("last_runs", last_runs)
        try:
            log.info("Ejecutando tarea diaria %s", key)
            await fn()
        except Exception:
            log.exception("Falló la tarea %s", key)


async def tick() -> None:
    if _lock.locked() or not config.settings.owner_id:
        return
    async with _lock:
        for step in (send_due_reminders, calendar_alerts, daily_jobs):
            try:
                await step()
            except Exception:
                log.exception("Error en %s", step.__name__)


async def loop(interval: int = 60) -> None:
    while True:
        await tick()
        await asyncio.sleep(interval)
