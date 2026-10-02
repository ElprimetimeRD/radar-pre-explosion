"""Diagnóstico de retraso del semáforo (se corre en GitHub Actions, rama diag-velas; no toca el servicio).

Para cada señal del registro (COMPRA y ARMA) y para las acciones que más subieron hoy, baja velas de 1 min de Yahoo
y mide: cuándo empezó el movimiento, cuándo rompió el nivel, cuándo avisó el semáforo, cuánto del movimiento ya había
pasado y qué habría dado entrar antes. Escribe data/diag/latencia.json y data/diag/latencia.md."""
from __future__ import annotations

import glob
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf

ET = ZoneInfo("America/New_York")
URL = os.environ.get("URL", "https://radar-semaforo.onrender.com")
OUT = "data/diag"
OPEN_M, OR_END, CLOSE_M = 570, 575, 960


def hm(m):
    return None if m is None else f"{int(m) // 60:02d}:{int(m) % 60:02d}"


def mins(s):
    h, m = s.split(":")[:2]
    return int(h) * 60 + int(m)


def to_et(df):
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.dropna(subset=["Close"]).copy()
    idx = d.index if getattr(d.index, "tz", None) is not None else d.index.tz_localize("UTC")
    d.index = idx.tz_convert(ET)
    d["m"] = d.index.hour * 60 + d.index.minute
    d["day"] = d.index.date.astype(str) if hasattr(d.index.date, "astype") else [x.isoformat() for x in d.index.date]
    d["Volume"] = d["Volume"].fillna(0)
    return d


def first_touch(post, up, down):
    """Qué toca primero en las velas `post`: 'up' (objetivo), 'down' (stop) o None. Si una vela toca los dos, stop."""
    for _, b in post.iterrows():
        if b["Low"] <= down:
            return "stop", int(b["m"])
        if b["High"] >= up:
            return "+2%", int(b["m"])
    return None, None


def vwap(reg):
    tp = (reg["High"] + reg["Low"] + reg["Close"]) / 3
    v = reg["Volume"]
    return (tp * v).cumsum() / v.cumsum().replace(0, np.nan)


def ignition(reg, lo_m, hi_m):
    """Primer minuto entre lo_m y hi_m con vela compradora de volumen ≥ 3× la mediana de los 20 previos y sobre VWAP."""
    v = reg["Volume"]
    med = v.shift(1).rolling(20, min_periods=5).median()
    vw = vwap(reg)
    ok = (v >= 3 * med) & (reg["Close"] > reg["Open"]) & (reg["Close"] > vw) & (reg["m"] >= lo_m) & (reg["m"] < hi_m)
    hit = reg[ok]
    return (int(hit["m"].iloc[0]), float(hit["Close"].iloc[0])) if not hit.empty else (None, None)


def load_signals():
    sigs = []
    for f in sorted(glob.glob("data/live/2*.json")):
        j = json.load(open(f))
        day = j.get("day") or os.path.basename(f)[:10]
        for kind, key in (("COMPRA", "trades"), ("ARMA", "armadas")):
            for x in j.get(key) or []:
                sigs.append({**x, "day": x.get("day") or day, "kind": kind})
    try:  # lo más nuevo de hoy, directo del servicio
        j = requests.get(f"{URL}/api/trades", timeout=90).json()
        day = j.get("day")
        if day:
            sigs = [s for s in sigs if s["day"] != day]
            for kind, key in (("COMPRA", "trades"), ("ARMA", "armadas")):
                for x in j.get(key) or []:
                    sigs.append({**x, "day": x.get("day") or day, "kind": kind})
    except Exception as e:  # noqa: BLE001
        print("sin /api/trades:", e)
    return sigs


def movers():
    out = {}
    for name in ("day_gainers", "small_cap_gainers", "most_actives"):
        try:
            r = yf.screen(name, count=100)
        except Exception as e:  # noqa: BLE001
            print("screen", name, e)
            continue
        for q in (r or {}).get("quotes", []):
            s, chg = q.get("symbol"), q.get("regularMarketChangePercent") or 0
            px, vol = q.get("regularMarketPrice") or 0, q.get("regularMarketVolume") or 0
            if s and chg >= 5 and px >= 1 and vol * px >= 3e6:
                out[s] = {"chg": round(chg, 1), "px": px, "usd_vol": round(vol * px / 1e6, 1)}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["chg"])[:30])


