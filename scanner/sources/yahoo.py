"""Yahoo Finance vía yfinance (versión fijada en requirements.txt)."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from ..util import fnum, log, retry

US_EXCH = ["NMS", "NYQ", "NGM", "NCM", "ASE"]


def _quotes(res):
    return (res or {}).get("quotes", []) if isinstance(res, dict) else []


def screens(horizon: str) -> dict[str, list[dict]]:
    """Listas de candidatos. Devuelve {nombre_fuente: [quote, ...]}."""
    Q = yf.EquityQuery
    exch = Q("is-in", ["exchange", *US_EXCH])
    out = {}
    custom = {
        "subidas": (Q("and", [Q("gt", ["percentchange", 5]), exch, Q("gt", ["dayvolume", 300000]), Q("gte", ["intradayprice", 0.5])]), "percentchange"),
        "short_alto": (Q("and", [Q("gt", ["short_percentage_of_float.value", 15]), exch, Q("gt", ["avgdailyvol3m", 200000]), Q("gt", ["intradayprice", 1])]), "short_percentage_of_float.value"),
        "volumen": (Q("and", [exch, Q("gt", ["dayvolume", 2000000]), Q("gte", ["intradayprice", 1])]), "dayvolume"),
    }
    for name, (q, sort) in custom.items():
        r = retry(lambda q=q, sort=sort: yf.screen(q, size=150, sortField=sort, sortAsc=False), what=f"screen {name}")
        out[name] = _quotes(r)
    predefined = ["small_cap_gainers", "most_actives", "aggressive_small_caps"]
    if horizon == "B":
        predefined += ["growth_technology_stocks", "undervalued_growth_stocks"]
    for name in predefined:
        r = retry(lambda name=name: yf.screen(name, count=100), what=f"screen {name}")
        out[name] = _quotes(r)
    for k, v in out.items():
        log.info("fuente %s: %d", k, len(v))
    return out


def batch_quotes(symbols: list[str]) -> dict[str, dict]:
    """Cotización v7 en lotes de 40 (incluye pre-market, bid/ask, volúmenes medios)."""
    from yfinance.data import YfData
    data, out = YfData(), {}
    for i in range(0, len(symbols), 40):
        chunk = symbols[i:i + 40]
        params = {"symbols": ",".join(chunk), "formatted": "false", "lang": "en-US", "region": "US"}
        r = retry(lambda: data.get_raw_json("https://query1.finance.yahoo.com/v7/finance/quote", params=params), what="quote batch")
        for q in ((r or {}).get("quoteResponse") or {}).get("result") or []:
            if q.get("symbol"):
                out[q["symbol"]] = q
        time.sleep(0.4)
    return out


def _split(df: pd.DataFrame, symbols: list[str]) -> dict[str, pd.DataFrame]:
    out = {}
    if df is None or df.empty:
        return out
    if isinstance(df.columns, pd.MultiIndex):
        lv0 = set(df.columns.get_level_values(0))
        for s in symbols:
            if s in lv0:
                d = df[s].dropna(how="all")
                if not d.empty:
                    out[s] = d
    elif len(symbols) == 1:
        out[symbols[0]] = df.dropna(how="all")
    return out


def history(symbols: list[str], period="1y", interval="1d", prepost=False) -> dict[str, pd.DataFrame]:
    out = {}
    for i in range(0, len(symbols), 80):
        chunk = symbols[i:i + 80]
        df = retry(lambda: yf.download(chunk, period=period, interval=interval, group_by="ticker", auto_adjust=True,
                                       threads=True, progress=False, prepost=prepost), what=f"download {interval}")
        out.update(_split(df, chunk))
        time.sleep(1)
    return out


def info(symbol: str) -> dict:
    return retry(lambda: yf.Ticker(symbol).info, tries=2, what=f"info {symbol}") or {}


def option_chains(symbol: str, max_days=30):
    """Hasta 3 vencimientos ≤ max_days. Devuelve [(días, calls_df, puts_df)]."""
    t = yf.Ticker(symbol)
    exps = retry(lambda: t.options, tries=2, what=f"options {symbol}") or ()
    now = datetime.now(timezone.utc).date()
    out = []
    for e in exps:
        d = (datetime.strptime(e, "%Y-%m-%d").date() - now).days
        if d < 0 or d > max(max_days, 45):
            continue
        ch = retry(lambda e=e: t.option_chain(e), tries=2, what=f"chain {symbol} {e}")
        if ch is not None and ch.calls is not None:
            out.append((d, ch.calls, ch.puts))
        if len(out) >= 3:
            break
        time.sleep(0.25)
    return out, bool(exps)


def news(symbol: str, count=8) -> list[dict]:
    raw = retry(lambda: yf.Ticker(symbol).get_news(count=count, tab="all"), tries=2, what=f"news {symbol}") or []
    return parse_news(raw)


def parse_news(raw) -> list[dict]:
    items = []
    for it in raw or []:
        c = it.get("content") if isinstance(it.get("content"), dict) else it
        title = c.get("title")
        if not title:
            continue
        when = c.get("pubDate") or c.get("displayTime")
        ts = None
        if isinstance(when, str):
            try:
                ts = datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = None
        elif fnum(it.get("providerPublishTime")):
            ts = float(it["providerPublishTime"])
        url = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url") or it.get("link"))
        prov = (c.get("provider") or {}).get("displayName") or it.get("publisher") or ""
        items.append({"title": title.strip(), "ts": ts, "url": url, "src": prov})
    items.sort(key=lambda x: x["ts"] or 0, reverse=True)
    return items


def insider_buys(symbol: str, days=30) -> dict | None:
    df = retry(lambda: yf.Ticker(symbol).insider_transactions, tries=2, what=f"insiders {symbol}")
    return parse_insiders(df, days)


def parse_insiders(df, days=30) -> dict | None:
    if df is None or not isinstance(df, pd.DataFrame):
        return None
    if df.empty:
        return {"n": 0, "ceo": False}
    cut = pd.Timestamp.now(tz="UTC").tz_localize(None) - pd.Timedelta(days=days)
    d = df.copy()
    d["Start Date"] = pd.to_datetime(d.get("Start Date"), errors="coerce")
    txt = d.get("Text", pd.Series([""] * len(d))).astype(str)
    buys = d[(d["Start Date"] >= cut) & txt.str.contains("Purchase", case=False, na=False)]
    names = set(buys.get("Insider", pd.Series(dtype=str)).astype(str))
    pos = " ".join(buys.get("Position", pd.Series(dtype=str)).astype(str)).lower()
    ceo = any(k in pos for k in ("chief executive", "chief financial", "ceo", "cfo"))
    return {"n": len(names), "ceo": ceo}


def earnings_calendar(start, end) -> pd.DataFrame | None:
    cal = yf.Calendars()
    frames = []
    for off in (0, 100, 200, 300):
        df = retry(lambda off=off: cal.get_earnings_calendar(start=start, end=end, limit=100, offset=off,
                                                             filter_most_active=False, force=True), tries=2, what="earnings cal")
        if df is None or df.empty:
            break
        frames.append(df)
        if len(df) < 100:
            break
    return pd.concat(frames) if frames else None


def splits_calendar(start, end) -> pd.DataFrame | None:
    cal = yf.Calendars()
    return retry(lambda: cal.get_splits_calendar(start=start, end=end, limit=100, force=True), tries=2, what="splits cal")


def earnings_dates(symbol: str) -> dict | None:
    """Earnings más cercano (pasado o futuro) de un ticker: {'date', 'surprise', 'reported'}."""
    df = retry(lambda: yf.Ticker(symbol).get_earnings_dates(limit=4), tries=2, what=f"earnings {symbol}")
    return parse_earnings_dates(df)


def parse_earnings_dates(df) -> dict | None:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return None
    now = pd.Timestamp.now(tz="UTC")
    best = None
    for ts, r in df.iterrows():
        t = pd.Timestamp(ts)
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        rep = fnum(r.get("Reported EPS"))
        cand = {"date": t.to_pydatetime(), "surprise": fnum(r.get("Surprise(%)")), "reported": rep is not None}
        if best is None or abs((now - t).total_seconds()) < abs((now - pd.Timestamp(best["date"])).total_seconds()):
            best = cand
    return best
