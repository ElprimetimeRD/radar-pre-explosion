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
    k = metrics.OR_MINUTES
    b = bars(DAY, runner_path(k), 40000)
    base = metrics.baseline_curve(five_days(), DAY.date())
    m = metrics.session_metrics(b, 9.6, base, DAY.replace(hour=9, minute=30 + k - 1))
    r = D.decide("X", m, ctx())
    assert r["decision"] == "ESPERA" and "apertura" in r["reason"], r


def test_triggers(m):
    """El gatillo es el máximo más alto que haya que romper, y nunca hay COMPRA por debajo de un máximo previo."""
    hod = m["orh"] * 1.03
    below = {**m, "px": m["orh"] * 0.999, "vwap": m["orh"] * 0.99, "ext": 0.9, "hod": hod}
    r = D.decide("X", below, ctx())
    assert r["decision"] == "ESPERA" and "máximo del día" in r["reason"] and abs(r["level"] - hod) < 1e-3, r
    assert f"{hod:.2f}" in r["trigger"] and abs(r["plan"]["entry"] - hod * 1.001) < 1e-3, r
    flat = {**below, "hod": m["orh"]}
    r = D.decide("X", flat, ctx())
    assert "apertura" in r["reason"] and abs(r["level"] - m["orh"]) < 1e-3 and r["plan"]["risk"] >= D.RISK_MIN - 1e-6, r
    under = D.decide("X", {**m, "breakout": m["px"] * 1.01}, ctx())  # un máximo previo arriba: todavía no es ruptura
    assert under["decision"] == "ESPERA" and "aún no rompe" in under["reason"] and under["level"] > m["px"], under
    calm = D.decide("X", {**m, "higher_lows": False, "whale": {}, "accel": 1.0, "breakout": m["px"] * 0.999}, ctx())
    assert calm["decision"] == "ESPERA" and "nuevo máximo" in calm["trigger"] and calm["level"] == round(m["hod"], 4), calm


def test_strength_acn():
    """ACN 1-oct 10:10: +23 % por resultados, RVOL 14×, calls inusuales, debajo del máximo de apertura. Antes: fuerza 99."""
    m = {"now_m": 610, "px": 225.6, "chg": 23.0, "vwap": 221.6, "ext": 1.8, "orh": 226.5, "hod": 227.63, "or_done": True,
         "higher_lows": True, "dist_hod": 0.9, "rvol": 14.3, "whale": {}, "usd_vol": 9e8, "swing_low": 224.0, "breakout": 227.63}
    c = ctx(cat={"type": "earnings", "age": "fresh", "title": "x"}, news_ok=True, callVolOI=1.8, atr=3.93, spy_chg=-0.2,
            shelf=True, spread=0.04, regime="amarillo")
    r = D.decide("ACN", m, c)
    assert r["score"] <= 60 and r["decision"] == "ESPERA", (r["score"], r["why"])
    assert abs(r["level"] - 227.63) < 1e-6 and any("rangos diarios" in w for w in r["why"]), r
    # el mismo cuadro sin la subida exagerada sí puntúa alto: el tope castiga la extensión, no el volumen
    calm = D.decide("ACN", {**m, "chg": 4.0}, c)
    assert calm["score"] >= r["score"] + 5, (calm["score"], r["score"])


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
    # armadas (compras stop de los avisos ARMA): se guardan aparte y el resumen las cuenta por separado, junto con el
    # retraso y el deslizamiento promedio de la COMPRA
    with open(f"{tmp}/in.json", "w") as f:
        json.dump({"day": "2026-09-30", "trades": [{"t": "D", "time": "10:00", "status": "t1", "hit1": True, "lag_min": 4, "slip": 0.3}],
                   "armadas": [{"t": "D", "time": "09:50", "status": "abierta", "hit1": False, "fill_m": 600},
                               {"t": "E", "time": "09:55", "status": "cancelada", "hit1": False}]}, f)
    subprocess.run([sys.executable, "tools/merge_trades.py", f"{tmp}/in.json", f"{tmp}/live"], check=True, capture_output=True)
    sm = json.load(open(f"{tmp}/live/summary.json"))
    day = json.load(open(f"{tmp}/live/2026-09-30.json"))
    assert len(day["trades"]) == 1 and len(day["armadas"]) == 2, day
    assert sm["armadas"] == {"n": 2, "filled": 1, "t1": 0, "t2": 0, "stop": 0, "cancelled": 1}, sm
    assert sm["total"]["n"] == 4 and sm["lag"] == 4.0 and sm["slip"] == 0.3 and sm["days"] == 2, sm
    # un reinicio del servidor no retrocede una armada ya cancelada
    with open(f"{tmp}/in.json", "w") as f:
        json.dump({"day": "2026-09-30", "trades": [], "armadas": [{"t": "E", "time": "09:55", "status": "pendiente", "hit1": False}]}, f)
    subprocess.run([sys.executable, "tools/merge_trades.py", f"{tmp}/in.json", f"{tmp}/live"], check=True, capture_output=True)
    assert [x["status"] for x in json.load(open(f"{tmp}/live/2026-09-30.json"))["armadas"]] == ["abierta", "cancelada"]


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


def test_news_relevance():
    """Un resumen genérico de analistas no es noticia de cada ticker que Yahoo le asocia (UTHR, COHR, LITE el 1-oct)."""
    now = DAY.timestamp()
    roundup = [{"title": "Target upgraded, Moderna downgraded: Wall Street's top analyst calls", "ts": now - 3600}]
    assert not runner.classify(roundup, now, "LITE", "Lumentum Holdings Inc.").get("type")
    assert not runner.classify(roundup, now, "UTHR", "United Therapeutics Corporati").get("type")
    assert runner.classify(roundup, now).get("type") == "analyst"  # sin ticker/nombre: comportamiento anterior
    own = [{"title": "Lumentum upgraded to Buy at Goldman", "ts": now - 3600}]
    assert runner.classify(own, now, "LITE", "Lumentum Holdings Inc.")["type"] == "analyst"
    tick = [{"title": "UTHR stock upgraded after trial data", "ts": now - 3600}]
    assert runner.classify(tick, now, "UTHR", "United Therapeutics Corporati")["type"] == "analyst"
    phrase = [{"title": "United Therapeutics upgraded at JPMorgan", "ts": now - 3600}]
    assert runner.classify(phrase, now, "UTHR", "United Therapeutics Corporati")["type"] == "analyst"
    acn = [{"title": "Accenture Surges On Fiscal Q4 Beat, Outlook Amid AI Disruption Worries", "ts": now - 3600}]
    assert runner.classify(acn, now, "ACN", "Accenture plc")["type"] == "earnings"


def test_spread_sanity():
    """Bid/ask viejos de Yahoo no deben vetar acciones líquidas (MRVL con 10 % de spread a las 10:40)."""
    assert runner.sane_spread(251.5, 279.0, 265.3, 0.15) is None          # más ancho que lo que opera en 1 min
    assert runner.sane_spread(10.0, 10.5, 12.0, None) is None             # no cuadra con el último precio
    assert runner.sane_spread(265.2, 265.3, 265.25, 0.15) == 0.04
    assert runner.sane_spread(1.90, 2.00, 1.95, 2.5) == 5.13              # spread ancho real (centavos): se respeta
    assert runner.sane_spread(None, 2.0, 1.95) is None and runner.sane_spread(2.0, 2.0, 2.0) is None


