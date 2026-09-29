"""Mantiene despierto el servicio en el plan gratuito de Render durante el horario de mercado.

Render duerme un servicio free tras 15 min sin tráfico entrante; este hilo se llama a sí mismo cada 9 min
de 4:00 a 16:30 ET. Para despertarlo por la mañana está .github/workflows/keepalive.yml.
Con plan Starter (no duerme) no hace falta, pero no estorba.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import requests

from scanner.util import ET, log


def market_window(now: datetime) -> bool:
    t = now.astimezone(ET)
    m = t.hour * 60 + t.minute
    return t.weekday() < 5 and 4 * 60 - 10 <= m <= 16 * 60 + 30


def loop():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return
    while True:
        if market_window(datetime.now(timezone.utc)):
            try:
                requests.get(url.rstrip("/") + "/health", timeout=15)
            except requests.RequestException as e:
                log.warning("keepalive: %s", e)
        time.sleep(540)
