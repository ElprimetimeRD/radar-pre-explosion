"""Simula el semáforo minuto a minuto sobre las velas reales de hoy (data/diag/bars_1m.csv.gz) para medir de dónde
viene el retraso: el dato (Yahoo 1–2 min atrás) o las reglas. Escenarios con las reglas de producción y variantes.
Uso: python -m tools.sim_semaforo  →  data/diag/sim.json"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from multiprocessing import Pool
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

os.environ.setdefault("NO_LOOP", "1")
from live import decide as D  # noqa: E402
from live import metrics as M  # noqa: E402

ET = ZoneInfo("America/New_York")
TODAY = date.fromisoformat(os.environ.get("SIM_DAY", "2026-10-02"))
START, END = 9 * 60 + 31, int(os.environ.get("SIM_END", str(13 * 60 + 40)))
ARM_MIN, RISK_MAX = 55, D.RISK_MAX
D.ENTRY_END_M = 930  # producción: COMPRA y ARMA hasta las 15:30 (ENTRY_END_M en Render)

B = pd.read_csv("data/diag/bars_1m.csv.gz")
DAILY = pd.read_csv("data/diag/daily.csv.gz")
FR, PREV, ATR, BASE = {}, {}, {}, {}
for t, g in B.groupby("t"):
    df = g.set_index(pd.to_datetime(g["ts"], unit="s", utc=True))[["Open", "High", "Low", "Close", "Volume"]].sort_index()
    d = M.to_et(df)
    FR[t] = d[d["day"] == TODAY]
    BASE[t] = M.baseline_curve(df, TODAY)
    dd = DAILY[(DAILY["t"] == t) & (DAILY["date"] < TODAY.isoformat())].dropna(subset=["Close"])
    PREV[t] = float(dd["Close"].iloc[-1]) if not dd.empty else None
    ATR[t] = M.atr_pct(dd.set_index("date")) if not dd.empty else None


def at(t, m, lag):
    """Velas que el semáforo tiene a las m:30 con `lag` min de retraso, y el precio en vivo (cotización ≈ al instante)."""
    d = FR.get(t)
    if d is None or d.empty:
        return None, None
    avail = d[d["m"] <= m - 1 - lag]
    last = d[d["m"] <= m - 1]
    return avail, (float(last["Close"].iloc[-1]) if not last.empty else None)


def mets(t, m, lag):
    avail, px = at(t, m, lag)
    if avail is None:
        return {}
    now = datetime(TODAY.year, TODAY.month, TODAY.day, m // 60, m % 60, 30, tzinfo=ET)
    return M.session_metrics(avail, PREV.get(t), BASE.get(t), now, px)


REG = {}


def regime_at(m, lag):
    k = (m, lag)
    if k not in REG:
        spy, qqq = mets("SPY", m, lag), mets("QQQ", m, lag)
        REG[k] = (D.regime(spy, qqq)[0], spy.get("chg"))
    return REG[k]


def first_touch(t, m0, entry, stop, cap=None, order=None):
    """Desde la vela siguiente a m0: llenado (compra stop) y qué toca primero, +2 % o el stop. Si una vela toca los
    dos, cuenta stop."""
    d = FR[t]
    post = d[(d["m"] > m0) & (d["m"] < 960)]
    fill_m = None
    if order == "stop":
        for _, b in post.iterrows():
            if b["Low"] <= stop and b["High"] < entry:
                return {"fill": None, "res": "cancelada (perdió el stop antes)"}
            if b["High"] >= entry:
                if b["Open"] > cap and b["Low"] > cap:
                    return {"fill": None, "res": "saltó el límite"}
                fill_m, entry = int(b["m"]), min(max(entry, float(b["Open"])), cap)
                break
        if fill_m is None:
            return {"fill": None, "res": "no se activó"}
        post = d[(d["m"] > fill_m) & (d["m"] < 960)]
    for _, b in post.iterrows():
        if b["Low"] <= stop:
            return {"fill": fill_m, "res": "stop", "touch_m": int(b["m"])}
        if b["High"] >= entry * 1.02:
            return {"fill": fill_m, "res": "+2%", "touch_m": int(b["m"])}
    last = float(d["Close"].iloc[-1])
    return {"fill": fill_m, "res": f"abierta {100 * (last / entry - 1):+.1f}%"}


def run(args):
    t, sc = args
    lag, near, no_amber, rv15 = sc["lag"], sc.get("near", 1.0), sc.get("no_amber", False), sc.get("rv15", 0.0)
    D.RVOL15_IN_PLAY = rv15
    out = {"t": t, "compra": None, "arma": None, "reasons": {}, "dec": {}}
    for m in range(START, END + 1):
        mm = mets(t, m, lag)
        if not mm:
            continue
        reg, spy_chg = regime_at(m, lag)
        if no_amber and reg == "amarillo":
            reg = "verde"
        ctx = {"phase": "open", "regime": reg, "spy_chg": spy_chg, "spread": None, "atr": ATR.get(t), "halted": None,
               "name": t, "watch": False}
        r = D.decide(t, mm, ctx)
        key = (r["reason"] or "").split("(")[0].split(":")[0].strip()[:40]
        out["dec"][m] = f'{r["decision"]} · {key}'  # decisión de cada minuto (para ver qué la frenó en el arranque)
        if out["compra"] is None:
            out["reasons"][f'{r["decision"]} · {key}'] = out["reasons"].get(f'{r["decision"]} · {key}', 0) + 1
        need = ARM_MIN + (5 if reg == "amarillo" else 0)
        p, lvl, px = r.get("plan"), r.get("level"), r.get("px")
        if (out["arma"] is None and r["decision"] == "ESPERA" and lvl and p and px and reg != "rojo"
                and (p.get("risk") or 99) <= RISK_MAX and r["score"] >= need and px >= lvl * (1 - near / 100)):
            e = p["entry"]
            out["arma"] = {"m": m, "entry": e, "stop": p["stop"], "score": r["score"],
                           **first_touch(t, m, e, p["stop"], round(e * 1.003, 4), "stop")}
        if out["compra"] is None and r["decision"] == "COMPRA":
            p = r["plan"]
            out["compra"] = {"m": m, "entry": p["entry"], "stop": p["stop"], "limit": p.get("limit"), "score": r["score"],
                             "reason": r["reason"], **first_touch(t, m, p["entry"], p["stop"])}
    return out


def ignition(t):
    d = FR.get(t)
    reg = d[(d["m"] >= 570) & (d["m"] < 960)] if d is not None else None
    if reg is None or len(reg) < 10:
        return {}
    v = reg["Volume"]
    med = v.shift(1).rolling(20, min_periods=5).median()
    tp = (reg["High"] + reg["Low"] + reg["Close"]) / 3
    vw = (tp * v).cumsum() / v.cumsum().replace(0, np.nan)
    ok = (v >= 3 * med) & (reg["Close"] > reg["Open"]) & (reg["Close"] > vw) & (reg["m"] >= 575)
    hit = reg[ok]
    orh = float(reg[reg["m"] < 575]["High"].max())
    brk = reg[(reg["m"] >= 575) & (reg["High"] > orh * 1.0005)]
    hod_i = reg["High"].idxmax()
    o = float(reg["Open"].iloc[0])
    return {"ign": int(hit["m"].iloc[0]) if not hit.empty else None,
            "or_break": int(brk["m"].iloc[0]) if not brk.empty else None,
            "hod_m": int(reg.loc[hod_i, "m"]), "open_to_hod": round(100 * (float(reg.loc[hod_i, "High"]) / o - 1), 1)}


SCEN = {
    "produccion (Yahoo 2 min)": {"lag": 2},
    "datos al instante (IBKR/SIP)": {"lag": 0},
    "Yahoo + armar a 2.5 % del gatillo": {"lag": 2, "near": 2.5},
    "Yahoo + sin castigo de mercado amarillo": {"lag": 2, "no_amber": True},
    "Yahoo + RVOL 15 min (3x) pone en juego": {"lag": 2, "rv15": 3.0},
}

if __name__ == "__main__":
    j = json.load(open("data/diag/latencia.json"))
    movers = [x["t"] for x in j["movers"]]
    univ = sorted(set(FR) - {"SPY", "QQQ"})
    names = sys.argv[1:] or list(SCEN)
    res = {"movers": {t: ignition(t) for t in movers}, "scen": {}}
    with Pool(2) as pool:
        for name in names:
            res["scen"][name] = pool.map(run, [(t, SCEN[name]) for t in univ])
            print("listo:", name, flush=True)
    json.dump(res, open("data/diag/sim.json", "w"), indent=1, default=str)