def test_arm_and_fast(tmp="/tmp/claude-0/arm_test_state"):
    """Ruptura armada: aviso con la orden lista, el vigía rápido avisa al cruzar el gatillo y despierta el ciclo."""
    from scanner.sources import yahoo
    now = DAY.replace(hour=10, minute=0)

    def row(t, lvl, px, score=70, risk=1.0):
        entry = lvl * 1.001
        return {"t": t, "decision": "ESPERA", "level": lvl, "px": px, "chg": 6.0, "score": score,
                "reason": "debajo del máximo de apertura",
                "plan": {"entry": entry, "stop": entry * (1 - risk / 100), "t1": entry * 1.02, "risk": risk}}
    old_q = yahoo.batch_quotes
    try:
        r = runner.Radar(notify=False)
        r.trades, r.arms, r.sent = {}, {}, set()
        rows = [row("AAA", 50.0, 49.8), row("FAR", 50.0, 48.0), row("WEAK", 50.0, 49.9, score=40),
                row("RISKY", 50.0, 49.9, risk=3.0)] + [row(f"Z{i}", 20.0, 19.95) for i in range(10)]
        r._arm(rows, "verde", "open", 10 * 60)
        assert "AAA" in r.armed and not ({"FAR", "WEAK", "RISKY"} & set(r.armed)), r.armed
        arms = [k for k in r.sent if k.startswith("arm:")]
        assert len(arms) == runner.ARM_MAX and "arm:AAA" in r.sent, arms                 # tope diario de avisos
        assert len(r.arms) == runner.ARM_MAX and r.arms["AAA"]["status"] == "pendiente"   # una orden virtual por aviso
        r._arm(rows, "rojo", "open", 10 * 60)
        assert r.armed == {}                                                             # mercado en rojo: nada armado
        r._arm([row("MID", 50.0, 49.8, score=57)], "amarillo", "open", 10 * 60)
        assert r.armed == {}                                                             # amarillo pide 5 puntos más
        r._arm([row("MID", 50.0, 49.8, score=57)], "verde", "open", 10 * 60)
        assert "MID" in r.armed
        r._arm(rows, "verde", "open", 10 * 60)
        yahoo.batch_quotes = lambda syms: {s: {"symbol": s, "regularMarketPrice": 50.08 if s == "AAA" else 19.9} for s in syms}
        r.q_off_until = 0
        fired = r.fast_once(now)
        assert fired == ["AAA"] and r.wake.is_set() and "break:AAA:50.00" in r.sent, (fired, r.sent)
        r.wake.clear()
        assert r.fast_once(now) == [] and not r.wake.is_set()                           # no repite el aviso
        assert r.fast_once(now.replace(hour=16, minute=5)) == []                         # fuera de sesión no vigila
    finally:
        yahoo.batch_quotes = old_q


def test_pre_list():
    r = runner.Radar(notify=False)
    r.sent = set()
    got = []
    r.tg = lambda key, text: got.append((key, text))
    rows = [{"t": "GAP", "decision": "ESPERA", "chg": 12.5, "pmh": 8.4, "cat": {"type": "earnings"}},
            {"t": "NOP", "decision": "NO", "chg": 1.0}]
    r._pre_list(rows, DAY.replace(hour=9, minute=5))
    assert got == []                                                                    # antes de las 9:15 no avisa
    r._pre_list(rows, DAY.replace(hour=9, minute=20))
    assert len(got) == 1 and "GAP +12.5% · máx. pre 8.40 · resultados" in got[0][1] and "NOP" not in got[0][1], got


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
        r.trades, r.arms, r.sent = {}, {}, set()
        r.universe, r.universe_ts = ["RUN"], 1e18
        r.cycle(now.astimezone(ET))
        snap = r.snapshot
        row = snap["rows"][0]
        assert snap["regime"] == "verde", snap["regime"]
        assert row["decision"] == "COMPRA", (row["decision"], row["reason"], row["score"])
        assert snap["best"] == "RUN" and "RUN" in r.trades and "buy:RUN" in r.sent
        assert snap["armed"] == [] and "watching" in snap and "fast" in snap, snap.keys()
        assert snap["armadas"] == [] and snap["armStats"]["n"] == 0, snap["armStats"]
        # qué tan tarde llegó: minutos desde la ruptura del nivel y entrada sobre el nivel (retesteo: ~+0.2 %)
        tr = r.trades["RUN"]
        assert tr["level"] and 0 <= tr["slip"] <= 0.6 and tr["lag_min"] is not None and 0 <= tr["lag_min"] <= 30, tr
        assert snap["stats"]["lag"] == tr["lag_min"] and snap["stats"]["slip"] == tr["slip"], snap["stats"]
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


def _fake_sources(today, now):
    """Fuentes falsas (sin red) para correr un ciclo completo con las velas de `today`. Devuelve lo original."""
    from scanner.sources import other, yahoo
    hist5 = {s: five_days() for s in today}
    daily = pd.DataFrame({"Open": 10.0, "High": 10.5, "Low": 10.0, "Close": 10.2, "Volume": 1e6},
                         index=pd.date_range("2026-06-01", periods=60))

    def history(syms, period="1y", interval="1d", prepost=False):
        if interval == "1d":
            return {s: daily for s in syms}
        return {s: (hist5 if period == "5d" else today)[s] for s in syms if s in today}

    quotes = {s: {"symbol": s, "regularMarketPrice": float(today[s]["Close"].iloc[-1]), "quoteType": "EQUITY",
                  "regularMarketPreviousClose": float(today[s]["Close"].iloc[0]) * 0.96, "shortName": s} for s in today}
    patches = {(yahoo, "history"): history,
               (yahoo, "batch_quotes"): lambda syms: {s: quotes[s] for s in syms if s in quotes},
               (yahoo, "news"): lambda s, count=10: [{"title": f"{s} update", "ts": now.timestamp() - 1800, "url": None}],
               (yahoo, "option_chains"): lambda s, max_days=30: ([], False),
               (other, "sec_ticker_map"): lambda: {},
               (halts, "fetch"): lambda: None}
    old = {k: getattr(*k) for k in patches}
    for (mod, name), fn in patches.items():
        setattr(mod, name, fn)
    return old


def test_follow_outside_universe():
    """Una COMPRA abierta se sigue aunque su acción salga del universo (SITC, 1-oct: dejó de seguirse a los 6 min
    de la señal y nunca llegó el aviso de stop ni de objetivo)."""
    n = 40
    now = DAY.replace(hour=9, minute=30) + timedelta(minutes=n - 1)
    today = {"RUN": bars(DAY, [10.0] * n, 40000), "SPY": bars(DAY, list(np.linspace(500, 502, n)), 50000),
             "QQQ": bars(DAY, list(np.linspace(400, 402, n)), 50000),
             "OUT": bars(DAY, [20.0] * 20 + list(np.linspace(20.0, 19.6, n - 20)), 30000)}
    old = _fake_sources(today, now)
    try:
        r = runner.Radar(notify=False)
        r.trades, r.arms, r.sent = {}, {}, set()
        r.universe, r.universe_ts = ["RUN"], 1e18
        r.trades["OUT"] = {"t": "OUT", "day": DAY.date().isoformat(), "time": "09:45", "m": 585, "entry": 20.0,
                           "stop": 19.8, "t1": 20.4, "t2": 21.0, "risk": 1.0, "score": 70, "limit": False,
                           "valid_m": None, "status": "abierta", "hit1": False, "mfe": 0.0, "mae": 0.0, "last": 20.0}
        r.cycle(now.astimezone(ET))
        assert "OUT" not in [x["t"] for x in r.snapshot["rows"]]                       # no entra al semáforo...
        assert r.trades["OUT"]["status"] == "stop" and "stop:OUT" in r.sent, r.trades["OUT"]  # ...pero se sigue
    finally:
        for (mod, name), fn in old.items():
            setattr(mod, name, fn)


def test_stop_orders():
    """Compra stop de un aviso ARMA: se activa al subir a la entrada; si antes pierde el stop se cancela; si abre por
    encima del límite no se llena; vence a la hora de corte."""
    def arm():
        return runner.new_arm("AAA", {"level": 10.0, "entry": 10.01, "stop": 9.9, "t1": 10.21, "t2": 10.51, "risk": 1.1,
                                      "score": 70, "px": 9.95}, "2026-09-29", "10:00", 600, 720)

    def b(rows, start=601):
        df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"])
        df["m"] = range(start, start + len(df))
        return df
    o = arm()
    assert o["cap"] == 10.04 and o["status"] == "pendiente" and o["kind"] == "armada", o
    assert runner.advance(o, b([(9.95, 9.98, 9.93, 9.97)])) == [] and o["status"] == "pendiente"
    assert runner.advance(o, b([(9.97, 10.05, 9.96, 10.04), (10.04, 10.25, 10.03, 10.2)], 602)) == ["fill", "t1"]
    assert o["fill"] == 10.01 and o["fill_m"] == 602 and o["hit1"] and o["mfe"] > 2, o
    o = arm()
    assert runner.advance(o, b([(9.95, 9.97, 9.88, 9.9)])) == ["invalid"] and o["status"] == "cancelada"
    o = arm()
    assert runner.advance(o, b([(10.2, 10.3, 10.15, 10.25)])) == ["gap"] and o["status"] == "no ejecutada"
    o = arm()
    assert runner.advance(o, b([(10.03, 10.06, 10.02, 10.05)])) == ["fill"] and o["fill"] == 10.03  # abrió sobre la entrada
    o = arm()
    assert runner.advance(o, b([(9.95, 9.98, 9.93, 9.97)], 721)) == ["expired"] and o["status"] == "no ejecutada"


