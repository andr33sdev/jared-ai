"""Conversión de Markdown simple al HTML que acepta Telegram."""
from __future__ import annotations

import html
import re


def md_to_tg_html(text: str) -> str:
    parts = re.split(r"(```.*?```)", text, flags=re.S)
    out: list[str] = []
    for part in parts:
        if part.startswith("```") and part.endswith("```") and len(part) >= 6:
            inner = re.sub(r"^[\w+-]*\n", "", part[3:-3])
            out.append(f"<pre>{html.escape(inner, quote=False)}</pre>")
            continue
        s = html.escape(part, quote=False)
        s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s, flags=re.S)
        s = re.sub(r"(?<![\w*])\*(?![\s*])([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", s)
        s = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
        s = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", s, flags=re.M)
        out.append(s)
    return "".join(out)


def split_text(text: str, limit: int = 3800) -> list[str]:
    """Parte un texto largo en trozos aptos para Telegram (máx. 4096)."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    current = ""
    for para in text.split("\n"):
        while len(para) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(para[:limit])
            para = para[limit:]
        candidate = f"{current}\n{para}" if current else para
        if len(candidate) > limit:
            chunks.append(current)
            current = para
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    return chunks


def plain_for_speech(text: str) -> str:
    """Limpia el texto para pasarlo a voz."""
    s = re.sub(r"```.*?```", " ", text, flags=re.S)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"[*_`#>]", "", s)
    s = re.sub(r"\s+\n", "\n", s)
    return s.strip()[:2500]
