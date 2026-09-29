from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

log = logging.getLogger("radar")
if not log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def now_et() -> datetime:
    return datetime.now(timezone.utc).astimezone(ET)


def session_phase(t: datetime | None = None) -> str:
    t = t or now_et()
    if t.weekday() >= 5:
        return "cerrado"
    m = t.hour * 60 + t.minute
    if 4 * 60 <= m < 9 * 60 + 30:
        return "pre-market"
    if 9 * 60 + 30 <= m < 16 * 60:
        return "sesión"
    if 16 * 60 <= m < 20 * 60:
        return "after-hours"
    return "cerrado"


def retry(fn, tries=3, base=1.5, what=""):
    """Reintenta con backoff; devuelve None si todo falla (nunca rompe el escaneo)."""
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            msg = str(e)[:160]
            if i == tries - 1:
                log.warning("falló %s: %s", what, msg)
                return None
            wait = base * (2 ** i) + random.random()
            if "Rate" in type(e).__name__ or "429" in msg or "Too Many" in msg:
                wait += 10
            time.sleep(wait)
    return None


def fnum(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def rnd(x, d=2):
    return None if x is None else round(float(x), d)


def write_json(path, obj, compact=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default
