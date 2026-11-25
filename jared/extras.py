"""Minutas, esperas, documentos, fechas importantes y hábitos."""
from __future__ import annotations

import io
import logging
from datetime import date, datetime, timedelta
from typing import Any

from . import config, db, tools
from .tools import _out, now_local, parse_dt

log = logging.getLogger("jared.extras")


# --- minutas ---------------------------------------------------------------

async def guardar_minuta(titulo: str, resumen: str, decisiones: list[str] | None = None,
                         acciones: list[dict] | None = None, participantes: list[str] | None = None,
                         fecha: str | None = None) -> str:
    fecha_d = parse_dt(fecha).date() if fecha else now_local().date()
    acciones = acciones or []
    lines = [f"Minuta: {titulo} ({fecha_d:%d/%m/%Y})"]
    if participantes:
        lines.append("Participantes: " + ", ".join(participantes))
    lines.append("Resumen: " + resumen.strip())
    if decisiones:
        lines.append("Decisiones: " + "; ".join(decisiones))
    if acciones:
        lines.append("Acciones: " + "; ".join(
            f"{a.get('tarea')} ({'yo' if a.get('es_mio') else a.get('responsable') or '?'}"
            + (f", {a['fecha']}" if a.get("fecha") else "") + ")" for a in acciones))
    row = await db.fetchrow(
        "insert into memories (category, content, tags, happened_on) values ('minuta', %s, %s, %s) returning id",
        "\n".join(lines), [t.lower() for t in (participantes or [])][:10], fecha_d,
    )
    created: dict[str, list] = {"tareas": [], "esperas": [], "recordatorios": []}
    for a in acciones:
        tarea = (a.get("tarea") or "").strip()
        if not tarea:
            continue
        if a.get("es_mio"):
            r = await db.fetchrow(
                "insert into memories (category, content, tags) values ('tarea', %s, %s) returning id",
                f"{tarea} (de la reunión: {titulo})", ["minuta"])
            created["tareas"].append(r["id"])
            if a.get("fecha"):
                due = parse_dt(a["fecha"])
                if due > now_local():
                    rr = await db.fetchrow(
                        "insert into reminders (text, due_at) values (%s, %s) returning id", tarea[:120], due)
                    created["recordatorios"].append(rr["id"])
        else:
            res = await registrar_espera(tarea, a.get("responsable") or "alguien", a.get("fecha"),
                                         origen=f"reunión: {titulo}")
            created["esperas"].append(res)
    return _out({"ok": True, "minuta_id": row["id"], **created})


# --- esperas (lo que otros te deben) --------------------------------------

async def registrar_espera(que: str, quien: str, para_cuando: str | None = None,
                           origen: str | None = None) -> Any:
    when = parse_dt(para_cuando).date() if para_cuando else now_local().date() + timedelta(days=3)
    content = f"{quien} tiene que: {que}" + (f" ({origen})" if origen else "")
    row = await db.fetchrow(
        "insert into memories (category, content, tags, happened_on) values ('espera', %s, %s, %s) returning id",
        content, [quien.lower().strip()], when)
    return row["id"]


async def registrar_espera_tool(que: str, quien: str, para_cuando: str | None = None) -> str:
    mem_id = await registrar_espera(que, quien, para_cuando)
    return _out({"ok": True, "id": mem_id})


async def cerrar_espera(id: int) -> str:
    row = await db.fetchrow(
        "update memories set status = 'recibida', updated_at = now() where id = %s and category = 'espera' "
        "returning id", id)
    return _out({"ok": bool(row)})


async def open_waits() -> list[dict]:
    return await db.fetch(
        "select id, content, tags, happened_on from memories where active and category = 'espera' "
        "and status is null order by happened_on")


async def due_waits() -> list[dict]:
    today = now_local().date()
    return await db.fetch(
        "select id, content, tags, happened_on from memories where active and category = 'espera' "
        "and status is null and happened_on <= %s "
        "and (reviewed_at is null or (reviewed_at at time zone %s)::date < %s) order by happened_on",
        today, config.settings.tz_name, today)


# --- documentos ---------------------------------------------------------------

def extract_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception as exc:
        log.warning("No pude extraer texto del PDF: %s", exc)
        return ""


async def store_document(filename: str, file_id: str | None, text: str) -> int:
    row = await db.fetchrow(
        "insert into documents (filename, file_id, text) values (%s, %s, %s) returning id",
        filename, file_id, text)
    return row["id"]


async def describir_documento(id: int, titulo: str, resumen: str) -> str:
    row = await db.fetchrow(
        "update documents set title = %s, summary = %s where id = %s returning id", titulo, resumen, id)
    return _out({"ok": bool(row)})


