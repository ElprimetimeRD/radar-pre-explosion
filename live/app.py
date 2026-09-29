"""Servicio web (Render): página del semáforo + API. El bucle corre en un hilo al arrancar."""
from __future__ import annotations

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


@app.get("/api/signals")
def signals():
    return JSONResponse(radar.snapshot, headers={"Cache-Control": "no-store"})


@app.get("/health")
def health():
    ts = radar.snapshot.get("ts")
    return {"ok": True, "status": radar.snapshot.get("status"), "last_cycle": ts,
            "uptime_min": round((time.time() - STARTED) / 60, 1), "now": datetime.now(timezone.utc).isoformat()}
