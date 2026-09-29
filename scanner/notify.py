"""Alerta opcional por Telegram (secretos TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID)."""
from __future__ import annotations

import os

import requests

from .util import log

NAME = {"trigger": "TRIGGER", "alert": "ALERT", "watch": "WATCH"}


def format_message(doc: dict, limit=8) -> str | None:
    top = [r for r in doc["rows"] if r["tier"] in ("trigger", "alert", "watch")][:limit]
    hz = "Intradía" if doc["H"] == "A" else "Swing 1–20 d"
    head = f"Radar Pre-Explosión · {hz} · régimen {doc.get('regime')}\n{doc['evaluated']} evaluadas de {doc['universe']}"
    if not top:
        return head + "\nSin WATCH, ALERT ni TRIGGER en este escaneo."
    lines = [head]
    for r in top:
        fam = ", ".join(r["firing"][:3]) or "—"
        extra = f" · gap {r['gap']:+.1f}%" if doc["H"] == "A" and r.get("gap") is not None else ""
        risk = f" · riesgo: {r['flags'][0]}" if r["flags"] else ""
        lines.append(f"[{NAME[r['tier']]}] {r['t']} {r['score']:.0f}/100 · cob {round(100 * r['cov'])}%{extra} · {fam}{risk}")
    return "\n".join(lines)


def telegram(doc: dict):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    msg = format_message(doc)
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": msg}, timeout=20)
    except requests.RequestException as e:
        log.warning("Telegram falló: %s", e)