async def buscar_documentos(query: str | None = None, limit: int = 10) -> str:
    if query:
        rows = await db.fetch(
            "select id, filename, title, summary, created_at from documents where "
            "to_tsvector('spanish', coalesce(title,'') || ' ' || coalesce(summary,'') || ' ' || text) "
            "@@ plainto_tsquery('spanish', %s) or filename ilike %s or title ilike %s "
            "order by created_at desc limit %s", query, f"%{query}%", f"%{query}%", limit)
    else:
        rows = await db.fetch(
            "select id, filename, title, summary, created_at from documents order by created_at desc limit %s",
            limit)
    return _out({"documentos": rows})


async def leer_documento(id: int, desde: int = 0) -> str:
    row = await db.fetchrow("select id, filename, title, text from documents where id = %s", id)
    if not row:
        return _out({"error": "No existe ese documento"})
    chunk = row["text"][desde:desde + 40000]
    more = len(row["text"]) > desde + 40000
    return _out({"id": row["id"], "archivo": row["filename"], "titulo": row["title"],
                 "texto": chunk or "(el PDF no tiene texto extraíble: es escaneado)",
                 "sigue_desde": desde + 40000 if more else None})


# --- fechas importantes -----------------------------------------------------

def next_date(d: date, anual: bool, today: date) -> date | None:
    if not anual:
        return d if d >= today else None
    for year in (today.year, today.year + 1):
        try:
            cand = d.replace(year=year)
        except ValueError:  # 29/2
            cand = date(year, 2, 28)
        if cand >= today:
            return cand
    return None


async def guardar_fecha(que: str, fecha: str, anual: bool = True, tipo: str = "otro") -> str:
    d = parse_dt(fecha).date()
    tags = [tipo.lower()] + (["anual"] if anual else [])
    row = await db.fetchrow(
        "insert into memories (category, content, tags, happened_on) values ('fecha', %s, %s, %s) returning id",
        que.strip(), tags, d)
    nxt = next_date(d, anual, now_local().date())
    return _out({"ok": True, "id": row["id"], "proxima": nxt})


async def upcoming_dates(days: int = 30) -> list[dict]:
    today = now_local().date()
    rows = await db.fetch(
        "select id, content, tags, happened_on from memories where active and category = 'fecha' and status is null")
    out = []
    for r in rows:
        nxt = next_date(r["happened_on"], "anual" in (r["tags"] or []), today)
        if nxt is not None and (nxt - today).days <= days:
            out.append({**r, "proxima": nxt, "faltan": (nxt - today).days,
                        "tipo": (r["tags"] or ["otro"])[0]})
    return sorted(out, key=lambda x: x["faltan"])


async def proximas_fechas(dias: int = 60) -> str:
    return _out({"fechas": await upcoming_dates(dias)})


# --- hábitos -------------------------------------------------------------------

async def get_habits() -> list[str]:
    return await db.kv_get("habits", [])


async def definir_habitos(habitos: list[str]) -> str:
    clean = [h.strip()[:30] for h in habitos if h.strip()][:6]
    await db.kv_set("habits", clean)
    return _out({"ok": True, "habitos": clean})


async def log_habit(habit: str, day: date, done: bool) -> None:
    await db.execute(
        "insert into habit_log (day, habit, done) values (%s, %s, %s) "
        "on conflict (day, habit) do update set done = excluded.done", day, habit, done)


async def streak(habit: str, until: date) -> int:
    rows = await db.fetch(
        "select day, done from habit_log where habit = %s and day <= %s order by day desc limit 400", habit, until)
    count, expected = 0, until
    for r in rows:
        if r["day"] != expected or not r["done"]:
            break
        count += 1
        expected -= timedelta(days=1)
    return count


async def habit_report(days: int = 7) -> list[dict]:
    today = now_local().date()
    out = []
    for h in await get_habits():
        rows = await db.fetch(
            "select day, done from habit_log where habit = %s and day > %s", h, today - timedelta(days=days))
        done = {r["day"]: r["done"] for r in rows}
        grid = "".join("✅" if done.get(today - timedelta(days=i)) else
                       "❌" if (today - timedelta(days=i)) in done else "·"
                       for i in range(days - 1, -1, -1))
        last = max(done) if done else today
        out.append({"habito": h, "semana": grid, "racha": await streak(h, last),
                    "cumplidos": sum(1 for v in done.values() if v)})
    return out


async def ver_habitos() -> str:
    return _out({"habitos": await habit_report()})


