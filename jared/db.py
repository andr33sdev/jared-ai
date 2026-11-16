"""Acceso a Postgres (Supabase o cualquier Postgres)."""
from __future__ import annotations

import json
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

pool: AsyncConnectionPool | None = None

SCHEMA = """
create table if not exists messages (
    id bigserial primary key,
    role text not null,
    content text not null,
    created_at timestamptz not null default now()
);
create table if not exists memories (
    id bigserial primary key,
    category text not null,
    content text not null,
    amount numeric,
    currency text,
    tags text[] not null default '{}',
    happened_on date,
    active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists memories_fts
    on memories using gin (to_tsvector('spanish', content));
create index if not exists memories_cat on memories (category, created_at desc);
create table if not exists reminders (
    id bigserial primary key,
    text text not null,
    due_at timestamptz not null,
    recurrence text not null default 'none',
    done boolean not null default false,
    created_at timestamptz not null default now()
);
create index if not exists reminders_due on reminders (done, due_at);
create table if not exists kv (
    key text primary key,
    value jsonb not null,
    updated_at timestamptz not null default now()
);
create table if not exists processed_updates (
    update_id bigint primary key,
    created_at timestamptz not null default now()
);
"""


async def init(url: str) -> None:
    global pool
    if pool is not None:
        return
    pool = AsyncConnectionPool(
        url,
        min_size=1,
        max_size=4,
        open=False,
        # prepare_threshold=None: compatible con el pooler de Supabase
        kwargs={"prepare_threshold": None, "row_factory": dict_row, "autocommit": True},
    )
    await pool.open(wait=True, timeout=30)
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)


async def close() -> None:
    global pool
    if pool is not None:
        await pool.close()
        pool = None


async def fetch(sql: str, *args: Any) -> list[dict]:
    assert pool is not None, "DB no inicializada"
    async with pool.connection() as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchall() if cur.description else []


async def fetchrow(sql: str, *args: Any) -> dict | None:
    rows = await fetch(sql, *args)
    return rows[0] if rows else None


async def execute(sql: str, *args: Any) -> None:
    assert pool is not None, "DB no inicializada"
    async with pool.connection() as conn:
        await conn.execute(sql, args)


# --- kv -----------------------------------------------------------------

async def kv_get(key: str, default: Any = None) -> Any:
    row = await fetchrow("select value from kv where key = %s", key)
    return row["value"] if row else default


async def kv_set(key: str, value: Any) -> None:
    await execute(
        "insert into kv (key, value, updated_at) values (%s, %s::jsonb, now()) "
        "on conflict (key) do update set value = excluded.value, updated_at = now()",
        key,
        json.dumps(value, ensure_ascii=False),
    )


# --- mensajes -----------------------------------------------------------

async def add_message(role: str, content: str) -> None:
    await execute("insert into messages (role, content) values (%s, %s)", role, content)


async def recent_messages(limit: int = 24) -> list[dict]:
    rows = await fetch(
        "select role, content, created_at from messages order by id desc limit %s", limit
    )
    return list(reversed(rows))


async def mark_update(update_id: int) -> bool:
    """Devuelve True si el update es nuevo (no procesado antes)."""
    row = await fetchrow(
        "insert into processed_updates (update_id) values (%s) "
        "on conflict do nothing returning update_id",
        update_id,
    )
    return row is not None
