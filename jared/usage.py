"""Registro del consumo de la API de Claude y costo estimado."""
from __future__ import annotations

from . import db
from .tools import now_local

# USD por millón de tokens (entrada, salida, escritura de caché, lectura de caché)
PRICES = {
    "claude-sonnet": (2.0, 10.0, 2.5, 0.2),
    "claude-opus": (4.0, 20.0, 5.0, 0.2),
    "claude-haiku": (1.0, 5.0, 1.25, 0.1),
}
SEARCH_PRICE = 10.0 / 1000  # USD por búsqueda web


def _prices(model: str) -> tuple[float, float, float, float]:
    for prefix, p in PRICES.items():
        if model.startswith(prefix):
            return p
    return PRICES["claude-sonnet"]


async def record(model: str, usage) -> None:
    if usage is None:
        return
    stu = getattr(usage, "server_tool_use", None)
    searches = getattr(stu, "web_search_requests", 0) or 0 if stu else 0
    await db.execute(
        "insert into usage (day, model, input, output, cache_write, cache_read, searches) "
        "values (%s, %s, %s, %s, %s, %s, %s) on conflict (day, model) do update set "
        "input = usage.input + excluded.input, output = usage.output + excluded.output, "
        "cache_write = usage.cache_write + excluded.cache_write, "
        "cache_read = usage.cache_read + excluded.cache_read, searches = usage.searches + excluded.searches",
        now_local().date(), model,
        getattr(usage, "input_tokens", 0) or 0,
        getattr(usage, "output_tokens", 0) or 0,
        getattr(usage, "cache_creation_input_tokens", 0) or 0,
        getattr(usage, "cache_read_input_tokens", 0) or 0,
        searches,
    )


def cost(row: dict) -> float:
    pin, pout, pcw, pcr = _prices(row["model"])
    return (row["input"] * pin + row["output"] * pout + row["cache_write"] * pcw
            + row["cache_read"] * pcr) / 1_000_000 + row["searches"] * SEARCH_PRICE


async def summary() -> dict:
    today = now_local().date()
    first = today.replace(day=1)
    rows = await db.fetch("select * from usage where day >= %s", first)
    month = sum(cost(r) for r in rows)
    day = sum(cost(r) for r in rows if r["day"] == today)
    searches = sum(r["searches"] for r in rows)
    days = (today - first).days + 1
    import calendar
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    projection = month / days * days_in_month if days else 0
    return {"hoy": day, "mes": month, "busquedas": searches, "proyeccion": projection}
