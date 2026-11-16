"""Herramientas que Jared puede usar (memoria, recordatorios, agenda)."""
from __future__ import annotations

import calendar as _cal
import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Awaitable, Callable

from . import config, db, gcal

log = logging.getLogger("jared.tools")

CATEGORIES = [
    "idea", "pensamiento", "gasto", "ingreso", "tarea", "interes", "persona",
    "preferencia", "objetivo", "proyecto", "dato",
]
RECURRENCES = ["none", "daily", "weekdays", "weekly", "monthly"]


# --- utilidades de fechas ------------------------------------------------

def now_local() -> datetime:
    return datetime.now(config.settings.tz)


def parse_dt(value: str) -> datetime:
    """ISO 8601; si no trae zona horaria, se asume la local."""
    v = value.strip().replace("Z", "+00:00")
    if len(v) == 10:  # solo fecha
        v += "T09:00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.settings.tz)
    return dt


def next_occurrence(due: datetime, recurrence: str, after: datetime) -> datetime | None:
    """Siguiente fecha de un recordatorio recurrente posterior a `after`."""
    if recurrence == "none":
        return None
    tz = config.settings.tz
    cur = due.astimezone(tz)
    for _ in range(2000):
        if recurrence == "daily":
            cur = cur + timedelta(days=1)
        elif recurrence == "weekdays":
            cur = cur + timedelta(days=1)
            while cur.weekday() >= 5:
                cur = cur + timedelta(days=1)
        elif recurrence == "weekly":
            cur = cur + timedelta(days=7)
        elif recurrence == "monthly":
            y, m = (cur.year + 1, 1) if cur.month == 12 else (cur.year, cur.month + 1)
            day = min(due.astimezone(tz).day, _cal.monthrange(y, m)[1])
            cur = cur.replace(year=y, month=m, day=day)
        else:
            return None
        if cur > after:
            return cur
    return None


DIAS_CORTOS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
REPITE = {"daily": "todos los días", "weekdays": "días hábiles", "weekly": "cada semana", "monthly": "cada mes"}


def when_label(dt: datetime) -> str:
    """'hoy 15:36', 'mañana 09:00', 'jue 19/11 13:45'."""
    d = dt.astimezone(config.settings.tz)
    days = (d.date() - now_local().date()).days
    if days == 0:
        return f"hoy {d:%H:%M}"
    if days == 1:
        return f"mañana {d:%H:%M}"
    return f"{DIAS_CORTOS[d.weekday()]} {d:%d/%m %H:%M}"


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        if isinstance(obj, datetime):
            return obj.astimezone(config.settings.tz).isoformat(timespec="minutes")
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def _out(obj: Any) -> str:
    return json.dumps(_jsonable(obj), ensure_ascii=False)


# --- memoria -------------------------------------------------------------

async def guardar_memoria(category: str, content: str, amount: float | None = None,
                          currency: str | None = None, tags: list[str] | None = None,
                          happened_on: str | None = None) -> str:
    if category not in CATEGORIES:
        category = "dato"
    if category in ("gasto", "ingreso") and amount is not None and not currency:
        currency = "ARS"
    row = await db.fetchrow(
        "insert into memories (category, content, amount, currency, tags, happened_on) "
        "values (%s, %s, %s, %s, %s, %s) returning id",
        category, content.strip(), amount, currency,
        [t.lower().strip() for t in (tags or [])],
        happened_on or (now_local().date().isoformat() if category in ("gasto", "ingreso") else None),
    )
    return _out({"ok": True, "id": row["id"]})


async def buscar_memoria(query: str | None = None, category: str | None = None,
                         since: str | None = None, limit: int = 15) -> str:
    where = ["active"]
    args: list[Any] = []
    if category:
        where.append("category = %s")
        args.append(category)
    if since:
        where.append("created_at >= %s")
        args.append(parse_dt(since))
    order = "created_at desc"
    if query:
        where.append(
            "(to_tsvector('spanish', content) @@ plainto_tsquery('spanish', %s) "
            "or content ilike %s or %s = any(tags))"
        )
        args += [query, f"%{query}%", query.lower()]
    sql = (
        "select id, category, content, amount, currency, tags, happened_on, created_at "
        f"from memories where {' and '.join(where)} order by {order} limit %s"
    )
    rows = await db.fetch(sql, *args, max(1, min(int(limit), 50)))
    return _out({"resultados": rows, "cantidad": len(rows)})


async def editar_memoria(id: int, content: str | None = None, category: str | None = None,
                         amount: float | None = None, tags: list[str] | None = None) -> str:
    sets, args = ["updated_at = now()"], []
    for col, val in (("content", content), ("category", category), ("amount", amount), ("tags", tags)):
        if val is not None:
            sets.append(f"{col} = %s")
            args.append(val)
    row = await db.fetchrow(
        f"update memories set {', '.join(sets)} where id = %s returning id", *args, id
    )
    return _out({"ok": bool(row)})


