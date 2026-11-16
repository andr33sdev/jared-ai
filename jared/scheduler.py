"""Lo que Jared hace solo: recordatorios, avisos de agenda y mensajes programados."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from . import agent, config, db, gcal, telegram
from .tools import next_occurrence, now_local

log = logging.getLogger("jared.scheduler")
_lock = asyncio.Lock()

BRIEF_PROMPT = """[Tarea automática: resumen de la mañana. {name} no escribió nada; vos iniciás la conversación.]
Armá un mensaje de buenos días breve y útil:
1. Agenda de hoy (ver_agenda de hoy 00:00 a 23:59), si el calendario está conectado.
2. Recordatorios y tareas de hoy.
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
        await telegram.send_message(owner, f"⏰ {r['text']}")
        nxt = next_occurrence(r["due_at"], r["recurrence"], now_local())
        if nxt:
            await db.execute("update reminders set due_at = %s where id = %s", nxt, r["id"])
        else:
            await db.execute("update reminders set done = true where id = %s", r["id"])
        await db.add_message("assistant", f"(Recordatorio enviado) ⏰ {r['text']}")


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
        await telegram.send_message(owner, f"📅 En {mins} min: **{ev['titulo']}**{where} ({start:%H:%M})")
        alerted.append(ev["id"])
        changed = True
    if changed:
        await db.kv_set("alerted_events", alerted[-200:])


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


JOBS = [
    ("brief", "brief_time", morning_brief),
    ("checkin", "checkin_time", checkin),
    ("reflection", "reflection_time", reflection),
]


async def daily_jobs() -> None:
    now = now_local()
    today = now.date().isoformat()
    last_runs: dict = await db.kv_get("last_runs", {})
    for key, attr, fn in JOBS:
        h, m = _parse_hhmm(getattr(config.settings, attr))
        scheduled = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if last_runs.get(key) == today or not (scheduled <= now <= scheduled + timedelta(hours=3)):
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