# --- cosas que antes eran comandos -------------------------------------------

async def modo_charla(activo: bool) -> str:
    await db.kv_set("mode", "charla" if activo else None)
    return _out({"ok": True, "modo_charla": activo})


async def respuestas_en_audio(activo: bool) -> str:
    await db.kv_set("voice_replies", bool(activo))
    return _out({"ok": True, "audio": bool(activo)})


async def ver_costo() -> str:
    from . import usage
    u = await usage.summary()
    from . import voice
    chars = await voice.eleven_used()
    return _out({"voz_premium_mes_usd": round(chars * voice.ELEVEN_PRICE_PER_1K / 1000, 2),
                 "voz_premium_caracteres_mes": chars,
                 "voz_premium_tope_caracteres": config.settings.elevenlabs_monthly_chars,
                 "hoy_usd": round(u["hoy"], 2), "mes_usd": round(u["mes"], 2),
                 "proyeccion_mes_usd": round(u["proyeccion"], 2), "busquedas_web_mes": u["busquedas"],
                 "nota": "estimado propio, no es la factura de Anthropic"})


async def link_llamada(nuevo: bool = False) -> str:
    from . import call, telegram
    if nuevo:
        await db.kv_set("call_token", None)
    link = await call.call_link()
    if not link:
        return _out({"error": "Falta la URL pública del servidor (PUBLIC_URL)."})
    await telegram.send_message(
        config.settings.owner_id,
        "📞 Tocá para llamarme. Funciona mejor en Chrome; podés agregarlo a la pantalla de inicio.",
        buttons=[[("📞 Llamar a Jared", link)]],
    )
    return _out({"ok": True, "nota": "Ya le mandé el botón para llamar. Respondé solo una línea breve o nada."})


async def ver_esperas() -> str:
    return _out({"esperando": await open_waits()})


async def mostrar_pendientes() -> str:
    from . import telegram
    rows = await tools.open_tasks()
    if not rows:
        return _out({"ok": True, "tareas": 0, "nota": "No hay tareas abiertas; decíselo vos."})
    lines = [f"{i}. {r['content']}" for i, r in enumerate(rows, 1)]
    buttons = [[(f"✅ {i}", f"task:done:{r['id']}") for i, r in list(enumerate(rows, 1))[j:j + 5]]
               for j in range(0, len(rows), 5)]
    await telegram.send_message(config.settings.owner_id,
                                f"**Tus tareas abiertas, {config.settings.owner_name}.** Tocá ✅ en las que ya hiciste: "
                                "no sabés el placer que me da verlas cerrarse.\n" + "\n".join(lines),
                                buttons=buttons)
    return _out({"ok": True, "tareas": len(rows),
                 "nota": "Ya le mostré la lista con botones. No la repitas: como mucho una línea breve o nada."})


# --- registro en las herramientas de Jared ------------------------------------