async def borrar_memoria(id: int) -> str:
    row = await db.fetchrow("update memories set active = false where id = %s returning id", id)
    return _out({"ok": bool(row)})


async def resumen_gastos(desde: str, hasta: str | None = None) -> str:
    d0 = parse_dt(desde).date()
    d1 = parse_dt(hasta).date() if hasta else now_local().date()
    rows = await db.fetch(
        "select category, currency, coalesce(t.tag, '(sin etiqueta)') as tag, "
        "sum(amount) as total, count(*) as cantidad "
        "from memories left join lateral (select tags[1] as tag) t on true "
        "where active and category in ('gasto','ingreso') and amount is not null "
        "and coalesce(happened_on, created_at::date) between %s and %s "
        "group by 1,2,3 order by 1, 4 desc",
        d0, d1,
    )
    totals: dict[str, float] = {}
    for r in rows:
        k = f"{r['category']} {r['currency']}"
        totals[k] = totals.get(k, 0) + float(r["total"])
    return _out({"desde": d0, "hasta": d1, "por_etiqueta": rows, "totales": totals})


async def actualizar_perfil(texto: str) -> str:
    await db.kv_set("profile", texto.strip())
    return _out({"ok": True})


# --- recordatorios ---------------------------------------------------------

async def crear_recordatorio(texto: str, cuando: str, repetir: str = "none") -> str:
    due = parse_dt(cuando)
    if repetir not in RECURRENCES:
        repetir = "none"
    if due < now_local() - timedelta(minutes=1) and repetir == "none":
        return _out({"ok": False, "error": "La fecha ya pasó"})
    row = await db.fetchrow(
        "insert into reminders (text, due_at, recurrence) values (%s, %s, %s) returning id, due_at",
        texto.strip().rstrip(".")[:120], due, repetir,
    )
    return _out({"ok": True, "id": row["id"], "cuando": row["due_at"], "repetir": repetir})


async def listar_recordatorios(dias: int = 30) -> str:
    rows = await db.fetch(
        "select id, text, due_at, recurrence from reminders "
        "where not done and due_at <= now() + make_interval(days => %s) order by due_at",
        int(dias),
    )
    return _out({"pendientes": rows})


async def cancelar_recordatorio(id: int) -> str:
    row = await db.fetchrow("update reminders set done = true where id = %s returning id", id)
    return _out({"ok": bool(row)})


# --- agenda ----------------------------------------------------------------

def _need_calendar() -> str | None:
    if not config.settings.calendar_enabled:
        return _out({"error": "Google Calendar no está configurado todavía"})
    return None


async def ver_agenda(desde: str, hasta: str) -> str:
    if err := _need_calendar():
        return err
    events = await gcal.list_events(parse_dt(desde), parse_dt(hasta))
    return _out({"eventos": events})


async def crear_evento(titulo: str, inicio: str, fin: str | None = None,
                       descripcion: str | None = None, lugar: str | None = None,
                       todo_el_dia: bool = False) -> str:
    if err := _need_calendar():
        return err
    if todo_el_dia:
        d0 = parse_dt(inicio).date()
        d1 = parse_dt(fin).date() + timedelta(days=1) if fin else d0 + timedelta(days=1)
        ev = await gcal.create_event(titulo, d0, d1, descripcion, lugar, all_day=True)
    else:
        s = parse_dt(inicio)
        e = parse_dt(fin) if fin else s + timedelta(hours=1)
        ev = await gcal.create_event(titulo, s, e, descripcion, lugar)
    return _out({"ok": True, "evento": ev})


async def borrar_evento(event_id: str) -> str:
    if err := _need_calendar():
        return err
    await gcal.delete_event(event_id)
    return _out({"ok": True})


# --- definiciones para Claude ---------------------------------------------

