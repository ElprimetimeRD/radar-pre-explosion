"""Simulación del semáforo en un día del historial (data/diag/ds_*.gz), por escenarios. Se corre en GitHub Actions,
un trabajo por día: python -m tools.sim_hist 2026-09-15  →  out/sim-2026-09-15.json"""
from __future__ import annotations

import gzip
import json
import os
import sys
from array import array
from datetime import date, datetime, timedelta
from multiprocessing import Pool
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

os.environ.setdefault("NO_LOOP", "1")
os.environ.setdefault("RIESGO", "normal")
from live import decide as D  # noqa: E402
from live import metrics as M  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY = date.fromisoformat(sys.argv[1])
START, END = 9 * 60 + 31, 15 * 60 + 30
D.ENTRY_END_M = 930
NORMAL = {k: getattr(D, k) for k in ("RVOL_IN_PLAY", "RVOL_NEWS", "BUY_MIN", "CHASE_MAX", "EXT_MAX", "RISK_MAX")}
ALTO = {"RVOL_IN_PLAY": 1.5, "RVOL_NEWS": 1.2, "BUY_MIN": 55, "CHASE_MAX": 1.0, "EXT_MAX": 5.0, "RISK_MAX": 3.0}
SCEN = {
    "normal": {"lag": 2, "near": 1.0, "arm_min": 55, "rv15": 0.0, "D": {}},
    "alto": {"lag": 2, "near": 2.5, "arm_min": 50, "rv15": 3.0, "D": ALTO},
    "alto sin perseguir": {"lag": 2, "near": 2.5, "arm_min": 50, "rv15": 3.0,
                           "D": {k: v for k, v in ALTO.items() if k not in ("CHASE_MAX", "EXT_MAX")}},
    "alto + datos al instante": {"lag": 0, "near": 2.5, "arm_min": 50, "rv15": 3.0, "D": ALTO},
}

META = [x for x in json.load(gzip.open("data/diag/ds_meta.json.gz", "rt")) if x["day"] == DAY.isoformat()]
B = pd.read_csv("data/diag/ds_bars.csv.gz")
B = B[B["day"] == DAY.isoformat()]
FR, INFO = {}, {}
for x in META:
    g = B[B["t"] == x["t"]].sort_values("m")
    if g.empty:
        continue
    idx = pd.DatetimeIndex([datetime(DAY.year, DAY.month, DAY.day, tzinfo=ET) + timedelta(minutes=int(m)) for m in g["m"]])
    df = pd.DataFrame({"Open": g["o"].values, "High": g["h"].values, "Low": g["l"].values, "Close": g["c"].values,
                       "Volume": g["v"].values.astype(float)}, index=idx)
    df["m"] = g["m"].values
    df["day"] = DAY
    FR[x["t"]] = df
    INFO[x["t"]] = {"role": x["role"], "prev": x["prev"], "atr": x["atr"], "base": array("d", x["base"])}


