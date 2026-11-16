"""El cerebro de Jared: Claude + herramientas + memoria."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from anthropic import AsyncAnthropic

from . import config, db, extras, persona, usage
from .tools import TOOL_DEFS, now_local, run_tool, when_label

log = logging.getLogger("jared.agent")

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_client: AsyncAnthropic | None = None
lock = asyncio.Lock()  # una conversación a la vez para mantener el historial ordenado


def client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=config.settings.anthropic_api_key, max_retries=3)
    return _client


def tools() -> list[dict]:
    web = {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 3,
        "user_location": {
            "type": "approximate",
            "city": config.settings.city,
            "country": "AR",
            "timezone": config.settings.tz_name,
        },
    }
    return [*TOOL_DEFS, web]


async def context_block(mode: str | None) -> str:
    """Parte dinámica del prompt: fecha, perfil, memorias clave y pendientes."""
    now = now_local()
    parts = [f"## Ahora\n{DIAS[now.weekday()]} {now:%d/%m/%Y %H:%M} ({config.settings.tz_name})"]

    profile = await db.kv_get("profile")
    parts.append("## Perfil de " + config.settings.owner_name + "\n" + (profile or "(todavía vacío: andá completándolo)"))

    key_rows = await db.fetch(
        "select id, category, content from memories where active and category in "
        "('preferencia','interes','objetivo','proyecto','persona') order by updated_at desc limit 40"
    )
    if key_rows:
        parts.append("## Memorias clave\n" + "\n".join(
            f"- [{r['id']}·{r['category']}] {r['content']}" for r in key_rows))

    recent = await db.fetch(
        "select id, category, content, amount, currency from memories where active "
        "and category not in ('preferencia','interes','objetivo','proyecto','persona','tarea') "
        "order by created_at desc limit 15"
    )
    if recent:
        lines = []
        for r in recent:
            amt = f" ({float(r['amount']):,.0f} {r['currency']})" if r["amount"] is not None else ""
            lines.append(f"- [{r['id']}·{r['category']}] {r['content']}{amt}")
        parts.append("## Anotado recientemente\n" + "\n".join(lines))

    tasks = await db.fetch(
        "select id, content from memories where active and category = 'tarea' and status is null "
        "order by created_at limit 15"
    )
    if tasks:
        parts.append("## Tareas abiertas\n" + "\n".join(f"- [{r['id']}] {r['content']}" for r in tasks))

    waits = await extras.open_waits()
    if waits:
        parts.append("## Esperando de otros\n" + "\n".join(
            f"- [{r['id']}] {r['content']} (para el {r['happened_on']:%d/%m})" for r in waits[:15]))

    dates = await extras.upcoming_dates(21)
    if dates:
        parts.append("## Fechas importantes (próximas 3 semanas)\n" + "\n".join(
            f"- {d['content']}: {d['proxima']:%d/%m} (faltan {d['faltan']} días)" for d in dates))

    habits = await extras.get_habits()
    if habits:
        parts.append("## Hábitos que sigue\n" + ", ".join(habits))

    rems = await db.fetch(
        "select id, text, due_at, recurrence from reminders where not done "
        "and due_at <= now() + interval '7 days' order by due_at limit 15"
    )
    if rems:
        tz = config.settings.tz
        parts.append("## Recordatorios próximos (7 días)\n" + "\n".join(
            f"- [{r['id']}] {when_label(r['due_at'])} — {r['text']}"
            + ("" if r["recurrence"] == "none" else f" (se repite: {r['recurrence']})")
            for r in rems))

    if not config.settings.calendar_enabled:
        parts.append("(Google Calendar todavía no está conectado.)")
    if mode == "charla":
        parts.append(persona.charla())
    return "\n\n".join(parts)


def _history_to_messages(rows: list[dict]) -> list[dict]:
    msgs: list[dict] = []
    for r in rows:
        role = "assistant" if r["role"] == "assistant" else "user"
        text = r["content"]
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n\n" + text
        else:
            msgs.append({"role": role, "content": text})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def _dump(block: Any) -> dict:
    return block.model_dump(mode="json", exclude_none=True)


async def run(
    user_content: str | list,
    *,
    record_as: str | None = None,
    use_history: bool = True,
    save: bool = True,
    extra_system: str = "",
    max_steps: int = 15,
    smart: bool | None = None,
) -> str:
    """Corre un turno completo de Jared y devuelve el texto final.

    user_content: texto o lista de bloques (texto/imagen) del usuario.
    record_as: cómo guardar el mensaje del usuario en el historial (por defecto el texto).
    """
    async with lock:
        mode = await db.kv_get("mode")
        if smart is None:
            smart = mode == "charla" or _has_document(user_content)
        model = config.settings.model_smart if smart else config.settings.model
        system = [
            {"type": "text", "text": persona.persona(), "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": await context_block(mode) + ("\n\n" + extra_system if extra_system else "")},
        ]
        messages = _history_to_messages(await db.recent_messages(16)) if use_history else []
        if messages and messages[-1]["role"] == "user":
            messages.append({"role": "assistant", "content": "(sigo atento)"})
        messages.append({"role": "user", "content": user_content})

        final_text = ""
        for _ in range(max_steps):
            resp = await client().messages.create(
                model=model,
                max_tokens=4000,
                system=system,
                tools=tools(),
                messages=_with_cache_breakpoint(messages),
            )
            try:
                await usage.record(model, resp.usage)
            except Exception:
                log.exception("No pude registrar el uso")
            messages.append({"role": "assistant", "content": [_dump(b) for b in resp.content]})
            texts = [b.text for b in resp.content if b.type == "text"]
            if texts:
                final_text = "".join(texts).strip()

            if resp.stop_reason == "tool_use":
                results = []
                for b in resp.content:
                    if b.type == "tool_use":
                        out, is_err = await run_tool(b.name, dict(b.input or {}))
                        log.info("tool %s(%s) -> %s", b.name, json.dumps(b.input, ensure_ascii=False)[:200], out[:200])
                        results.append({"type": "tool_result", "tool_use_id": b.id, "content": out, "is_error": is_err})
                messages.append({"role": "user", "content": results})
                continue
            if resp.stop_reason == "pause_turn":
                continue
            break

        if save:
            if record_as is None:
                record_as = user_content if isinstance(user_content, str) else _text_of(user_content)
            await db.add_message("user", record_as)
            if final_text:
                await db.add_message("assistant", final_text)
        return final_text


def _has_document(content: Any) -> bool:
    return isinstance(content, list) and any(b.get("type") == "document" for b in content)


def _with_cache_breakpoint(messages: list[dict]) -> list[dict]:
    """Marca el último mensaje del usuario para que la API cachee todo lo anterior.

    En cada vuelta del ciclo de herramientas se reenvía la conversación entera;
    así esa parte se cobra al 10% en vez de precio completo.
    """
    out = []
    for i, m in enumerate(messages):
        content = m["content"]
        is_last = i == len(messages) - 1
        if isinstance(content, list):
            blocks = []
            for b in content:
                if isinstance(b, dict) and "cache_control" in b:
                    b = {k: v for k, v in b.items() if k != "cache_control"}
                blocks.append(b)
            content = blocks
        if is_last and m["role"] == "user":
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if content:
                content = [*content[:-1], {**content[-1], "cache_control": {"type": "ephemeral"}}]
        out.append({"role": m["role"], "content": content})
    return out


def _text_of(blocks: list) -> str:
    out = []
    for b in blocks:
        if b.get("type") == "text":
            out.append(b["text"])
        elif b.get("type") == "image":
            out.append("[foto]")
    return " ".join(out) or "[adjunto]"