def main():
    os.makedirs(OUT, exist_ok=True)
    sigs = load_signals()
    mov = movers()
    try:
        snap = requests.get(f"{URL}/api/signals", timeout=90).json()
    except Exception:  # noqa: BLE001
        snap = {}
    rows = {r["t"]: r for r in snap.get("rows") or []}
    syms = sorted({s["t"] for s in sigs} | set(mov))
    raw = yf.download(syms, period="7d", interval="1m", prepost=True, group_by="ticker", auto_adjust=True,
                      threads=True, progress=False)
    bars = {}
    for s in syms:
        try:
            df = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
            bars[s] = to_et(df.dropna(how="all"))
        except KeyError:
            pass

    res = []
    for sg in sigs:
        d = bars.get(sg["t"])
        if d is None or d.empty or not sg.get("time"):
            continue
        day = d[d["day"] == sg["day"]]
        reg = day[(day["m"] >= OPEN_M) & (day["m"] < CLOSE_M)].copy()
        if reg.empty:
            continue
        sm = mins(sg["time"])
        entry = float(sg.get("fill") or sg["entry"])
        stop = float(sg["stop"])
        pre = reg[reg["m"] < sm]
        post = reg[reg["m"] > sm]
        win = pre[pre["m"] >= sm - 60]
        r = {"day": sg["day"], "t": sg["t"], "kind": sg["kind"], "time": sg["time"], "entry": entry,
             "status": sg.get("status"), "limit": sg.get("limit"), "score": sg.get("score")}
        if not win.empty:
            lo_i = win["Low"].idxmin()
            lo, lo_m = float(win.loc[lo_i, "Low"]), int(win.loc[lo_i, "m"])
            r["low_before"], r["low_m"] = round(lo, 4), hm(lo_m)
            r["run_before_pct"] = round(100 * (entry / lo - 1), 2)
            r["min_since_low"] = sm - lo_m
            ig_m, ig_px = ignition(reg, lo_m, sm)
            if ig_m is not None:
                r["ignition"], r["min_after_ignition"] = hm(ig_m), sm - ig_m
                r["entry_vs_ignition_pct"] = round(100 * (entry / ig_px - 1), 2)
        lvl = sg.get("level")
        if lvl:
            brk = pre[(pre["m"] >= OR_END) & (pre["m"] >= sm - 90) & (pre["High"] > float(lvl) * 1.0005)]
            if not brk.empty:
                r["break_m"] = hm(int(brk["m"].iloc[0]))
                r["min_after_break"] = sm - int(brk["m"].iloc[0])
        p60 = post[post["m"] <= sm + 60]
        if not p60.empty:
            hi = float(p60["High"].max())
            r["max_60m_pct"] = round(100 * (hi / entry - 1), 2)
            r["min_60m_pct"] = round(100 * (float(p60["Low"].min()) / entry - 1), 2)
            if "low_before" in r and hi > r["low_before"]:
                r["move_done_pct"] = round(100 * (entry - r["low_before"]) / (hi - r["low_before"]), 0)
        if sg["kind"] == "ARMA":
            fill_m = sg.get("fill_m")
            p = reg[reg["m"] > (fill_m or sm)] if fill_m else post
        else:
            p = post
        r["first_touch"], ft_m = first_touch(p, entry * 1.02, stop)
        r["first_touch_m"] = hm(ft_m)
        # ¿Y si hubiera entrado al romper el nivel (compra stop, como el ARMA)?
        if lvl and r.get("break_m"):
            e2 = float(lvl) * 1.001
            p2 = reg[reg["m"] > mins(r["break_m"])]
            r["at_break_entry"] = round(e2, 4)
            r["at_break_touch"], _ = first_touch(p2, e2 * 1.02, stop)
        res.append(r)

    mv = []
    for s, q in mov.items():
        d = bars.get(s)
        if d is None or d.empty:
            continue
        today = d["day"].max()
        day = d[d["day"] == today]
        reg = day[(day["m"] >= OPEN_M) & (day["m"] < CLOSE_M)]
        if len(reg) < 10:
            continue
        o = float(reg["Open"].iloc[0])
        hod_i = reg["High"].idxmax()
        orh = float(reg[reg["m"] < OR_END]["High"].max())
        brk = reg[(reg["m"] >= OR_END) & (reg["High"] > orh * 1.0005)]
        my = [x for x in sigs if x["t"] == s and x["day"] == today]
        first = min(my, key=lambda x: mins(x["time"])) if my else None
        row = rows.get(s)
        m = {"t": s, "chg": q["chg"], "usd_vol_m": q["usd_vol"], "open": round(o, 4), "hod": round(float(reg.loc[hod_i, "High"]), 4),
             "hod_m": hm(int(reg.loc[hod_i, "m"])), "open_to_hod_pct": round(100 * (float(reg.loc[hod_i, "High"]) / o - 1), 1),
             "or_break": hm(int(brk["m"].iloc[0])) if not brk.empty else None,
             "radar_first": f"{first['kind']} {first['time']}" if first else None,
             "now_decision": row.get("decision") if row else "fuera del universo",
             "now_reason": row.get("reason") if row else None}
        ig_m, _ = ignition(reg, OR_END, CLOSE_M)
        m["ignition"] = hm(ig_m)
        if first:
            e = float(first.get("entry"))
            m["left_after_alert_pct"] = round(100 * (m["hod"] / e - 1), 1)
        mv.append(m)

    json.dump({"generated": datetime.now(ET).isoformat(), "signals": res, "movers": mv}, open(f"{OUT}/latencia.json", "w"),
              indent=1, default=str)
    df = pd.DataFrame(res)
    lines = [f"# Diagnóstico de retraso · {datetime.now(ET):%Y-%m-%d %H:%M} ET", ""]
    if not df.empty:
        lines.append("## Señales")
        cols = [c for c in ["day", "t", "kind", "time", "status", "low_m", "ignition", "break_m", "min_after_ignition",
                            "min_after_break", "run_before_pct", "move_done_pct", "max_60m_pct", "min_60m_pct",
                            "first_touch", "at_break_touch"] if c in df.columns]
        lines.append(df[cols].to_markdown(index=False))
    if mv:
        lines += ["", "## Las que más subieron hoy", pd.DataFrame(mv).to_markdown(index=False)]
    open(f"{OUT}/latencia.md", "w").write("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
