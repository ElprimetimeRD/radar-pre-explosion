"""Métricas intradía a partir de velas de 1 minuto (funciones puras, sin red).

Todas las horas en Nueva York (ET). La sesión regular va de 9:30 (minuto 570) a 16:00 (minuto 960).
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from scanner.util import ET

OPEN_M, CLOSE_M = 570, 960
SESSION_LEN = CLOSE_M - OPEN_M  # 390

# Parámetros de ballena (vela de 1 min anómala)
WHALE_K = 5.0            # volumen ≥ K × mediana de los 30 minutos previos
WHALE_MIN_USD = 150_000  # y al menos este monto en dólares en ese minuto
WHALE_WINDOW = 15        # minutos hacia atrás que cuentan como "reciente"
OR_MINUTES = 15          # rango de apertura (9:30–9:45)


def to_et(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.dropna(subset=["Close"]).copy()
    idx = d.index
    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize("UTC")
    d.index = idx.tz_convert(ET)
    d["m"] = d.index.hour * 60 + d.index.minute
    d["day"] = d.index.date
    d["Volume"] = d["Volume"].fillna(0)
    return d


def baseline_curve(df5d: pd.DataFrame, today) -> list[float] | None:
    """Volumen acumulado promedio por minuto de sesión (0..389) de los días previos a `today`."""
    d = to_et(df5d)
    if d.empty:
        return None
    d = d[(d["day"] < today) & (d["m"] >= OPEN_M) & (d["m"] < CLOSE_M)]
    curves = []
    for _, g in d.groupby("day"):
        s = g.groupby(g["m"] - OPEN_M)["Volume"].sum().reindex(range(SESSION_LEN), fill_value=0).cumsum()
        if s.iloc[-1] > 0:
            curves.append(s.values)
    if not curves:
        return None
    return [float(x) for x in np.mean(curves, axis=0)]


def atr_pct(daily: pd.DataFrame, n=14) -> float | None:
    if daily is None or daily.empty or len(daily) < n + 1:
        return None
    d = daily.dropna(subset=["Close"])
    c, h, l = d["Close"], d["High"], d["Low"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    last = float(c.iloc[-1])
    return round(100 * float(tr.tail(n).mean()) / last, 2) if last > 0 else None


def whale_bars(reg: pd.DataFrame, now_m: int) -> dict:
    """Velas de 1 min con volumen anómalo en los últimos WHALE_WINDOW minutos, clasificadas por lado."""
    out = {"buy": 0, "sell": 0, "absorb": 0, "usd": 0.0, "last": None}
    if len(reg) < 12:
        return out
    v = reg["Volume"]
    med = v.shift(1).rolling(30, min_periods=10).median()
    usd = v * reg["Close"]
    hit = (v >= WHALE_K * med) & (usd >= WHALE_MIN_USD) & (med > 0)
    rec = reg[hit & (reg["m"] >= now_m - WHALE_WINDOW)]
    for ts, r in rec.iterrows():
        rng = r["High"] - r["Low"]
        pos = (r["Close"] - r["Low"]) / rng if rng > 0 else 0.5
        if pos >= 0.6 and r["Close"] >= r["Open"]:
            side = "buy"
        elif pos <= 0.4 and r["Close"] < r["Open"]:
            side = "sell"
        else:
            side = "absorb"
        out[side] += 1
        out["usd"] += float(r["Volume"] * r["Close"])
        out["last"] = {"t": ts.strftime("%H:%M"), "side": side, "x": round(float(r["Volume"] / med.loc[ts]), 1),
                       "usd": round(float(r["Volume"] * r["Close"]))}
    out["usd"] = round(out["usd"])
    return out


def session_metrics(bars: pd.DataFrame, prev_close: float | None, baseline: list[float] | None,
                    now: datetime, live_px: float | None = None) -> dict:
    """Todo lo que la decisión necesita de las velas de hoy (pre-market incluido)."""
    d = to_et(bars)
    now = now.astimezone(ET)
    now_m = now.hour * 60 + now.minute
    out: dict = {"now_m": now_m}
    if d.empty:
        return out
    today = d[d["day"] == now.date()]
    if today.empty:
        return out
    pm = today[today["m"] < OPEN_M]
    reg = today[(today["m"] >= OPEN_M) & (today["m"] < CLOSE_M)]
    if not pm.empty:
        out["pm_vol"] = float(pm["Volume"].sum())
        out["pm_high"] = float(pm["High"].max())
        out["pm_last"] = float(pm["Close"].iloc[-1])
    if reg.empty:
        px = live_px or out.get("pm_last")
        out["px"] = px
        if px and prev_close:
            out["chg"] = round(100 * (px / prev_close - 1), 2)
        return out

    px = float(live_px or reg["Close"].iloc[-1])
    out["px"] = px
    if prev_close:
        out["chg"] = round(100 * (px / prev_close - 1), 2)
        out["gap"] = round(100 * (float(reg["Open"].iloc[0]) / prev_close - 1), 2)
    tp = (reg["High"] + reg["Low"] + reg["Close"]) / 3
    vol = reg["Volume"]
    out["vwap"] = float((tp * vol).sum() / vol.sum()) if vol.sum() > 0 else float(tp.mean())
    out["ext"] = round(100 * (px / out["vwap"] - 1), 2)
    orb = reg[reg["m"] < OPEN_M + OR_MINUTES]
    out["orh"], out["orl"] = float(orb["High"].max()), float(orb["Low"].min())
    out["or_done"] = now_m >= OPEN_M + OR_MINUTES
    out["hod"], out["lod"] = float(reg["High"].max()), float(reg["Low"].min())
    out["dist_hod"] = round(100 * (out["hod"] / px - 1), 2)
    out["usd_vol"] = float((reg["Close"] * vol).sum())
    elapsed = int(min(max(reg["m"].iloc[-1] - OPEN_M, 0), SESSION_LEN - 1))
    if baseline and baseline[elapsed] > 0:
        out["rvol"] = round(float(vol.sum()) / baseline[elapsed], 2)
    # Aceleración: volumen de los últimos 5 min vs. mediana de bloques de 5 min de hoy
    if len(reg) >= 15:
        blocks = vol.groupby((reg["m"] - OPEN_M) // 5).sum()
        last5 = float(vol.tail(5).sum())
        prev = blocks.iloc[:-1]
        if len(prev) >= 2 and prev.median() > 0:
            out["accel"] = round(last5 / float(prev.median()), 2)
    closes = reg["Close"]
    if len(closes) > 5:
        out["chg5"] = round(100 * (px / float(closes.iloc[-6]) - 1), 2)
    if len(closes) > 15:
        out["chg15"] = round(100 * (px / float(closes.iloc[-16]) - 1), 2)
    # Escalera: mínimos crecientes en las últimas 3 velas de 5 min completas
    b5 = reg.groupby((reg["m"] - OPEN_M) // 5).agg(low=("Low", "min"), high=("High", "max"), n=("Low", "size"))
    done = b5[b5["n"] >= 5]
    if len(done) >= 3:
        lows = done["low"].tail(3).values
        out["higher_lows"] = bool(lows[0] < lows[1] < lows[2])
        out["swing_low"] = float(done["low"].tail(2).min())
    else:
        out["higher_lows"] = False
        out["swing_low"] = float(reg["Low"].tail(10).min())
    out["whale"] = whale_bars(reg, now_m)
    out["bars"] = int(len(reg))
    out["last_bar_m"] = int(reg["m"].iloc[-1])
    return out
