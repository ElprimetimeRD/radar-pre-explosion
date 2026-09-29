"""Convierte datos crudos en las entradas del motor (mismas claves que el evaluador de la página)."""
from __future__ import annotations

import re
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .util import ET, fnum, rnd


# ---------------- Precio diario ----------------
def daily_features(df: pd.DataFrame, spy: pd.DataFrame | None) -> dict:
    """pct52, rel6m, maxRet, bbw (percentil), vcp, dryUp, rvolD, last, prevClose, n."""
    out: dict = {}
    if df is None or df.empty or "Close" not in df:
        return out
    d = df.dropna(subset=["Close"])
    if len(d) < 25:
        return out
    c, hi, lo, v = d["Close"], d["High"], d["Low"], d["Volume"].fillna(0)
    out["n"] = int(len(d))
    out["last"] = float(c.iloc[-1])
    out["prevClose"] = float(c.iloc[-2])
    out["pct52"] = rnd(100 * c.iloc[-1] / hi.tail(252).max(), 1)
    if len(d) > 126 and spy is not None and not spy.empty:
        s = spy["Close"].dropna()
        r_t = c.iloc[-1] / c.iloc[-127] - 1
        r_s = s.iloc[-1] / s.iloc[-127] - 1 if len(s) > 126 else 0
        out["rel6m"] = rnd(100 * (r_t - r_s), 1)
    ret = c.pct_change()
    out["maxRet"] = rnd(100 * ret.tail(21).max(), 1)
    mid = c.rolling(20).mean()
    sd = c.rolling(20).std()
    bbw = ((mid + 2 * sd) - (mid - 2 * sd)) / mid
    win = bbw.dropna().tail(126)
    if len(win) >= 40:
        out["bbw"] = rnd(100 * (win < win.iloc[-1]).mean(), 0)
    rng = hi - lo
    nr7 = len(rng) >= 7 and rng.iloc[-1] <= rng.tail(7).min()
    tr = pd.concat([hi - lo, (hi - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
    atr10, atr50 = tr.tail(10).mean(), tr.tail(50).mean()
    tight = atr50 > 0 and atr10 / atr50 < 0.65 and (out["pct52"] or 0) >= 85
    out["vcp"] = "yes" if (nr7 or tight) else "no"
    v50 = v.tail(51).head(50).mean()
    if v50 > 0:
        base = v.tail(11).head(10).mean() < 0.7 * v50
        brk = v.iloc[-1] >= 1.5 * v50 and c.iloc[-1] >= c.tail(21).head(20).max()
        out["dryUp"] = "yes" if (base and brk) else "no"
        out["rvolD"] = rnd(v.iloc[-1] / v50, 2)
    return out


# ---------------- Intradía con pre-market ----------------
def intraday_features(df: pd.DataFrame, prev_close: float | None, now: datetime | None = None) -> dict:
    """gap %, volumen pre-market (M), RVOL ajustado por hora vs. los días previos a la misma hora."""
    out: dict = {}
    if df is None or df.empty or "Volume" not in df:
        return out
    d = df.dropna(subset=["Close"]).copy()
    idx = d.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    d.index = idx.tz_convert(ET)
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    today = now.date()
    t_now = now.hour * 60 + now.minute
    d["mins"] = d.index.hour * 60 + d.index.minute
    d["day"] = d.index.date
    cur = d[(d["day"] == today) & (d["mins"] <= t_now)]
    if cur.empty:
        return out
    last = float(cur["Close"].iloc[-1])
    if prev_close:
        out["gap"] = rnd(100 * (last / prev_close - 1), 1)
    out["lastPx"] = last
    pm = cur[cur["mins"] < 9 * 60 + 30]
    out["pmVol"] = rnd(pm["Volume"].sum() / 1e6, 3)
    prior = [g[g["mins"] <= t_now]["Volume"].sum() for day, g in d[d["day"] < today].groupby("day")]
    prior = [p for p in prior if p > 0]
    if prior:
        out["rvol"] = rnd(cur["Volume"].sum() / np.mean(prior), 2)
    return out


# ---------------- Opciones ----------------
def options_features(chains, spot: float | None) -> dict:
    """callVolOI (calls OTM ≤30 d), putCall, ivSpread ATM y smirk (put 0.90–0.95 − call ATM), en puntos de vol."""
    out: dict = {}
    if not chains or not spot:
        return out
    cv = coi = pv = allc = 0.0
    for days, calls, puts in chains:
        if days > 30:
            continue
        c = calls.fillna({"volume": 0, "openInterest": 0})
        p = puts.fillna({"volume": 0, "openInterest": 0}) if puts is not None else None
        otm = c[c["strike"] > spot]
        cv += otm["volume"].sum()
        coi += otm["openInterest"].sum()
        allc += c["volume"].sum()
        if p is not None:
            pv += p["volume"].sum()
    if cv >= 500 and coi > 0:
        out["callVolOI"] = rnd(cv / coi, 2)
    if allc + pv >= 300 and allc > 0:
        out["putCall"] = rnd(pv / allc, 2)

    def ok(df):
        return df[(df["impliedVolatility"] > 0.05) & (df["impliedVolatility"] < 5) & (df["bid"].fillna(0) > 0)]

    cand = sorted([ch for ch in chains if ch[0] >= 5], key=lambda ch: abs(ch[0] - 30)) or chains
    days, calls, puts = cand[0]
    if puts is not None:
        c, p = ok(calls), ok(puts)
        common = sorted(set(c["strike"]) & set(p["strike"]), key=lambda k: abs(k - spot))
        if common:
            k = common[0]
            civ = float(c[c["strike"] == k]["impliedVolatility"].iloc[0])
            piv = float(p[p["strike"] == k]["impliedVolatility"].iloc[0])
            out["ivSpread"] = rnd(100 * (civ - piv), 1)
            otm_p = p[(p["strike"] >= 0.88 * spot) & (p["strike"] <= 0.96 * spot)]
            if not otm_p.empty:
                row = otm_p.iloc[(otm_p["strike"] - 0.925 * spot).abs().argsort().iloc[0]]
                out["smirk"] = rnd(100 * (float(row["impliedVolatility"]) - civ), 1)
    return out


# ---------------- Catalizador por titulares ----------------
RULES = [
    ("offer", r"(public offering|registered direct|at[- ]the[- ]market|\bATM\b (program|offering)|priced .*offering|private placement|securities purchase agreement|shelf (registration|offering)|warrant (inducement|exercise))"),
    ("fda", r"(\bFDA\b.*(approv|clear|grant|accept)|(approv|clear)\w* by the FDA|positive (topline|top-line|phase)|met (its|the) primary endpoint)"),
    ("mna", r"(to acquire|to be acquired|acquisition of|merger agreement|buyout|takeover|tender offer|go-private|13D|strategic alternatives)"),
    ("contract", r"((contract|award|order|purchase order)\b.{0,60}\$\s?\d|\$\s?\d[\d.,]*\s?(million|billion|[MB])\b.{0,60}(contract|award|order))"),
    ("index", r"((added|join|inclusion|to replace).{0,40}(S&P ?500|S&P ?MidCap|S&P ?SmallCap|Nasdaq-100|Russell))"),
    ("analyst", r"(upgrade[sd]?\b|raises? (its )?price target|initiat\w+ .{0,30}(buy|outperform|overweight))"),
    ("theme", r"(\bAI\b|artificial intelligence|quantum|nuclear|\bSMR\b|uranium|drone|defense contract|bitcoin|crypto|digital asset treasury|rare earth)"),
    ("softpr", r"(partnership|collaborat|memorandum of understanding|\bMOU\b|letter of intent|strategic alliance|launches|unveils|announces)"),
]
PRIORITY = ["fda", "mna", "contract", "index", "analyst", "theme", "softpr"]


def classify_news(items: list[dict], now_ts: float, horizon: str) -> dict:
    """Devuelve catType, catAge, offer (bool) y el titular que lo justifica."""
    out: dict = {"offer": False}
    best = None
    for it in items[:10]:
        ts = it.get("ts")
        if not ts:
            continue
        age_h = (now_ts - ts) / 3600
        if age_h > 24 * 30:
            continue
        t = it["title"]
        hits = [k for k, rx in RULES if re.search(rx, t, re.I)]
        if "offer" in hits and age_h <= 24 * 30:
            out["offer"] = True
            out["offerTitle"] = t
        for k in PRIORITY:
            if k in hits:
                rank = PRIORITY.index(k)
                if best is None or rank < best[0] or (rank == best[0] and age_h < best[1]):
                    best = (rank, age_h, k, t)
                break
    if best:
        _, age_h, k, t = best
        out["catType"] = k
        out["catAge"] = "fresh" if age_h < 12 else "d3" if age_h < 72 else "d20" if age_h < 480 else "old"
        out["catTitle"] = t
    return out


def earnings_catalyst(row: dict | None, now: datetime, horizon: str) -> dict:
    """row = {'date': datetime, 'surprise': float|None, 'reported': bool}. Earnings reciente o evento binario próximo."""
    if not row:
        return {}
    dt = row["date"]
    days = (now - dt).total_seconds() / 86400
    if row.get("reported") and 0 <= days <= 20 and (row.get("surprise") or 0) > 0:
        age = "fresh" if days < 0.6 else "d3" if days <= 3 else "d20"
        return {"catType": "earnings", "catAge": age, "eps": rnd(min(row["surprise"], 100), 1)}
    ahead = -days
    if 0 < ahead <= (2 if horizon == "A" else 14):
        return {"catType": "binary", "catAge": "ahead", "earningsIn": rnd(ahead, 1)}
    return {}


def spread_pct(q: dict) -> float | None:
    b, a = fnum(q.get("bid")), fnum(q.get("ask"))
    if not b or not a or a <= b:
        return None
    return rnd(100 * (a - b) / ((a + b) / 2), 2)
