"""Pruebas sin red del semáforo en vivo: python -m tests.test_live"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from scanner.util import ET
from live import decide as D
from live import halts, metrics, runner

DAY = datetime(2026, 9, 29, tzinfo=ET)


def bars(day, path, vol, start_m=570, pm=None):
    """path: lista de cierres por minuto desde start_m; vol: volumen por minuto (escalar o lista)."""
    rows, idx = [], []
    if pm:
        for i, c in enumerate(pm):
            idx.append(day.replace(hour=8, minute=0) + timedelta(minutes=i))
            rows.append((c, c * 1.002, c * 0.998, c, 20000))
    prev = path[0]
    for i, c in enumerate(path):
        v = vol[i] if isinstance(vol, (list, np.ndarray)) else vol
        o = prev
        idx.append(day.replace(hour=0, minute=0) + timedelta(minutes=start_m + i))
        rows.append((o, max(o, c) * 1.001, min(o, c) * 0.999, c, v))
        prev = c
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=pd.DatetimeIndex(idx))


def five_days(base_vol=10000):
    frames = []
    for k in range(1, 5):
        d = DAY - timedelta(days=k)
        frames.append(bars(d, [10.0] * 390, base_vol))
    return pd.concat(frames)


def runner_path(n):
    """Rango 9:30–9:45 entre 10.0 y 10.3, luego escalera suave hasta ~10.6 con mínimos crecientes."""
    p = list(np.linspace(10.0, 10.3, 8)) + list(np.linspace(10.3, 10.05, 7))
    p += list(10.3 + 0.012 * np.arange(n - 15) + 0.02 * np.sin(np.arange(n - 15) / 2))
    return p[:n]


def ctx(**k):
    c = {"phase": "open", "regime": "verde", "spy_chg": 0.3, "spread": 0.1, "atr": 5.0}
    c.update(k)
    return c


def test_compra():
    n = 40
    vol = [40000] * n
    vol[-3] = 400000  # vela ballena compradora
    b = bars(DAY, runner_path(n), vol)
    base = metrics.baseline_curve(five_days(), DAY.date())
    now = DAY.replace(hour=9, minute=30) + timedelta(minutes=n - 1)
    m = metrics.session_metrics(b, 9.6, base, now)
    assert m["or_done"] and m["px"] > m["orh"] and m["px"] > m["vwap"], m
    assert m["rvol"] and m["rvol"] > 3, m["rvol"]
    assert m["whale"]["buy"] >= 1, m["whale"]
    r = D.decide("TEST", m, ctx(cat={"type": "contract", "age": "fresh", "title": "x"}))
    assert r["decision"] == "COMPRA", (r["decision"], r["reason"], r["score"], m)
    p = r["plan"]
    assert p["stop"] < p["entry"] < p["t1"] < p["t2"] and 0.7 <= p["risk"] <= D.RISK_MAX, p
    return m


def test_vetos(m):
    assert D.decide("X", m, ctx(offer30=["424B5 2026-09-20"]))["decision"] == "NO"
    assert D.decide("X", m, ctx(spread=2.0))["decision"] == "NO"
    assert D.decide("X", m, ctx(regime="rojo"))["decision"] == "ESPERA"
    assert D.decide("X", {**m, "now_m": 15 * 60 + 40}, ctx())["decision"] == "NO"
    assert D.decide("X", {**m, "ext": 6.0}, ctx())["decision"] == "ESPERA"
    assert D.decide("X", {**m, "ext": 12.0}, ctx())["decision"] == "NO"
    assert D.decide("X", {**m, "px": m["vwap"] * 0.99, "ext": -1}, ctx())["decision"] == "NO"
    assert D.decide("X", {**m, "rvol": 1.1}, ctx())["decision"] == "NO"
    assert D.decide("X", m, ctx(halted={"code": "LUDP", "time": "10:01:00"}))["decision"] == "ESPERA"
    assert D.decide("X", m, ctx(phase="pre"))["decision"] in ("ESPERA", "NO")
    assert D.decide("X", {**m, "px": 0.8}, ctx())["decision"] == "NO"
    below = {**m, "px": m["orh"] * 0.999, "vwap": m["orh"] * 0.99, "ext": 0.9}
    r = D.decide("X", below, ctx())
    assert r["decision"] == "ESPERA" and "rompe" in r["trigger"], r


def test_espera_or():
    b = bars(DAY, runner_path(10), 40000)
    base = metrics.baseline_curve(five_days(), DAY.date())
    m = metrics.session_metrics(b, 9.6, base, DAY.replace(hour=9, minute=39))
    r = D.decide("X", m, ctx())
    assert r["decision"] == "ESPERA" and "apertura" in r["reason"], r


def test_limit_and_cutoff(m):
    r = D.decide("X", m, ctx())
    assert r["decision"] == "COMPRA" and r["plan"]["limit"] and r["plan"]["entry"] < m["px"], r
    late = D.decide("X", {**m, "now_m": 12 * 60 + 5}, ctx())
    assert late["decision"] == "NO" and "12:00" in late["reason"], late
    near = D.decide("X", {**m, "breakout": m["px"] * 0.999}, ctx())
    assert near["decision"] == "COMPRA" and not near["plan"]["limit"] and abs(near["plan"]["entry"] - m["px"]) < 1e-3, near
    # orden límite que nunca se llena → no ejecutada
    tr = runner.new_trade(r, "d", "10:00", 600)
    never = bars(DAY, [tr["entry"] * 1.02] * 15, 1000, start_m=601)
    ev = runner.advance(tr, metrics.to_et(never))
    assert ev == ["expired"] and tr["status"] == "no ejecutada", (ev, tr)
    # llena, toca +2 % y regresa a la entrada → t1-cerrada
    tr = runner.new_trade(r, "d", "10:00", 600)
    e = tr["entry"]
    path = bars(DAY, [e, e * 1.021, e * 1.0], 1000, start_m=601)
    ev = runner.advance(tr, metrics.to_et(path))
    assert ev[:2] == ["fill", "t1"] and tr["status"] == "t1-cerrada", (ev, tr)


def test_merge(tmp="/tmp/claude-0/merge_test"):
    import json, os, shutil, subprocess, sys
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    def run(trades):
        with open(f"{tmp}/in.json", "w") as f:
            json.dump({"day": "2026-09-29", "trades": trades}, f)
        subprocess.run([sys.executable, "tools/merge_trades.py", f"{tmp}/in.json", f"{tmp}/live"], check=True, capture_output=True)
        return json.load(open(f"{tmp}/live/summary.json"))
    run([{"t": "A", "time": "10:00", "status": "t1", "hit1": True}, {"t": "B", "time": "10:05", "status": "stop", "hit1": False}])
    sm = run([{"t": "A", "time": "10:00", "status": "abierta", "hit1": False}])  # reinicio del servidor: no retrocede
    assert sm["total"] == {"n": 2, "filled": 2, "t1": 1, "t2": 0, "stop": 1}, sm
    # día siguiente: el cierre de ayer llega por "history" aunque GitHub se haya saltado la corrida de la tarde
    with open(f"{tmp}/in.json", "w") as f:
        json.dump({"day": "2026-09-30", "trades": [], "history": [{"day": "2026-09-29", "trades": [
            {"t": "C", "time": "11:00", "status": "cierre", "hit1": False, "close_pct": 0.4}]}]}, f)
    subprocess.run([sys.executable, "tools/merge_trades.py", f"{tmp}/in.json", f"{tmp}/live"], check=True, capture_output=True)
    sm = json.load(open(f"{tmp}/live/summary.json"))
    assert sm["total"]["n"] == 3 and sm["total"]["filled"] == 3 and sm["days"] == 1, sm


def test_regime():
    assert D.regime({"px": 99, "vwap": 100, "chg": -1.2}, {"px": 99, "vwap": 100, "chg": -1.5})[0] == "rojo"
    assert D.regime({"px": 101, "vwap": 100, "chg": 0.4}, {"px": 101, "vwap": 100, "chg": 0.6})[0] == "verde"
    assert D.regime({"px": 99, "vwap": 100, "chg": -0.2}, {"px": 101, "vwap": 100, "chg": 0.2})[0] == "verde"
    assert D.regime({"px": 99, "vwap": 100, "chg": -0.2}, {"px": 99, "vwap": 100, "chg": -0.3})[0] == "amarillo"


def test_halts():
    xml = """<?xml version="1.0"?><rss xmlns:ndaq="http://www.nasdaqtrader.com/"><channel>
    <item><ndaq:HaltDate>09/29/2026</ndaq:HaltDate><ndaq:HaltTime>10:01:02</ndaq:HaltTime><ndaq:IssueSymbol>ABCD</ndaq:IssueSymbol>
    <ndaq:ReasonCode>LUDP</ndaq:ReasonCode><ndaq:ResumptionTradeTime>10:06:02</ndaq:ResumptionTradeTime></item>
    <item><ndaq:HaltDate>09/29/2026</ndaq:HaltDate><ndaq:HaltTime>10:20:00</ndaq:HaltTime><ndaq:IssueSymbol>WXYZ</ndaq:IssueSymbol>
    <ndaq:ReasonCode>T1</ndaq:ReasonCode><ndaq:ResumptionTradeTime></ndaq:ResumptionTradeTime></item>
    <item><ndaq:HaltDate>09/28/2026</ndaq:HaltDate><ndaq:HaltTime>11:00:00</ndaq:HaltTime><ndaq:IssueSymbol>OLD</ndaq:IssueSymbol>
    <ndaq:ReasonCode>LUDP</ndaq:ReasonCode></item></channel></rss>"""
    h = halts.parse(xml, DAY.replace(hour=10, minute=30))
    assert set(h) == {"ABCD", "WXYZ"} and h["ABCD"]["resumed"] and not h["WXYZ"]["resumed"], h


def test_classify():
    now = DAY.timestamp()
    c = runner.classify([{"title": "Acme beats estimates and raises guidance", "ts": now - 3600},
                         {"title": "Acme announces partnership", "ts": now - 7200}], now)
    assert c["type"] == "earnings" and c["age"] == "fresh" and not c["offer"], c
    c = runner.classify([{"title": "Acme announces pricing of $10 million registered direct offering", "ts": now - 3600}], now)
    assert c["offer"], c


def test_cycle():
    """Ciclo completo con fuentes falsas: universo, contexto, decisión, alerta y seguimiento."""
    from scanner.sources import other, yahoo
    n = 40
    now = (DAY.replace(hour=9, minute=30) + timedelta(minutes=n - 1))
    vol = [40000] * n
    vol[-3] = 400000
    today = {"RUN": bars(DAY, runner_path(n), vol), "SPY": bars(DAY, list(np.linspace(500, 502, n)), 50000),
             "QQQ": bars(DAY, list(np.linspace(400, 402, n)), 50000)}
    hist5 = {s: five_days() for s in today}
    daily = pd.DataFrame({"Open": 10.0, "High": 10.5, "Low": 10.0, "Close": 10.2, "Volume": 1e6},
                         index=pd.date_range("2026-06-01", periods=60))

    def history(syms, period="1y", interval="1d", prepost=False):
        if interval == "1d":
            return {s: daily for s in syms}
        return {s: (hist5 if period == "5d" else today)[s] for s in syms if s in today}

    quotes = {s: {"symbol": s, "regularMarketPrice": float(today[s]["Close"].iloc[-1]), "regularMarketPreviousClose": float(today[s]["Close"].iloc[0]) * 0.96,
                  "bid": float(today[s]["Close"].iloc[-1]) * 0.9995, "ask": float(today[s]["Close"].iloc[-1]) * 1.0005,
                  "quoteType": "EQUITY", "regularMarketChangePercent": 6, "regularMarketVolume": 2e6, "averageDailyVolume10Day": 1e6,
                  "shortName": s} for s in today}
    patches = {
        (yahoo, "history"): history,
        (yahoo, "batch_quotes"): lambda syms: {s: quotes[s] for s in syms if s in quotes},
        (yahoo, "news"): lambda s, count=10: [{"title": "RUN wins $50 million contract award", "ts": now.timestamp() - 1800, "url": None}],
        (yahoo, "option_chains"): lambda s, max_days=30: ([], False),
        (other, "sec_ticker_map"): lambda: {},
        (halts, "fetch"): lambda: None,
    }
    old = {k: getattr(*k) for k in patches}
    for (mod, name), fn in patches.items():
        setattr(mod, name, fn)
    try:
        r = runner.Radar(notify=False)
        r.trades, r.sent = {}, set()
        r.universe, r.universe_ts = ["RUN"], 1e18
        r.cycle(now.astimezone(ET))
        snap = r.snapshot
        row = snap["rows"][0]
        assert snap["regime"] == "verde", snap["regime"]
        assert row["decision"] == "COMPRA", (row["decision"], row["reason"], row["score"])
        assert snap["best"] == "RUN" and "RUN" in r.trades and "buy:RUN" in r.sent
        # siguiente minuto: sube a +2 %
        e = r.trades["RUN"]["entry"]
        assert r.trades["RUN"]["limit"] and r.trades["RUN"]["status"] == "pendiente", r.trades["RUN"]
        extra = bars(DAY, [e, e * 1.01, e * 1.025], 50000, start_m=570 + n)  # retesteo: llena la límite y sube
        today["RUN"] = pd.concat([today["RUN"], extra])
        r.cycle((now + timedelta(minutes=3)).astimezone(ET))
        assert "fill:RUN" in r.sent, r.trades["RUN"]
        assert r.trades["RUN"]["hit1"] and "t1:RUN" in r.sent, r.trades["RUN"]
        # replay de la sesión con las mismas velas: debe encontrar la COMPRA y marcarla como +2 %
        import live.runner as LR
        from datetime import datetime as _dt
        class FakeDT(_dt):
            @classmethod
            def now(cls, tz=None):
                return (now + timedelta(minutes=3)).astimezone(tz)
        old_dt = LR.datetime
        LR.datetime = FakeDT
        try:
            import datetime as dtmod
            orig = dtmod.datetime
            dtmod.datetime = FakeDT
            rep = r.replay(step=5)
        finally:
            LR.datetime = old_dt
            dtmod.datetime = orig
        assert rep["status"] == "ok" and "ejecutadas" in rep["resumen"], rep
        for x in rep["trades"]:
            assert x["result"] in ("+2%", "+5%", "abierta", "stop", "no ejecutada"), x
        # cierre del día: nada queda abierto y el día pasa al historial
        r._eod(now.replace(hour=16, minute=5).astimezone(ET))
        x = r.trades["RUN"]
        assert x["status"] == "cierre" and x["hit1"] and x["close_pct"] is not None, x
        assert DAY.date().isoformat() in r.history and r.stats()["open"] == 0, r.stats()
    finally:
        for (mod, name), fn in old.items():
            setattr(mod, name, fn)
    return snap


if __name__ == "__main__":
    m = test_compra()
    test_vetos(m)
    test_limit_and_cutoff(m)
    test_merge()
    test_espera_or()
    test_regime()
    test_halts()
    test_classify()
    snap = test_cycle()
    print("OK · ejemplo:", {k: snap["rows"][0][k] for k in ("t", "decision", "score", "reason", "why", "plan")})
