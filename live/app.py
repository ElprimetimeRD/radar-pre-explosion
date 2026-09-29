"""Servicio web (Render): página del semáforo + API. El bucle corre en un hilo al arrancar."""
from __future__ import annotations

import math
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from . import keepalive
from .runner import Radar

PAGE = os.path.join(os.path.dirname(__file__), "page.html")
radar = Radar(notify=os.environ.get("NO_NOTIFY") != "1")
STARTED = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("NO_LOOP") != "1":
        threading.Thread(target=radar.run_forever, name="radar", daemon=True).start()
        threading.Thread(target=keepalive.loop, name="keepalive", daemon=True).start()
    yield


app = FastAPI(title="Radar · Semáforo", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def page():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


def clean(o):
    """NaN/inf → None (JSON estricto no los acepta y la API respondía 500)."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


@app.get("/api/signals")
def signals():
    return JSONResponse(clean(radar.snapshot), headers={"Cache-Control": "no-store"})


@app.get("/api/replay")
def replay(step: int = 10, fresh: int = 0):
    """Reproduce la sesión de hoy con las reglas actuales (en segundo plano). Guarda el último resultado; ?fresh=1 lo recalcula."""
    return JSONResponse(clean(radar.replay_async(max(5, min(step, 30)), bool(fresh))), headers={"Cache-Control": "no-store"})


@app.get("/api/trades")
def trades():
    """Señales del día (las guarda GitHub Actions en data/live/ para que sobrevivan a los redeploys)."""
    day = radar.day.isoformat() if radar.day else None
    return JSONResponse(clean({"day": day, "trades": list(radar.trades.values()), "stats": radar.stats()}),
                        headers={"Cache-Control": "no-store"})


@app.get("/health")
def health():
    ts = radar.snapshot.get("ts")
    return {"ok": True, "status": radar.snapshot.get("status"), "last_cycle": ts,
            "uptime_min": round((time.time() - STARTED) / 60, 1), "now": datetime.now(timezone.utc).isoformat()}


@app.get("/api/news/{t}")
def news_check(t: str):
    """Diagnóstico: qué devuelve cada fuente de titulares desde este servidor."""
    import requests
    from scanner.sources import yahoo
    from .runner import rss_news
    out = {"yahoo_get_news": len(yahoo.news(t.upper(), count=10) or [])}
    try:
        r = requests.get("https://feeds.finance.yahoo.com/rss/2.0/headline",
                         params={"s": t.upper(), "region": "US", "lang": "en-US"}, timeout=10)
        out["rss_status"], out["rss_head"] = r.status_code, r.text[:160]
    except requests.RequestException as e:
        out["rss_error"] = str(e)[:160]
    out["rss_items"] = [x["title"] for x in rss_news(t.upper())[:3]]
    out["google_items"] = [x["title"] for x in rss_news(t.upper(), t.upper())[:3]] if not out["rss_items"] else []
    return out