TOOL_DEFS: list[dict] = [
    {
        "name": "guardar_memoria",
        "description": (
            "Anota algo en la memoria permanente de Andrés: ideas, pensamientos, gastos, ingresos, "
            "tareas, intereses, personas, preferencias, objetivos, proyectos o datos útiles. "
            "Usala sin que te lo pidan cada vez que Andrés cuente algo que valga la pena recordar. "
            "Antes de guardar algo que quizás ya esté, buscá con buscar_memoria para no duplicar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": CATEGORIES},
                "content": {"type": "string", "description": "Texto claro y autocontenido, en tercera persona si es un dato sobre Andrés"},
                "amount": {"type": "number", "description": "Monto, solo para gasto/ingreso"},
                "currency": {"type": "string", "description": "ARS, USD, etc. Por defecto ARS"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Etiquetas cortas; para gastos la primera es el rubro (super, nafta, casa, impresion3d…)"},
                "happened_on": {"type": "string", "description": "Fecha YYYY-MM-DD si no es hoy"},
            },
            "required": ["category", "content"],
        },
    },
    {
        "name": "buscar_memoria",
        "description": "Busca en la memoria de Andrés por texto, categoría y/o fecha. Sin query devuelve lo más reciente.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "category": {"type": "string", "enum": CATEGORIES},
                "since": {"type": "string", "description": "Fecha ISO desde la cual buscar"},
                "limit": {"type": "integer", "default": 15},
            },
        },
    },
    {
        "name": "editar_memoria",
        "description": "Corrige o actualiza una memoria existente por id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "content": {"type": "string"},
                "category": {"type": "string", "enum": CATEGORIES},
                "amount": {"type": "number"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["id"],
        },
    },
    {
        "name": "borrar_memoria",
        "description": "Olvida una memoria (cuando Andrés lo pide o quedó obsoleta).",
        "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
    },
    {
        "name": "resumen_gastos",
        "description": "Totales de gastos e ingresos anotados entre dos fechas, agrupados por rubro y moneda.",
        "input_schema": {
            "type": "object",
            "properties": {"desde": {"type": "string"}, "hasta": {"type": "string"}},
            "required": ["desde"],
        },
    },
    {
        "name": "actualizar_perfil",
        "description": (
            "Reescribe el perfil resumido de Andrés (quién es, trabajo, familia, proyectos, gustos, "
            "forma de trabajar). Es lo que leés al comienzo de cada charla: mantenelo corto (máx. 250 palabras) y al día."
        ),
        "input_schema": {"type": "object", "properties": {"texto": {"type": "string"}}, "required": ["texto"]},
    },
    {
        "name": "crear_recordatorio",
        "description": "Programa un mensaje de recordatorio para Andrés por Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "texto": {"type": "string", "description": "Título corto de QUÉ es (máx. 8 palabras), sin fecha ni hora. Ej: 'Reunión ISO en Conoflex', 'Pasarle a Jared qué llevar a la reunión ISO'. NO escribas el mensaje que le vas a mandar."},
                "cuando": {"type": "string", "description": "Fecha y hora ISO, hora local de Andrés (ej. 2026-11-17T09:30)"},
                "repetir": {"type": "string", "enum": RECURRENCES, "default": "none"},
            },
            "required": ["texto", "cuando"],
        },
    },
    {
        "name": "listar_recordatorios",
        "description": "Lista los recordatorios pendientes de los próximos días.",
        "input_schema": {"type": "object", "properties": {"dias": {"type": "integer", "default": 30}}},
    },
    {
        "name": "cancelar_recordatorio",
        "description": "Cancela un recordatorio por id.",
        "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
    },
    {
        "name": "ver_agenda",
        "description": "Lista eventos del Google Calendar de Andrés entre dos fechas/horas ISO.",
        "input_schema": {
            "type": "object",
            "properties": {"desde": {"type": "string"}, "hasta": {"type": "string"}},
            "required": ["desde", "hasta"],
        },
    },
    {
        "name": "crear_evento",
        "description": "Crea un evento en el Google Calendar de Andrés. Confirmá con él antes si hay ambigüedad.",
        "input_schema": {
            "type": "object",
            "properties": {
                "titulo": {"type": "string"},
                "inicio": {"type": "string", "description": "ISO local"},
                "fin": {"type": "string", "description": "ISO local; por defecto 1 hora después"},
                "descripcion": {"type": "string"},
                "lugar": {"type": "string"},
                "todo_el_dia": {"type": "boolean", "default": False},
            },
            "required": ["titulo", "inicio"],
        },
    },
    {
        "name": "borrar_evento",
        "description": "Borra un evento del calendario por id. Solo si Andrés lo pidió explícitamente.",
        "input_schema": {"type": "object", "properties": {"event_id": {"type": "string"}}, "required": ["event_id"]},
    },
]

HANDLERS: dict[str, Callable[..., Awaitable[str]]] = {
    "guardar_memoria": guardar_memoria,
    "buscar_memoria": buscar_memoria,
    "editar_memoria": editar_memoria,
    "borrar_memoria": borrar_memoria,
    "resumen_gastos": resumen_gastos,
    "actualizar_perfil": actualizar_perfil,
    "crear_recordatorio": crear_recordatorio,
    "listar_recordatorios": listar_recordatorios,
    "cancelar_recordatorio": cancelar_recordatorio,
    "ver_agenda": ver_agenda,
    "crear_evento": crear_evento,
    "borrar_evento": borrar_evento,
}


async def run_tool(name: str, args: dict) -> tuple[str, bool]:
    """Ejecuta una herramienta. Devuelve (resultado, es_error)."""
    fn = HANDLERS.get(name)
    if fn is None:
        return _out({"error": f"Herramienta desconocida: {name}"}), True
    try:
        return await fn(**args), False
    except Exception as exc:
        log.exception("Error en herramienta %s", name)
        return _out({"error": f"{type(exc).__name__}: {exc}"}), True