DEFS = [
    {
        "name": "guardar_minuta",
        "description": (
            "Guarda la minuta de una reunión que Andrés te cuenta. Las acciones suyas (es_mio=true) se crean "
            "como tareas (y recordatorio si tienen fecha); las de otros quedan como 'esperas' y le preguntás "
            "si cumplieron cuando llegue la fecha."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "titulo": {"type": "string"},
                "fecha": {"type": "string", "description": "Fecha de la reunión (ISO); por defecto hoy"},
                "participantes": {"type": "array", "items": {"type": "string"}},
                "resumen": {"type": "string", "description": "2-4 oraciones"},
                "decisiones": {"type": "array", "items": {"type": "string"}},
                "acciones": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tarea": {"type": "string"},
                            "responsable": {"type": "string"},
                            "es_mio": {"type": "boolean", "description": "true si la tiene que hacer Andrés"},
                            "fecha": {"type": "string", "description": "Fecha límite ISO, si se mencionó"},
                        },
                        "required": ["tarea", "es_mio"],
                    },
                },
            },
            "required": ["titulo", "resumen"],
        },
    },
    {
        "name": "registrar_espera",
        "description": (
            "Anota algo que OTRA persona le debe a Andrés (un presupuesto, una respuesta, una entrega). "
            "Ese día le vas a preguntar si llegó. Sin fecha, en 3 días."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "que": {"type": "string"},
                "quien": {"type": "string"},
                "para_cuando": {"type": "string", "description": "Fecha ISO"},
            },
            "required": ["que", "quien"],
        },
    },
    {
        "name": "cerrar_espera",
        "description": "Marca como recibido algo que Andrés estaba esperando de otra persona.",
        "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
    },
    {
        "name": "describir_documento",
        "description": "Guarda título y resumen de un documento que Andrés te mandó (usá el id que te indiqué).",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "titulo": {"type": "string"},
                           "resumen": {"type": "string", "description": "Resumen con los datos clave: partes, montos, fechas, vencimientos"}},
            "required": ["id", "titulo", "resumen"],
        },
    },
    {
        "name": "buscar_documentos",
        "description": "Busca entre los documentos (PDFs) que Andrés te mandó. Sin query lista los últimos.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 10}}},
    },
    {
        "name": "leer_documento",
        "description": "Lee el texto completo de un documento guardado para responder preguntas sobre él.",
        "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}, "desde": {"type": "integer", "default": 0}}, "required": ["id"]},
    },
    {
        "name": "guardar_fecha",
        "description": (
            "Guarda una fecha importante: cumpleaños, aniversario, vencimiento (seguro, VTV, patente, etc.). "
            "Avisás 7 días antes, el día anterior y el mismo día."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "que": {"type": "string", "description": "Ej: 'Cumpleaños de Juan', 'Vence el seguro del auto'"},
                "fecha": {"type": "string", "description": "Fecha ISO (para anuales sirve cualquier año)"},
                "anual": {"type": "boolean", "default": True},
                "tipo": {"type": "string", "enum": ["cumpleaños", "aniversario", "vencimiento", "otro"]},
            },
            "required": ["que", "fecha"],
        },
    },
    {
        "name": "proximas_fechas",
        "description": "Fechas importantes de los próximos días.",
        "input_schema": {"type": "object", "properties": {"dias": {"type": "integer", "default": 60}}},
    },
    {
        "name": "definir_habitos",
        "description": "Define la lista de hábitos que Andrés quiere seguir (máx. 6). Reemplaza la lista anterior.",
        "input_schema": {"type": "object", "properties": {"habitos": {"type": "array", "items": {"type": "string"}}}, "required": ["habitos"]},
    },
    {
        "name": "ver_habitos",
        "description": "Cómo viene con sus hábitos: últimos 7 días y rachas.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

DEFS += [
    {
        "name": "modo_charla",
        "description": (
            "Activa el modo charla (por Telegram) cuando Andrés quiere pensar en voz alta o charlar un tema largo. "
            "Si quiere hablarte por VOZ ('te llamo'), usá link_llamada. "
            "Desactivalo cuando diga que terminó, después de hacer el cierre."
        ),
        "input_schema": {"type": "object", "properties": {"activo": {"type": "boolean"}}, "required": ["activo"]},
    },
    {
        "name": "respuestas_en_audio",
        "description": "Activa o desactiva que tus respuestas lleguen también como audio (si lo pide).",
        "input_schema": {"type": "object", "properties": {"activo": {"type": "boolean"}}, "required": ["activo"]},
    },
    {
        "name": "ver_costo",
        "description": "Cuánto se lleva gastado en la API de Claude hoy y este mes (estimado).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "link_llamada",
        "description": (
            "Le manda a Andrés el botón para llamarte por voz (página de llamada). Usala cuando diga "
            "'te llamo', 'llamame', 'quiero hablar por voz'. Con nuevo=true genera un link nuevo e invalida el anterior "
            "(si dice que lo compartió o lo perdió)."
        ),
        "input_schema": {"type": "object", "properties": {"nuevo": {"type": "boolean", "default": False}}},
    },
    {
        "name": "ver_esperas",
        "description": "Lista lo que Andrés está esperando de otras personas.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "mostrar_pendientes",
        "description": (
            "Le muestra a Andrés sus tareas abiertas en un mensaje con botones ✅ para cerrarlas. "
            "Usala cuando pida ver sus pendientes/tareas."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]

HANDLERS = {
    "link_llamada": link_llamada,
    "modo_charla": modo_charla,
    "respuestas_en_audio": respuestas_en_audio,
    "ver_costo": ver_costo,
    "ver_esperas": ver_esperas,
    "mostrar_pendientes": mostrar_pendientes,
    "guardar_minuta": guardar_minuta,
    "registrar_espera": registrar_espera_tool,
    "cerrar_espera": cerrar_espera,
    "describir_documento": describir_documento,
    "buscar_documentos": buscar_documentos,
    "leer_documento": leer_documento,
    "guardar_fecha": guardar_fecha,
    "proximas_fechas": proximas_fechas,
    "definir_habitos": definir_habitos,
    "ver_habitos": ver_habitos,
}

if "guardar_minuta" not in tools.HANDLERS:
    tools.TOOL_DEFS.extend(DEFS)
    tools.HANDLERS.update(HANDLERS)
