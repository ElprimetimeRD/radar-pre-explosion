"""Servicio web (Render): página del semáforo + API. El bucle corre en un hilo al arrancar."""
from __future__ import annotations

import hmac
import math
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ValidationError

from . import keepalive, memory
from .runner import Radar

memory.limit_arenas(int(os.environ.get("MALLOC_ARENAS", "2")))  # antes de crear hilos
PAGE = os.path.join(os.path.dirname(__file__), "page.html")
radar = Radar(notify=os.environ.get("NO_NOTIFY") != "1")
STARTED = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("NO_LOOP") != "1":
        threading.Thread(target=radar.run_forever, name="radar", daemon=True).start()
        threading.Thread(target=radar.fast_forever, name="vigia-rapido", daemon=True).start()
        threading.Thread(target=radar.context_forever, name="contexto", daemon=True).start()
        threading.Thread(target=keepalive.loop, name="keepalive", daemon=True).start()
    yield


app = FastAPI(title="Radar · Semáforo", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def page():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


def clean(o):
    """NaN/inf → None (JSON estricto no los acepta y la API respondía 500). Copia cada dict/lista de una vez antes de
    recorrerlo: el ciclo puede estar agregando campos a una señal en otro hilo ("dictionary changed size")."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in list(o.items())}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in list(o)]
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
    """Señales del día y compras stop de los avisos ARMA (las guarda GitHub Actions en data/live/ para que sobrevivan
    a los redeploys)."""
    day = radar.day.isoformat() if radar.day else None
    hist = [{"day": d, "trades": radar.history.get(d, []), "armadas": radar.arm_history.get(d, [])}
            for d in sorted(set(radar.history) | set(radar.arm_history))]
    return JSONResponse(clean({"day": day, "trades": list(radar.trades.values()), "stats": radar.stats(),
                               "armadas": list(radar.arms.values()), "armStats": radar.arm_stats(), "history": hist}),
                        headers={"Cache-Control": "no-store"})


def _auth(token: str | None, var: str = "POSITIONS_TOKEN"):
    """Las posiciones y el puente IBKR son privados: exigen su token (cabecera X-Token). Sin la variable configurada,
    el acceso queda cerrado."""
    want = os.environ.get(var) or ""
    if not want:
        raise HTTPException(503, f"{var} no está configurado en el servicio")
    if not token or not hmac.compare_digest(token.encode(), want.encode()):
        raise HTTPException(401, "token inválido")


class PositionIn(BaseModel):
    t: str
    entry: float
    qty: float | None = None
    tp: float | None = None  # objetivo en % (por defecto DEFAULT_TP)


@app.get("/api/positions")
def positions_list(x_token: str | None = Header(default=None)):
    _auth(x_token)
    return JSONResponse(clean({"positions": radar.positions.listing()}), headers={"Cache-Control": "no-store"})


@app.post("/api/positions")
def positions_add(p: PositionIn, x_token: str | None = Header(default=None)):
    """Registra (o actualiza) una posición ya ejecutada para vigilarla: avisa si cae 2 %/3 % o llega al objetivo."""
    _auth(x_token)
    try:
        return clean(radar.positions.upsert(p.t, p.entry, p.qty, p.tp))
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.delete("/api/positions/{t}")
def positions_remove(t: str, x_token: str | None = Header(default=None)):
    _auth(x_token)
    return {"removed": radar.positions.remove(t)}


BRIDGE_MAX = 200_000  # bytes por envío del puente


class BridgeIn(BaseModel):
    v: int = 1
    scan: dict[str, list[str]] | None = None   # {código de escáner de IBKR: [tickers en orden]}
    quotes: dict[str, dict] | None = None      # {ticker: {last, high, bid, ask}} de las acciones armadas
    info: dict | None = None                   # estado del programa (conexión con IBKR, último error)


@app.post("/api/bridge")
async def bridge_feed(request: Request, x_token: str | None = Header(default=None)):
    """Puente IBKR (bridge/puente_ibkr.py en la PC de Priamo): recibe escáneres y precios al instante, avisa rupturas
    y responde qué acciones vigilar. Exige BRIDGE_TOKEN; el cuerpo se lee solo después de validar la clave."""
    _auth(x_token, "BRIDGE_TOKEN")
    try:  # por la cabecera, antes de leer el cuerpo
        if int(request.headers.get("content-length") or 0) > BRIDGE_MAX:
            raise HTTPException(413, "cuerpo demasiado grande")
    except ValueError:
        raise HTTPException(400, "Content-Length inválido")
    raw = await request.body()
    if len(raw) > BRIDGE_MAX:
        raise HTTPException(413, "cuerpo demasiado grande")
    try:
        b = BridgeIn.model_validate_json(raw or b"{}")
    except ValidationError as e:
        raise HTTPException(422, str(e)[:300])
    return clean(await run_in_threadpool(radar.on_bridge, b.scan, b.quotes, b.info))


@app.get("/health")
def health():
    ts = radar.snapshot.get("ts")
    rss = memory.rss_mb()
    return clean({"ok": True, "status": radar.snapshot.get("status"), "last_cycle": ts, "fast": radar.fast_state,
            "armed": sorted(radar.armed), "uptime_min": round((time.time() - STARTED) / 60, 1),
            "cycle_s": radar.cycle_s, "context": {"on": radar.bg, "queue": len(radar.enrich_q),
                                                  "halts_age_s": round(time.time() - radar.halts_ts) if radar.halts_ts else None},
            "mem": {"rss_mb": rss, "pct": memory.pct(rss), "limit_mb": memory.LIMIT_MB}, "bridge": radar.bridge.status(),
            "telegram": {"configured": bool(radar.notify and os.environ.get("TELEGRAM_BOT_TOKEN")
                                            and os.environ.get("TELEGRAM_CHAT_ID")), **radar.tg_state},
            "now": datetime.now(timezone.utc).isoformat()})


@app.get("/api/news/{t}")
def news_check(t: str, x_token: str | None = Header(default=None)):
    """Diagnóstico: qué devuelve cada fuente de titulares desde este servidor. Con el token de posiciones: cada llamada
    hace varias peticiones externas de hasta 10 s, y abierta servía para saturar el servicio."""
    _auth(x_token)
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
