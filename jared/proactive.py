"""Jared por iniciativa propia: hallazgos de internet y mejoras sobre el día a día."""
from __future__ import annotations

import logging
import re

from . import agent, config, db, telegram
from .tools import now_local

log = logging.getLogger("jared.proactive")

DISCOVERY_PROMPT = """[Tarea automática: HALLAZGO. {name} no escribió nada; vos le traés algo por iniciativa propia.]
Tu objetivo: encontrar en internet UNA sola cosa que de verdad le sirva o le interese a {name} esta semana.

1. Elegí un tema a partir de lo que sabés de él: intereses, proyectos, objetivos, ideas recientes, problemas que mencionó, su trabajo. Usá buscar_memoria si necesitás más contexto.
2. Buscá (máximo 3 búsquedas) algo concreto y reciente: una herramienta, un producto con buen precio, una técnica, una novedad del rubro, un proveedor, una oportunidad, un artículo práctico. Que sea accionable, no genérico.
3. Si ninguna opción vale la pena, respondé exactamente: NADA

Lo que ya le mostraste (no repitas):
{history}

Lo que le gustó y lo que no, para afinar:
{feedback}

Formato del mensaje (corto, para el celular):
- Primera línea: "💡 " + qué encontraste, en pocas palabras.
- 2 o 3 líneas: qué es y por qué le sirve A ÉL en particular (conectalo con algo suyo).
- El link.
- Si aplica, una oferta concreta de seguir ("¿Querés que compare precios?", "¿Te armo un plan para probarlo?").
- Última línea, que yo voy a borrar antes de mandar: TEMA: <tema en 3-6 palabras>"""

IMPROVE_PROMPT = """[Tarea automática: MEJORA. {name} no escribió nada; vos tomás la iniciativa y trabajás por él.]
Esta es la actividad de sus últimos días:

{activity}

Tu objetivo: elegir UNA cosa de su día a día donde puedas aportar trabajo hecho, no solo una opinión. Ejemplos:
- Redactar el mail o mensaje que quedó pendiente.
- Armar un checklist, un procedimiento o una plantilla para algo que repite.
- Ordenar una idea suya en un mini plan con primeros pasos.
- Calcular algo (costos, precios, tiempos) con los datos que dio.
- Proponer cómo resolver un problema que mencionó, investigando si hace falta (máximo 2 búsquedas).
Hacelo de verdad: entregá el borrador, la lista o el cálculo listo para usar, no la promesa de hacerlo.
Si no hay nada donde puedas aportar algo valioso, respondé exactamente: NADA

Lo que ya le propusiste (no repitas):
{history}

Lo que le sirvió y lo que no:
{feedback}

Formato (corto, para el celular):
- Primera línea: "🛠️ " + qué hiciste por él, en una frase ("Te armé el mail para reclamarle a Juan el presupuesto").
- El trabajo en sí, compacto.
- Una línea final para seguir ("¿Lo ajusto?", "¿Lo mando así?").
- Última línea, que yo voy a borrar antes de mandar: TEMA: <tema en 3-6 palabras>"""


def _split_topic(text: str) -> tuple[str, str]:
    m = re.search(r"\n?\s*TEMA:\s*(.+?)\s*$", text.strip(), flags=re.I)
    if not m:
        return text.strip(), text.strip().split("\n")[0][:60]
    return text[: m.start()].strip(), m.group(1).strip()[:80]


async def _history(kind: str) -> list[str]:
    return await db.kv_get(f"{kind}_history", [])


def _bullets(items: list[str], empty: str) -> str:
    return "\n".join(f"- {i}" for i in items) if items else empty


async def _feedback_text() -> str:
    fb = await db.kv_get("proactive_feedback", [])
    if not fb:
        return "(todavía no hay)"
    return "\n".join(f"- {'👍' if f['ok'] else '👎'} {f['tema']}" for f in fb[-20:])


async def _deliver(kind: str, raw: str) -> bool:
    if not raw or raw.strip().upper().rstrip(".") == "NADA":
        return False
    body, topic = _split_topic(raw)
    if not body:
        return False
    history = await _history(kind)
    history = (history + [topic])[-30:]
    await db.kv_set(f"{kind}_history", history)
    items: dict = await db.kv_get("proactive_items", {})
    item_id = str(int(await db.kv_get("proactive_seq", 0)) + 1)
    await db.kv_set("proactive_seq", int(item_id))
    items[item_id] = {"kind": kind, "tema": topic}
    await db.kv_set("proactive_items", dict(list(items.items())[-50:]))
    await db.add_message("user", f"(mensaje automático: {'hallazgo' if kind == 'discovery' else 'mejora'} por iniciativa de Jared)")
    await db.add_message("assistant", body)
    await telegram.send_message(
        config.settings.owner_id, body,
        buttons=[[("👍 Me sirve", f"fb:up:{item_id}"), ("👎 No va", f"fb:down:{item_id}")]],
    )
    return True


async def discovery() -> bool:
    prompt = DISCOVERY_PROMPT.format(
        name=config.settings.owner_name,
        history=_bullets(await _history("discovery"), "(nada todavía)"),
        feedback=await _feedback_text(),
    )
    return await _deliver("discovery", await agent.run(prompt, save=False))


async def improvement() -> bool:
    rows = await db.fetch(
        "select role, content from messages where created_at >= now() - interval '3 days' "
        "and content not like '(mensaje automático%%' order by id"
    )
    mems = await db.fetch(
        "select category, content from memories where active and created_at >= now() - interval '7 days' "
        "and category in ('idea','pensamiento','tarea','minuta','espera','proyecto','objetivo') "
        "order by created_at desc limit 25"
    )
    if not rows and not mems:
        return False
    who = {"user": config.settings.owner_name, "assistant": "Jared"}
    activity = "Conversación:\n" + "\n".join(
        f"{who.get(r['role'], r['role'])}: {r['content'][:500]}" for r in rows)[-15000:]
    if mems:
        activity += "\n\nAnotado en la semana:\n" + "\n".join(f"- [{m['category']}] {m['content'][:300]}" for m in mems)
    prompt = IMPROVE_PROMPT.format(
        name=config.settings.owner_name,
        activity=activity,
        history=_bullets(await _history("improve"), "(nada todavía)"),
        feedback=await _feedback_text(),
    )
    return await _deliver("improve", await agent.run(prompt, save=False, use_history=False, smart=True))


async def record_feedback(item_id: str, ok: bool) -> str | None:
    items: dict = await db.kv_get("proactive_items", {})
    item = items.get(item_id)
    if not item:
        return None
    fb: list = await db.kv_get("proactive_feedback", [])
    fb = [f for f in fb if f.get("id") != item_id]
    fb.append({"id": item_id, "kind": item["kind"], "tema": item["tema"], "ok": ok,
               "fecha": now_local().date().isoformat()})
    await db.kv_set("proactive_feedback", fb[-60:])
    return item["tema"]