def mets(t, m, lag):
    d = FR[t]
    avail = d[d["m"] <= m - 1 - lag]
    last = d[d["m"] <= m - 1]
    px = float(last["Close"].iloc[-1]) if not last.empty else None
    now = datetime(DAY.year, DAY.month, DAY.day, m // 60, m % 60, 30, tzinfo=ET)
    i = INFO[t]
    return M.session_metrics(avail, i["prev"], i["base"], now, px)


REG = {}


def regime_at(m, lag):
    if (m, lag) not in REG:
        spy, qqq = (mets(s, m, lag) if s in FR else {} for s in ("SPY", "QQQ"))
        REG[(m, lag)] = (D.regime(spy, qqq)[0], spy.get("chg"))
    return REG[(m, lag)]


def outcome(t, m0, entry, stop, order=None):
    d = FR[t]
    post = d[(d["m"] > m0) & (d["m"] < 960)]
    fill_m = None
    if order == "stop":
        cap = entry * 1.003
        for _, b in post.iterrows():
            if b["Low"] <= stop and b["High"] < entry:
                return {"fill": None, "res": "cancelada"}
            if b["High"] >= entry:
                if b["Open"] > cap and b["Low"] > cap:
                    return {"fill": None, "res": "saltó"}
                fill_m, entry = int(b["m"]), min(max(entry, float(b["Open"])), cap)
                break
        if fill_m is None:
            return {"fill": None, "res": "no activó"}
        post = d[(d["m"] > fill_m) & (d["m"] < 960)]
    risk = 100 * (1 - stop / entry)
    for _, b in post.iterrows():
        if b["Low"] <= stop:
            return {"fill": fill_m, "res": "stop", "pct": round(-risk, 2), "touch": int(b["m"])}
        if b["High"] >= entry * 1.02:
            return {"fill": fill_m, "res": "+2%", "pct": 2.0, "touch": int(b["m"])}
    last = float(d["Close"].iloc[-1])
    return {"fill": fill_m, "res": "cierre", "pct": round(100 * (last / entry - 1), 2)}


def run(args):
    t, name = args
    sc = SCEN[name]
    for k, v in NORMAL.items():
        setattr(D, k, sc["D"].get(k, v))
    D.RVOL15_IN_PLAY = sc["rv15"]
    out = {"t": t, "compra": None, "arma": None}
    for m in range(START, END + 1):
        mm = mets(t, m, sc["lag"])
        if not mm.get("px"):
            continue
        reg, spy_chg = regime_at(m, sc["lag"])
        ctx = {"phase": "open", "regime": reg, "spy_chg": spy_chg, "spread": None, "atr": INFO[t]["atr"],
               "halted": None, "name": t, "watch": False}
        r = D.decide(t, mm, ctx)
        p, lvl, px = r.get("plan"), r.get("level"), r.get("px")
        need = sc["arm_min"] + (5 if reg == "amarillo" else 0)
        if (out["arma"] is None and r["decision"] == "ESPERA" and lvl and p and px and reg != "rojo"
                and (p.get("risk") or 99) <= D.RISK_MAX and r["score"] >= need and px >= lvl * (1 - sc["near"] / 100)):
            out["arma"] = {"m": m, "entry": p["entry"], "stop": p["stop"], **outcome(t, m, p["entry"], p["stop"], "stop")}
        if out["compra"] is None and r["decision"] == "COMPRA":
            p = r["plan"]
            out["compra"] = {"m": m, "entry": p["entry"], "stop": p["stop"], "limit": bool(p.get("limit")),
                             **outcome(t, m, p["entry"], p["stop"])}
        if out["compra"] and out["arma"]:
            break
    return out


def move(t):
    d = FR[t]
    v = d["Volume"]
    med = v.shift(1).rolling(20, min_periods=5).median()
    tp = (d["High"] + d["Low"] + d["Close"]) / 3
    vw = (tp * v).cumsum() / v.cumsum().replace(0, np.nan)
    ok = (v >= 3 * med) & (d["Close"] > d["Open"]) & (d["Close"] > vw) & (d["m"] >= 575)
    hit = d[ok]
    first = d[d["m"] < 575]
    orh = float(first["High"].max()) if not first.empty else float(d["High"].iloc[0])
    brk = d[(d["m"] >= 575) & (d["High"] > orh * 1.0005)]
    hod_i = d["High"].values.argmax()
    o = float(d["Open"].iloc[0])
    return {"ign": int(hit["m"].iloc[0]) if not hit.empty else None, "or_break": int(brk["m"].iloc[0]) if not brk.empty else None,
            "hod_m": int(d["m"].iloc[hod_i]), "hod": float(d["High"].iloc[hod_i]), "open": o,
            "open_to_hod": round(100 * (float(d["High"].iloc[hod_i]) / o - 1), 2)}


if __name__ == "__main__":
    ticks = [t for t in FR if INFO[t]["role"] != "regimen"]
    res = {"day": DAY.isoformat(), "info": {t: {"role": INFO[t]["role"], **move(t)} for t in ticks}, "scen": {}}
    with Pool(os.cpu_count() or 2) as pool:
        for name in SCEN:
            res["scen"][name] = pool.map(run, [(t, name) for t in ticks])
            print(DAY, "listo:", name, flush=True)
    os.makedirs("out", exist_ok=True)
    json.dump(res, open(f"out/sim-{DAY.isoformat()}.json", "w"), default=str)