def test_track_arms():
    """El ciclo sigue la compra stop como si estuviera puesta: avisa al activarse, avisa para cancelarla si el semáforo
    la pasa a NO COMPRES o se acaba la hora de entradas, y una orden activada o cancelada no se vuelve a armar."""
    r = runner.Radar(notify=False)
    r.trades, r.arms, r.sent, r.day = {}, {}, set(), DAY.date()

    def row(t, dec="ESPERA", reason="debajo del máximo de apertura"):
        e = 50.0 * 1.001
        return {"t": t, "decision": dec, "level": 50.0, "px": 49.8, "chg": 6.0, "score": 70, "reason": reason,
                "plan": {"entry": e, "stop": e * 0.99, "t1": e * 1.02, "t2": e * 1.05, "risk": 1.0}}
    r._arm([row("AAA"), row("BBB"), row("CCC")], "verde", "open", 10 * 60)
    assert set(r.arms) == {"AAA", "BBB", "CCC"} and r.arms["AAA"]["day"] == DAY.date().isoformat(), r.arms
    e = r.arms["AAA"]["entry"]
    today = {"AAA": bars(DAY, [49.8, 49.9, e * 1.003, e * 1.004], 50000, start_m=601),   # rompe y se activa
             "BBB": bars(DAY, [49.8, 49.75, 49.7], 50000, start_m=601),                  # pierde el VWAP sin romper
             "CCC": bars(DAY, [49.8, 49.8], 50000, start_m=601)}                         # sigue esperando
    rows = [row("AAA"), row("BBB", "NO", "debajo del VWAP: mandan los vendedores"), row("CCC")]
    r._track_arms(rows, today, DAY.replace(hour=10, minute=5))
    assert r.arms["AAA"]["status"] == "abierta" and "arm-fill:AAA" in r.sent, r.arms["AAA"]
    assert r.arms["BBB"]["status"] == "cancelada" and "arm-x:BBB" in r.sent and "VWAP" in r.arms["BBB"]["cancel"]
    assert r.arms["CCC"]["status"] == "pendiente" and "arm-x:CCC" not in r.sent
    r._arm([row("AAA"), row("BBB"), row("CCC")], "verde", "open", 10 * 60 + 6)
    assert set(r.armed) == {"CCC"}, r.armed                    # el vigía rápido solo mira la que sigue pendiente
    vm = r.arms["CCC"]["valid_m"]
    r._track_arms([row("CCC")], {}, DAY.replace(hour=vm // 60, minute=vm % 60))
    assert r.arms["CCC"]["status"] == "no ejecutada" and "arm-x:CCC" in r.sent
    st = r.arm_stats()
    assert st == {"n": 3, "filled": 1, "t1": 0, "t2": 0, "stop": 0, "open": 1, "pending": 0, "cancelled": 2}, st
    # cierre: la activada se cierra al último precio y entra al resumen del día junto a las COMPRA
    r._eod(DAY.replace(hour=16, minute=5))
    assert r.arms["AAA"]["status"] == "cierre" and r.arms["AAA"]["close_pct"] is not None
    assert DAY.date().isoformat() in r.arm_history and "eod:" + DAY.date().isoformat() in r.sent


def test_break_lag():
    """Retraso de la señal: minutos desde la primera vela que superó el nivel."""
    b = bars(DAY, [9.9] * 10 + [10.1, 10.15, 10.2], 30000, start_m=600)   # rompe 10.0 en el minuto 610 (10:10)
    assert runner.break_lag(b, 10.0, DAY.replace(hour=10, minute=13)) == 3
    assert runner.break_lag(b, 11.0, DAY.replace(hour=10, minute=13)) is None    # todavía no rompe
    assert runner.break_lag(None, 10.0, DAY.replace(hour=10, minute=13)) is None


def test_day_reset():
    """Los avisos y las armadas son por día: al cambiar de día se vacían y lo de ayer pasa al historial.
    Antes, sin COMPRA el día anterior, un ARMA de ayer bloqueaba el de hoy para la misma acción."""
    r = runner.Radar(notify=False)
    r.trades, r.history, r.arm_history = {}, {}, {}
    r.sent, r.sent_day = {"arm:AAA", "break:AAA:50.00"}, "2026-09-28"
    r.arms = {"AAA": {"t": "AAA", "day": "2026-09-28", "status": "no ejecutada"}}
    r.cycle(DAY.replace(hour=20, minute=0).astimezone(ET))                        # 29-sep de noche: fase cerrada
    assert r.sent == set() and r.sent_day == "2026-09-29" and r.arms == {}, (r.sent, r.arms)
    assert r.arm_history.get("2026-09-28"), r.arm_history
    r.sent.add("arm:BBB")
    r.cycle(DAY.replace(hour=20, minute=5).astimezone(ET))                        # mismo día: no se vacía
    assert "arm:BBB" in r.sent


def test_memory_and_threads():
    """Los hilos de descarga terminados se sueltan (el 1-oct el servicio se quedó sin memoria a las 10:18) y la
    memoria se mide y avisa una vez por día si se acerca al límite."""
    import multitasking
    from scanner.sources import yahoo
    from live import memory

    @multitasking.task
    def job():
        return 1
    before = len(multitasking.config["TASKS"])
    for _ in range(30):
        job()
    for t in list(multitasking.config["TASKS"]):
        t.join()
    assert len(multitasking.config["TASKS"]) >= before + 30          # la librería guarda cada hilo terminado...
    gone = yahoo.prune_tasks()
    assert gone >= 30 and all(t.is_alive() for t in multitasking.config["TASKS"]), gone   # ...y ahora se sueltan
    m = memory.trim()
    assert m["rss_mb"] and m["rss_mb"] > 10 and 0 < m["pct"] < 100, m
    assert memory.pct(256.0) == round(100 * 256 / memory.LIMIT_MB, 1)
    r = runner.Radar(notify=False)
    r.sent, r.snapshot = set(), {"status": "ok"}
    r.after_cycle()
    assert r.snapshot["mem"]["rss_mb"] and not any(k.startswith("mem:") for k in r.sent)   # lejos del límite
    old = memory.ALERT_PCT
    memory.ALERT_PCT = 0.0
    try:
        r.after_cycle()
        r.after_cycle()
        assert sum(1 for k in r.sent if k.startswith("mem:")) == 1                          # un solo aviso por día
    finally:
        memory.ALERT_PCT = old


def test_positions(tmp="/tmp/claude-0/positions_test.json"):
    """Monitor de posiciones: cada nivel de caída y el objetivo avisan una sola vez; sobrevive reinicios."""
    import os
    from live.positions import Positions
    if os.path.exists(tmp):
        os.remove(tmp)
    P = Positions(tmp, drops=(2.0, 3.0), default_tp=3.0)
    P.upsert("run", 10.0, qty=50)
    assert P.check({"RUN": 9.90}) == []                                   # −1 %: nada
    a = P.check({"RUN": 9.79})                                            # −2.1 %: primer aviso
    assert len(a) == 1 and "cayó" in a[0][1] and "véndela RUN" in a[0][1] and "-10.50 US$" in a[0][1], a
    assert P.check({"RUN": 9.78}) == []                                   # mismo nivel: no repite
    b = P.check({"RUN": 9.69})                                            # −3.1 %: segundo nivel
    assert len(b) == 1 and a[0][0] != b[0][0], b
    assert P.check({"RUN": 9.60}) == []
    P.upsert("GAP", 10.0)
    c = P.check({"GAP": 9.60})                                            # cruza −2 % y −3 % de golpe: un solo aviso
    assert len(c) == 1 and "-4.0%" in c[0][1] and set(P.items["GAP"]["sent"]) == {"d2", "d3"}, c
    P.upsert("UP", 10.0, tp=3.0)
    assert P.check({"UP": 10.20}) == []
    d = P.check({"UP": 10.31})                                            # objetivo
    assert len(d) == 1 and "🎯" in d[0][1] and P.check({"UP": 10.50}) == []
    assert P.items["UP"]["max_pct"] >= 3.0 and P.items["RUN"]["min_pct"] <= -3.0
    assert P.check({}) == [] and P.check({"RUN": None, "UP": 0}) == []    # sin precio: no falla ni avisa
    # persistencia: reiniciar el servicio no repite avisos; volver a registrar igual conserva lo enviado
    Q = Positions(tmp, drops=(2.0, 3.0), default_tp=3.0)
    assert set(Q.items) == {"RUN", "GAP", "UP"} and Q.check({"RUN": 9.6}) == []
    Q.upsert("RUN", 10.0, qty=50)
    assert Q.check({"RUN": 9.6}) == []
    Q.upsert("RUN", 9.5)                                                  # entrada nueva = posición nueva: se reinician avisos
    assert Q.items["RUN"]["sent"] == [] and Q.check({"RUN": 9.2}) != []
    assert Q.remove("run") and not Q.remove("RUN") and "RUN" not in Q.symbols()
    for bad in (("", 10), ("TOOLONGX", 10), ("RUN", 0), ("RUN", -1), ("RUN", 10, -5), ("RUN", 10, None, 0)):
        try:
            Q.upsert(*bad)
        except ValueError:
            continue
        raise AssertionError(f"debió rechazar {bad}")
    os.remove(tmp)


def test_positions_api(tmp="/tmp/claude-0/positions_api_test.json"):
    """API de posiciones: cerrada sin token, 401 con token malo, alta/lista/baja con el bueno."""
    import os
    os.environ.update(NO_LOOP="1", NO_NOTIFY="1")
    from fastapi.testclient import TestClient
    from live import app as A
    from live.positions import Positions
    if os.path.exists(tmp):
        os.remove(tmp)
    A.radar.positions = Positions(tmp, drops=(2.0, 3.0), default_tp=3.0)
    cl = TestClient(A.app)
    body = {"t": "run", "entry": 10.41, "qty": 30, "tp": 3}
    os.environ.pop("POSITIONS_TOKEN", None)
    assert cl.get("/api/positions", headers={"X-Token": "x"}).status_code == 503
    os.environ["POSITIONS_TOKEN"] = "secreto"
    assert cl.get("/api/positions").status_code == 401
    assert cl.post("/api/positions", json=body, headers={"X-Token": "malo"}).status_code == 401
    ok = cl.post("/api/positions", json=body, headers={"X-Token": "secreto"})
    assert ok.status_code == 200 and ok.json()["t"] == "RUN" and ok.json()["tp"] == 3, ok.text
    assert cl.post("/api/positions", json={"t": "no valido!", "entry": 5}, headers={"X-Token": "secreto"}).status_code == 422
    lst = cl.get("/api/positions", headers={"X-Token": "secreto"}).json()["positions"]
    assert [p["t"] for p in lst] == ["RUN"] and lst[0]["entry"] == 10.41
    assert cl.delete("/api/positions/run", headers={"X-Token": "secreto"}).json() == {"removed": True}
    assert cl.get("/api/positions", headers={"X-Token": "secreto"}).json()["positions"] == []
    os.environ.pop("POSITIONS_TOKEN", None)
    if os.path.exists(tmp):
        os.remove(tmp)


def test_watch_positions(tmp="/tmp/claude-0/positions_watch_test.json"):
    """El ciclo lee precios (velas si Yahoo niega la cotización) y manda el aviso por el mismo canal de Telegram."""
    import os
    from scanner.sources import yahoo
    from live.positions import Positions
    if os.path.exists(tmp):
        os.remove(tmp)
    now = DAY.replace(hour=10, minute=30)
    bars_now = bars(DAY, [10.0, 9.9, 9.75], 50000, start_m=570 + 58)      # último cierre 9.75 = −2.5 %
    old_h, old_q = yahoo.history, yahoo.batch_quotes
    yahoo.history = lambda syms, period="1y", interval="1d", prepost=False: {s: bars_now for s in syms}
    yahoo.batch_quotes = lambda syms: {}                                   # Yahoo niega la cotización (401)
    sent = []
    try:
        r = runner.Radar(notify=False)
        r.positions = Positions(tmp, drops=(2.0, 3.0), default_tp=3.0)
        r.sent = set()
        r.tg = lambda key, text: sent.append((key, text))
        r.positions.upsert("RUN", 10.0)
        r._watch_positions(now, "open")
        assert len(sent) == 1 and "RUN cayó -2.5%" in sent[0][1], sent
        r._watch_positions(now, "open")
        assert len(sent) == 1, sent                                        # no repite
        r.positions.upsert("NOP", 5.0)                                     # sin datos de precio: no rompe el resto
        yahoo.history = lambda syms, period="1y", interval="1d", prepost=False: {}
        r._watch_positions(now, "open")
        assert len(sent) == 1
    finally:
        yahoo.history, yahoo.batch_quotes = old_h, old_q
        if os.path.exists(tmp):
            os.remove(tmp)


def test_bridge_state():
    """Puente IBKR: limpia lo que llega, los precios caducan a los 10 s y el escaneo a los 3 min; el estado que se
    publica no lleva precios."""
    from live.bridge import Bridge
    b = Bridge()
    t0 = 1_000_000.0
    fresh = b.update(scan={"TOP_PERC_GAIN": ["aaa", "BBB", "BRK B", "toolongx", "AAA"], "HOT_BY_VOLUME": ["CCC", "BBB"]},
                     quotes={"AAA": {"last": 10.5, "high": 10.6}, "BAD": {"last": float("nan")}, "NEG": {"last": -1},
                             "x y": {"last": 3}, "CCC": "no es un dict"},
                     info={"ib": "conectado", "error": None}, now=t0)
    assert fresh == ["AAA"], fresh
    assert b.scan["TOP_PERC_GAIN"] == ["AAA", "BBB"], b.scan                 # mayúsculas, sin clases ni repetidos
    assert b.scan_symbols(3, now=t0 + 5) == ["AAA", "CCC", "BBB"]           # intercalados: el 1.º de cada escaneo…
    assert b.scan_symbols(10, now=t0 + 181) == []                           # escaneo viejo: ya no alimenta el universo
    assert b.price("AAA", now=t0 + 9) == 10.5 and b.price("AAA", now=t0 + 11) is None
    st = b.status(now=t0 + 5)
    assert st["on"] and st["quotes"] == 1 and st["scan"] == {"TOP_PERC_GAIN": 2, "HOT_BY_VOLUME": 2}, st
    assert "10.5" not in str(st) and not b.status(now=t0 + 61)["on"]
    assert b.scan_symbols(0, now=t0) == []                                   # IBKR_TOP=0 apaga los agregados
    b.update(info={"lines": float("nan"), "big": "x" * 999}, now=t0)
    assert b.info["lines"] is None and len(b.info["big"]) == 200            # nada de NaN ni textos enormes
    b.update(quotes={f"Q{chr(65 + i // 26)}{chr(65 + i % 26)}": {"last": 1 + i} for i in range(100)}, now=t0 + 1)
    for k in range(4):
        b.update(quotes={f"R{k}{chr(65 + i // 26)}{chr(65 + i % 26)}"[:5]: {"last": 2} for i in range(100)}, now=t0 + 2 + k)
    assert len(b.quotes) <= 300, len(b.quotes)                              # memoria acotada


def test_bridge_break():
    """Con el puente conectado, la ruptura de una acción armada se avisa al llegar el precio (sin esperar los 15 s del
    vigía), una sola vez, y la armada guarda la hora y la fuente de la ruptura (sin el precio)."""
    from scanner.sources import yahoo
    now = DAY.replace(hour=10, minute=0)
    old_q = yahoo.batch_quotes
    asked = []
    try:
        r = runner.Radar(notify=False)
        r.trades, r.arms, r.sent = {}, {}, set()
        got, real_emit = [], r._emit
        r._emit = lambda key, text, wait=True, private=False: (got.append((key, text, private)),
                                                               real_emit(key, text, wait, private))
        a = {"level": 50.0, "entry": 50.05, "stop": 49.55, "t1": 51.05, "t2": 52.55, "risk": 1.0, "score": 70,
             "px": 49.9, "chg": 6.0, "reason": "debajo del máximo de apertura"}
        r.armed = {"AAA": dict(a), "BBB": dict(a)}
        r.arms["AAA"] = runner.new_arm("AAA", a, DAY.date().isoformat(), "09:58", 598, 720)
        out = r.on_bridge(quotes={"AAA": {"last": 49.98}}, now=now)
        assert out["fired"] == [] and set(out["armed"]) == {"AAA", "BBB"} and out["armed"]["AAA"]["level"] == 50.0, out
        out = r.on_bridge(quotes={"AAA": {"last": 50.08}}, now=now)
        assert out["fired"] == ["AAA"] and r.wake.is_set() and "break:AAA:50.00" in r.sent, out
        assert "(50.08, IBKR)" in got[-1][1], got
        assert r.breaks["AAA"] == {"break_t": "10:00:00", "break_src": "ibkr"}, r.breaks  # sin el precio de IBKR
        assert r.on_bridge(quotes={"AAA": {"last": 50.2}}, now=now)["fired"] == []                       # no repite
        assert r.on_bridge(quotes={"BBB": {"last": 51}}, now=now.replace(hour=16, minute=5))["fired"] == []  # fuera de sesión
        # El vigía rápido toma el precio del puente y solo le pregunta a Yahoo por las que no lo tienen
        yahoo.batch_quotes = lambda syms: (asked.append(list(syms)), {})[1]
        r.q_off_until = 0
        r.bridge.update(quotes={"BBB": {"last": 50.3}})
        r.wake.clear()
        assert r.fast_once(now) == ["BBB"] and asked == [] and r.wake.is_set(), asked
        assert "no persigas" in got[-1][1] and "IBKR" in got[-1][1], got[-1]
        r.armed["CCC"] = dict(a)
        r.fast_once(now)
        assert asked == [["CCC"]] and r.fast_state["ibkr"] == 2, (asked, r.fast_state)
        # En el siguiente ciclo la ruptura avisada queda en la armada (para medir cuánto adelanta el puente)
        r._track_arms([], {}, now.astimezone(ET))
        assert r.arms["AAA"]["break_t"] == "10:00:00" and r.arms["AAA"]["break_src"] == "ibkr", r.arms["AAA"]
        assert "50.08" not in str(r.arms) and "50.3" not in str(r.arms)        # el precio de IBKR no se guarda
    finally:
        yahoo.batch_quotes = old_q


def test_bridge_universe():
    """Lo que ven los escáneres de IBKR entra al universo: en sesión directo aunque Yahoo no lo liste; en pre-market
    solo compite en el ranking. Entre refrescos del universo, el ciclo lo agrega en el acto."""
    from scanner.sources import yahoo
    old = (yahoo.retry, yahoo.batch_quotes)
    try:
        yahoo.retry = lambda fn, tries=3, what="": None                      # las pantallas de Yahoo no traen nada
        yahoo.batch_quotes = lambda syms: {}
        r = runner.Radar(notify=False)
        r.bridge.update(scan={"HOT_BY_VOLUME": ["NEWB", "NEWC"], "TOP_PERC_GAIN": ["NEWD"]})
        now = DAY.replace(hour=10, minute=0)
        r.refresh_universe(now, "open", {})
        assert {"NEWB", "NEWC", "NEWD"} <= set(r.universe) and r.sources["NEWB"] == ["ibkr"], (r.universe, r.sources)
        r.premarket_rank = lambda syms: {}
        r.refresh_universe(now.replace(hour=8), "pre", {})
        assert "NEWB" not in r.universe, r.universe
    finally:
        yahoo.retry, yahoo.batch_quotes = old
    n = 40
    now = DAY.replace(hour=9, minute=30) + timedelta(minutes=n - 1)
    today = {"RUN": bars(DAY, [10.0] * n, 40000), "SPY": bars(DAY, list(np.linspace(500, 502, n)), 50000),
             "QQQ": bars(DAY, list(np.linspace(400, 402, n)), 50000), "HOT": bars(DAY, [5.0] * n, 30000)}
    old = _fake_sources(today, now)
    try:
        r = runner.Radar(notify=False)
        r.trades, r.arms, r.sent = {}, {}, set()
        r.universe, r.universe_ts = ["RUN"], 1e18                            # sin refresco del universo en este ciclo
        r.bridge.update(scan={"HOT_BY_VOLUME": ["HOT"]})
        r.cycle(now.astimezone(ET))
        row = next((x for x in r.snapshot["rows"] if x["t"] == "HOT"), None)
        assert row and row["src"] == ["ibkr"] and r.snapshot["bridge"]["on"], (row, r.snapshot.get("bridge"))
        sizes = []
        for k in range(3):                                                    # escaneos que rotan
            r.bridge.update(scan={"HOT_BY_VOLUME": [f"X{chr(65 + k)}{chr(65 + i)}" for i in range(runner.IBKR_TOP + 3)]})
            r.cycle(now.astimezone(ET))
            sizes.append(len(r.universe))
        assert sizes == [1 + runner.IBKR_TOP] * 3 and "XAA" not in r.universe and "HOT" not in r.universe, (sizes, r.universe)
    finally:
        for (mod, name), fn in old.items():
            setattr(mod, name, fn)


def test_bridge_api():
    """API del puente: cerrada sin BRIDGE_TOKEN, 401 con token malo; con el bueno guarda y responde qué vigilar.
    /health dice si está conectado sin mostrar precios."""
    import os
    os.environ.update(NO_LOOP="1", NO_NOTIFY="1")
    from fastapi.testclient import TestClient
    from live import app as A
    cl = TestClient(A.app)
    body = {"scan": {"TOP_PERC_GAIN": ["AAA"]}, "quotes": {"AAA": {"last": 10.25, "bid": None}}, "info": {"ib": "conectado"}}
    os.environ.pop("BRIDGE_TOKEN", None)
    assert cl.post("/api/bridge", json=body, headers={"X-Token": "x"}).status_code == 503
    os.environ["BRIDGE_TOKEN"] = "otro-secreto"
    assert cl.post("/api/bridge", json=body).status_code == 401
    assert cl.post("/api/bridge", json=body, headers={"X-Token": "malo"}).status_code == 401
    ok = cl.post("/api/bridge", json=body, headers={"X-Token": "otro-secreto"})
    assert ok.status_code == 200 and ok.json()["ok"] and "armed" in ok.json() and "phase" in ok.json(), ok.text
    h = cl.get("/health").json()
    assert h["bridge"]["on"] and h["bridge"]["scan"] == {"TOP_PERC_GAIN": 1} and h["bridge"]["ib"] == "conectado", h
    assert "10.25" not in str(h)
    assert cl.post("/api/bridge", content=b"{no es json", headers={"X-Token": "otro-secreto"}).status_code == 422
    assert cl.post("/api/bridge", content=b"x" * 300_000, headers={"X-Token": "malo"}).status_code == 401  # clave antes
    A.radar.bridge.update(info={"lines": float("nan")})
    assert cl.get("/health").status_code == 200                               # un NaN del puente no tumba /health
    # Una ruptura vista por IBKR: el aviso sale, pero su precio no llega a /api/trades ni a /api/signals (públicas)
    r, old_phase = A.radar, runner.phase_of
    runner.phase_of = lambda now: "open"
    try:
        a = {"level": 7.0, "entry": 7.01, "stop": 6.93, "t1": 7.15, "t2": 7.36, "risk": 1.1, "score": 70, "px": 6.95}
        r.sent, r.breaks, r.armed = set(), {}, {"PRC": dict(a)}
        r.arms = {"PRC": runner.new_arm("PRC", a, DAY.date().isoformat(), "10:00", 600, 720)}
        out = cl.post("/api/bridge", json={"quotes": {"PRC": {"last": 7.0137, "bid": 7.0111}}},
                      headers={"X-Token": "otro-secreto"}).json()
        assert out["fired"] == ["PRC"], out
        r._track_arms([], {}, DAY.replace(hour=10, minute=1).astimezone(ET))
        r.snapshot = {**r.snapshot, "armadas": list(r.arms.values()), "bridge": r.bridge.status()}
        for path in ("/api/trades", "/api/signals", "/health"):
            txt = cl.get(path).text
            assert "7.0137" not in txt and "7.0111" not in txt, (path, txt[:300])
        assert r.arms["PRC"]["break_src"] == "ibkr"
    finally:
        runner.phase_of = old_phase
        r.armed, r.arms = {}, {}
    os.environ.pop("BRIDGE_TOKEN", None)


def test_telegram_state():
    """Telegram: sin token o chat no intenta; si Telegram rechaza (token o chat equivocados) queda el motivo para
    /health; un error de red no deja el token ni en /health ni en el registro; al arrancar avisa que está en línea."""
    import os

    import requests
    env = {k: os.environ.get(k) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    old_post = requests.post

    class Resp:
        def __init__(self, code, body):
            self.status_code, self._b = code, body

        def json(self):
            return self._b
    try:
        r = runner.Radar(notify=True)
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        assert r._send("x") is False and r.tg_state == {}
        os.environ.update(TELEGRAM_BOT_TOKEN="123:SECRETO", TELEGRAM_CHAT_ID="42")
        requests.post = lambda *a, **k: Resp(400, {"ok": False, "description": "Bad Request: chat not found"})
        assert r._send("x") is False and r.tg_state["error"] == "Bad Request: chat not found", r.tg_state

        def boom(*a, **k):
            raise requests.ConnectionError("HTTPSConnectionPool url: /bot123:SECRETO/sendMessage")
        requests.post = boom
        assert r._send("x") is False and "SECRETO" not in str(r.tg_state), r.tg_state
        sent = []
        requests.post = lambda url, json=None, timeout=None: (sent.append(json["text"]), Resp(200, {"ok": True}))[1]
        r.hello()
        assert r.tg_state["ok"] and sent[0].startswith("✅ Semáforo en línea"), (sent, r.tg_state)
        from fastapi.testclient import TestClient
        from live import app as A
        h = TestClient(A.app).get("/health").json()["telegram"]
        assert "configured" in h and "SECRETO" not in str(h), h
    finally:
        requests.post = old_post
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_no_data_keeps_arms():
    """Si Yahoo no entrega velas (límite), la acción no queda sin precio y la compra stop armada NO se cancela. Un NO
    del mercado sí la cancela, salvo que el precio ya esté sobre la entrada (pudo activarse y las velas no lo muestran)."""
    m = metrics.session_metrics(pd.DataFrame(), 9.6, None, DAY.replace(hour=10), live_px=10.2)
    assert m["px"] == 10.2 and m["chg"] == round(100 * (10.2 / 9.6 - 1), 2), m
    r0 = D.decide("X", m, ctx())
    assert r0["decision"] == "NO" and r0.get("nodata"), r0                      # sin curva de volumen: NO por datos
    assert D.decide("X", {}, ctx()).get("nodata")                                # sin precio: NO por datos
    r = runner.Radar(notify=False)
    r.trades, r.arms, r.sent, r.day = {}, {}, set(), DAY.date()
    a = {"level": 50.0, "entry": 50.05, "stop": 49.55, "t1": 51.05, "t2": 52.55, "risk": 1.0, "score": 70, "px": 49.8}
    for t in ("AAA", "BBB", "CCC", "DDD"):
        r.arms[t] = runner.new_arm(t, a, DAY.date().isoformat(), "10:00", 600, 720)
    flat = bars(DAY, [49.8, 49.8], 50000, start_m=601)
    rows = [{"t": "AAA", "decision": "NO", "reason": "sin datos de precio", "nodata": True, "px": None},
            {"t": "BBB", "decision": "NO", "reason": "debajo del VWAP: mandan los vendedores", "px": 49.7},
            {"t": "CCC", "decision": "NO", "reason": "parabólico (+11.0% sobre VWAP): no persigas", "px": 50.9},
            {"t": "DDD", "decision": "NO", "reason": "debajo del VWAP: mandan los vendedores", "px": 49.7}]
    r._track_arms(rows, {"AAA": flat, "BBB": flat, "CCC": flat}, DAY.replace(hour=10, minute=3))
    assert r.arms["AAA"]["status"] == "pendiente" and "arm-x:AAA" not in r.sent     # falla de datos: sigue armada
    assert r.arms["BBB"]["status"] == "cancelada" and "arm-x:BBB" in r.sent         # NO del mercado: se cancela
    assert r.arms["CCC"]["status"] == "pendiente"                                  # ya sobre la entrada: no cancela
    assert r.arms["DDD"]["status"] == "pendiente"                                  # sin velas este ciclo: no cancela


def test_partial_bars():
    """La última vela de Yahoo llega a medio llenar: se revisa otra vez completa. Un stop tocado en la segunda mitad de
    ese minuto se avisa; una vela repasada no repite eventos, no marca el +2 % ni el máximo con precios de antes de
    llenarse la límite, y la vuelta a la entrada solo cuenta después de la vela del +2 %."""
    r = runner.Radar(notify=False)
    r.trades, r.arms, r.sent, r.day = {}, {}, set(), DAY.date()
    sig = {"t": "RUN", "score": 70, "plan": {"entry": 10.0, "stop": 9.9, "t1": 10.2, "t2": 10.5, "risk": 1.0}}
    r.trades["RUN"] = tr = runner.new_trade(sig, DAY.date().isoformat(), "10:02", 602)
    t_et = DAY.replace(hour=10, minute=7)
    # 10:01–10:02 son de antes del aviso (la de 10:01 bajó del stop: no cuenta); 10:05 llega a medio llenar
    b1 = bars(DAY, [9.8, 10.0, 10.01, 10.02, 10.03], 50000, start_m=601)
    r._track([], {"RUN": b1}, t_et, "verde")
    assert tr["status"] == "abierta" and tr["m"] == 603 and "stop:RUN" not in r.sent, tr   # las 2 últimas, otra vez
    b2 = b1.copy()
    b2.iloc[-1, b2.columns.get_loc("Low")] = 9.85                         # completa: bajó al stop en la 2.ª mitad
    r._track([], {"RUN": b2}, t_et.replace(minute=8), "verde")
    assert tr["status"] == "stop" and "stop:RUN" in r.sent, tr
    # límite llenada en el retesteo: las velas de antes (más arriba) no son de la posición
    lim = {"t": "LIM", "score": 70, "plan": {"entry": 10.0, "stop": 9.9, "t1": 10.2, "t2": 10.5, "risk": 1.0,
                                              "limit": True, "valid_min": 10}}
    o = runner.new_trade(lim, "d", "10:00", 600)
    path = pd.DataFrame([(10.3, 10.35, 10.25, 10.3), (10.2, 10.22, 9.99, 10.05), (10.05, 10.08, 10.0, 10.06)],
                        columns=["Open", "High", "Low", "Close"])
    path["m"] = [601, 602, 603]
    assert runner.advance(o, path) == ["fill"] and o["fill_m"] == 602 and not o["hit1"], o
    assert runner.advance(o, path) == [] and not o["hit1"] and o["mfe"] < 1, o   # repaso: sin +2 % ni máximo falso
    # +2 % en la vela 605; la 604 (antes) tocó la entrada: al repasarla no cierra la operación en la entrada
    o2 = runner.new_trade(sig, "d", "10:00", 600)
    p2 = pd.DataFrame([(10.0, 10.05, 9.99, 10.04), (10.04, 10.25, 10.03, 10.2)], columns=["Open", "High", "Low", "Close"])
    p2["m"] = [604, 605]
    assert runner.advance(o2, p2) == ["t1"] and o2["t1_m"] == 605
    assert runner.advance(o2, p2) == [] and o2["status"] == "t1", o2


def test_or_waits_for_bars():
    """El rango de apertura se cierra cuando ya hay una vela posterior, no por el reloj (a las 9:35 con Yahoo atrasado
    solo había 4 velas). Si la acción abre tarde (halt), el rango empieza en su primera vela y el nivel no sale NaN."""
    k = metrics.OR_MINUTES
    base = metrics.baseline_curve(five_days(), DAY.date())
    b = bars(DAY, runner_path(k - 1), 40000)                                 # 9:30–9:33: falta la vela de 9:34
    m = metrics.session_metrics(b, 9.6, base, DAY.replace(hour=9, minute=35, second=30))
    assert not m["or_done"], m
    b = bars(DAY, runner_path(k + 1), 40000)                                 # ya llegó la de 9:35
    assert metrics.session_metrics(b, 9.6, base, DAY.replace(hour=9, minute=36))["or_done"]
    late = bars(DAY, list(np.linspace(10.0, 10.6, 20)), 60000, start_m=600)   # abre a las 10:00
    m = metrics.session_metrics(late, 9.6, base, DAY.replace(hour=10, minute=19))
    assert m["or_end"] == 600 + k and m["or_done"] and np.isfinite(m["orh"]) and np.isfinite(m["breakout"]), m
    r = D.decide("LATE", m, ctx())
    assert r["level"] is None or np.isfinite(r["level"]), r


def test_rvol_recent_and_parabolic():
    """RVOL sin la vela a medio llenar; RVOL de los últimos 15 min (solo decide con RVOL15_IN_PLAY > 0); un parabólico
    con noticia fresca y volumen fuerte queda en ESPERA (se sigue para el retroceso) en vez de NO."""
    base = metrics.baseline_curve(five_days(), DAY.date())
    n = 60
    vol = [10000] * 45 + [40000] * 15                                          # volumen nuevo en los últimos 15 min
    m = metrics.session_metrics(bars(DAY, [10.0] * n, vol), 9.9, base, DAY.replace(hour=10, minute=29))
    assert 1.5 < m["rvol"] < 2.0 and m["rvol15"] > 3, m                        # acumulado bajo, reciente alto
    assert D.decide("X", m, ctx())["decision"] == "NO"                         # apagado por defecto
    old = D.RVOL15_IN_PLAY
    try:
        D.RVOL15_IN_PLAY = 3.0
        assert "volumen relativo" not in D.decide("X", m, ctx())["reason"]
    finally:
        D.RVOL15_IN_PLAY = old
    mm = test_compra()
    hot = {**mm, "rvol": 6.0, "chg15": 18.0}
    assert D.decide("X", hot, ctx())["decision"] == "NO"
    r = D.decide("X", hot, ctx(cat={"type": "contract", "age": "fresh", "title": "x"}))
    assert r["decision"] == "ESPERA" and "parabólico" in r["reason"] and r["level"] is None, r


def test_context_retry_and_quotes_backoff():
    """La curva de volumen o el cierre previo que fallaron se vuelven a pedir pasados CTX_RETRY_S (antes nunca); un
    reintento fallido no borra lo bueno. Las cotizaciones v7 descansan 1, 2, 5 y 20 min según los fallos seguidos."""
    from scanner.sources import yahoo
    old_h, old_q, old_t = yahoo.history, yahoo.batch_quotes, runner.time.time
    calls, ok = [], {"v": False}
    daily = pd.DataFrame({"Open": 10.0, "High": 10.5, "Low": 10.0, "Close": 10.2, "Volume": 1e6},
                         index=pd.date_range("2026-06-01", periods=60))

    def history(syms, period="1y", interval="1d", prepost=False):
        calls.append((tuple(syms), period))
        if not ok["v"]:
            return {}
        return {s: (daily if interval == "1d" else five_days()) for s in syms}
    clock = {"t": 1_000_000.0}
    try:
        yahoo.history = history
        runner.time.time = lambda: clock["t"]
        r = runner.Radar(notify=False)
        r.ensure_context(["AAA"], DAY.date())
        assert r.baseline["AAA"] is None and r.prev["AAA"] is None and len(calls) == 2
        r.ensure_context(["AAA"], DAY.date())
        assert len(calls) == 2                                                  # aún no toca reintentar
        clock["t"] += runner.CTX_RETRY_S
        ok["v"] = True
        r.ensure_context(["AAA"], DAY.date())
        assert r.baseline["AAA"] is not None and r.prev["AAA"] == 10.2 and r.atr["AAA"], (r.baseline.get("AAA"), r.prev)
        clock["t"] += runner.CTX_RETRY_S
        r.ensure_context(["AAA"], DAY.date())
        assert len(calls) == 4                                                  # completo: no se vuelve a pedir
        # cotizaciones: descanso creciente y vuelve a 0 al primer éxito
        yahoo.batch_quotes = lambda syms: {}
        waits = []
        for _ in range(5):
            r.q_off_until = 0
            r.quotes(["AAA"])
            waits.append(round(r.q_off_until - clock["t"]))
        assert waits == [60, 120, 300, 1200, 1200], waits
        yahoo.batch_quotes = lambda syms: {s: {"symbol": s} for s in syms}
        r.q_off_until = 0
        assert r.quotes(["AAA"]) and r.q_fail == 0
    finally:
        yahoo.history, yahoo.batch_quotes, runner.time.time = old_h, old_q, old_t


def test_background_context():
    """Con el hilo de contexto, el ciclo no espera noticias, SEC ni opciones: deja en cola lo que falta y usa lo último
    que haya. El hilo lo trae y, si llega una noticia fresca, despierta al ciclo. Un None guardado (sin opciones) cuenta
    como dato y no se vuelve a pedir en cada ciclo."""
    from scanner.sources import other, yahoo
    now = DAY.replace(hour=10, minute=0)
    fetched = []
    patches = {(yahoo, "news"): lambda s, count=10: (fetched.append(("news", s)),
                                                     [{"title": f"{s} wins $50 million contract award", "ts": now.timestamp() - 600}])[1],
               (yahoo, "option_chains"): lambda s, max_days=30: (fetched.append(("opt", s)), ([], False))[1],
               (other, "sec_ticker_map"): lambda: {},
               (halts, "fetch"): lambda: None}
    old = {k: getattr(*k) for k in patches}
    for (mod, name), fn in patches.items():
        setattr(mod, name, fn)
    try:
        r = runner.Radar(notify=False)
        r.bg = True
        c = {"name": "Acme Corp"}
        assert r._enrich("ACME", {"px": 12.0}, c, now.timestamp(), DAY.date(), True, True) is False  # incompleta
        assert fetched == [] and "ACME" in r.enrich_q and not c.get("cat")       # el ciclo no esperó
        r.wake.clear()
        done = r.context_once(now)
        assert done["enriched"] == 1 and ("news", "ACME") in fetched and ("opt", "ACME") in fetched, (done, fetched)
        assert r.wake.is_set() and done["halts"]                                 # noticia fresca: despierta al ciclo
        c = {"name": "Acme Corp"}
        assert r._enrich("ACME", {"px": 12.0}, c, now.timestamp(), DAY.date(), True, True) is True
        assert c["cat"]["type"] == "contract" and c["callVolOI"] is None
        n = len(fetched)
        assert r.opt_ctx("ACME", 12.0) is None and len(fetched) == n            # None en caché: no se vuelve a pedir
    finally:
        for (mod, name), fn in old.items():
            setattr(mod, name, fn)


def test_history_retry_and_lock():
    """yf.download devuelve vacío (sin error) cuando Yahoo limita: el lote se pide una vez más."""
    from scanner.sources import yahoo
    old = (yahoo.yf.download, yahoo.time.sleep)
    calls = []

    def dl(chunk, **k):
        calls.append(1)
        return pd.DataFrame() if len(calls) == 1 else bars(DAY, [10.0, 10.1], 1000)
    try:
        yahoo.yf.download, yahoo.time.sleep = dl, lambda s: None
        out = yahoo.history(["AAA"], period="1d", interval="1m")
        assert len(calls) == 2 and "AAA" in out and len(out["AAA"]) == 2, (calls, out)
    finally:
        yahoo.yf.download, yahoo.time.sleep = old


def test_breaks_outside_lock_and_private_log():
    """El aviso de ruptura se manda fuera del candado (el puente no espera a Telegram) y el registro no guarda el precio
    de IBKR, solo la clave."""
    import logging
    now = DAY.replace(hour=10, minute=0)
    r = runner.Radar(notify=False)
    r.sent = set()
    a = {"level": 50.0, "entry": 50.05, "stop": 49.55, "t1": 51.05, "t2": 52.55, "risk": 1.0, "score": 70, "px": 49.9}
    r.armed = {"AAA": dict(a)}
    free = []
    r._send = lambda text: (free.append(not r.brk_lock.locked()), True)[1]
    seen = []

    class H(logging.Handler):
        def emit(self, rec):
            seen.append(rec.getMessage())
    h = H()
    runner.log.addHandler(h)
    try:
        assert r._check_breaks({"AAA": 50.1234}, "ibkr", now) == ["AAA"]
        import time as _t
        for _ in range(50):
            if free:
                break
            _t.sleep(0.01)
        assert free == [True], free                                            # sin candado al mandar
        assert any("break:AAA:50.00" in x for x in seen) and not any("50.12" in x for x in seen), seen
        assert r._check_breaks({"AAA": 50.2}, "yahoo", now) == []               # una sola vez
    finally:
        runner.log.removeHandler(h)


def test_replay_and_api_limits():
    """El replay recalculado va como mucho cada REPLAY_MIN_S; /api/news pide el token; el puente rechaza un cuerpo
    grande por la cabecera antes de leerlo."""
    import os
    os.environ.update(NO_LOOP="1", NO_NOTIFY="1")
    from fastapi.testclient import TestClient
    from live import app as A
    r = runner.Radar(notify=False)
    r.replay = lambda step=10: {"status": "ok", "n": 1}
    r.replay_async(10, True)
    import time as _t
    for _ in range(100):
        if r.replay_state.get("status") == "ok":
            break
        _t.sleep(0.01)
    assert r.replay_state["status"] == "ok" and r.replay_ts
    runs = []
    r.replay = lambda step=10: (runs.append(1), {"status": "ok"})[1]
    assert "nota" in r.replay_async(10, True) and runs == [], runs              # muy pronto: devuelve el último
    cl = TestClient(A.app)
    os.environ.pop("POSITIONS_TOKEN", None)
    assert cl.get("/api/news/AAPL").status_code == 503
    os.environ["POSITIONS_TOKEN"] = "pos"
    assert cl.get("/api/news/AAPL", headers={"X-Token": "malo"}).status_code == 401
    os.environ["BRIDGE_TOKEN"] = "b"
    big = cl.post("/api/bridge", content=b"x" * (A.BRIDGE_MAX + 1), headers={"X-Token": "b"})
    assert big.status_code == 413, big.status_code
    h = cl.get("/health").json()
    assert "cycle_s" in h and h["context"]["on"] in (True, False), h
    os.environ.pop("BRIDGE_TOKEN", None)
    os.environ.pop("POSITIONS_TOKEN", None)


def test_whale_early():
    """La ballena de la ruptura del rango (9:35–9:40) ya cuenta: basta la mediana de 5 velas previas."""
    vol = [20000] * 7
    vol[6] = 300000
    b = bars(DAY, list(np.linspace(10.0, 10.3, 7)), vol)
    w = metrics.whale_bars(metrics.to_et(b).assign(), 576)
    assert w["buy"] == 1, w


def test_background_needs_sec_before_buy():
    """Con el hilo de contexto, una acción sin noticias ni SEC todavía (nueva o tras un reinicio) que daría COMPRA las
    pide en el acto antes de decidir: la dilución sigue vetando como antes (no sale COMPRA a ciegas)."""
    n = 40
    now = DAY.replace(hour=9, minute=30) + timedelta(minutes=n - 1)
    vol = [40000] * n
    vol[-3] = 400000
    today = {"RUN": bars(DAY, runner_path(n), vol), "SPY": bars(DAY, list(np.linspace(500, 502, n)), 50000),
             "QQQ": bars(DAY, list(np.linspace(400, 402, n)), 50000)}
    old = _fake_sources(today, now)
    from scanner.sources import yahoo
    yahoo.news = lambda s, count=10: [{"title": f"{s} announces $20 million public offering", "ts": now.timestamp() - 1800}]
    try:
        r = runner.Radar(notify=False)
        r.bg = True
        r.trades, r.arms, r.sent = {}, {}, set()
        r.universe, r.universe_ts = ["RUN"], 1e18
        r.halts_ts = runner.time.time()
        r.cycle(now.astimezone(ET))
        row = next(x for x in r.snapshot["rows"] if x["t"] == "RUN")
        assert row["decision"] == "NO" and "dilución" in row["reason"] and "RUN" not in r.trades, (row["decision"], row["reason"])
    finally:
        for (mod, name), fn in old.items():
            setattr(mod, name, fn)


def test_armed_survives_missing_data():
    """Si Yahoo no da velas de una armada pendiente, el vigía rápido y el puente la siguen mirando (no se pierde el ⚡)."""
    r = runner.Radar(notify=False)
    r.trades, r.arms, r.sent, r.day = {}, {}, set(), DAY.date()
    e = 50.0 * 1.001
    row = {"t": "AAA", "decision": "ESPERA", "level": 50.0, "px": 49.8, "chg": 6.0, "score": 70, "reason": "x",
           "plan": {"entry": e, "stop": e * 0.99, "t1": e * 1.02, "t2": e * 1.05, "risk": 1.0}}
    r._arm([row], "verde", "open", 600)
    assert "AAA" in r.armed and r.arms["AAA"]["status"] == "pendiente"
    r._arm([{"t": "AAA", "decision": "NO", "reason": "sin datos de precio", "nodata": True, "score": 0}], "verde", "open", 602)
    assert "AAA" in r.armed                                                   # sigue vigilada
    r._arm([{"t": "AAA", "decision": "NO", "reason": "debajo del VWAP", "score": 0}], "verde", "open", 603)
    assert "AAA" not in r.armed                                               # un NO del mercado sí la saca
    # replay: un resultado sin datos se puede reintentar al minuto (no espera 10)
    r.replay_state, r.replay_ts = {"status": "sin datos todavía"}, runner.time.time() - 61
    import threading as th
    gate = th.Event()
    r.replay = lambda step=10: (gate.wait(2), {"status": "ok"})[1]
    assert r.replay_async(10, False)["status"] == "corriendo"
    gate.set()


if __name__ == "__main__":
    m = test_compra()
    test_vetos(m)
    test_limit_and_cutoff(m)
    test_triggers(m)
    test_strength_acn()
    test_merge()
    test_espera_or()
    test_regime()
    test_halts()
    test_classify()
    test_news_relevance()
    test_spread_sanity()
    test_arm_and_fast()
    test_stop_orders()
    test_track_arms()
    test_break_lag()
    test_day_reset()
    test_memory_and_threads()
    test_pre_list()
    test_positions()
    test_positions_api()
    test_watch_positions()
    test_follow_outside_universe()
    test_bridge_state()
    test_bridge_break()
    test_bridge_universe()
    test_bridge_api()
    test_telegram_state()
    test_no_data_keeps_arms()
    test_partial_bars()
    test_or_waits_for_bars()
    test_rvol_recent_and_parabolic()
    test_context_retry_and_quotes_backoff()
    test_background_context()
    test_history_retry_and_lock()
    test_breaks_outside_lock_and_private_log()
    test_replay_and_api_limits()
    test_whale_early()
    test_background_needs_sec_before_buy()
    test_armed_survives_missing_data()
    snap = test_cycle()
    print("OK · ejemplo:", {k: snap["rows"][0][k] for k in ("t", "decision", "score", "reason", "why", "plan")})
