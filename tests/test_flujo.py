"""Pruebas sin red del radar de flujo en penny stocks: python -m tests.test_flujo"""
from __future__ import annotations

import os as _os
_os.environ.setdefault("RIESGO", "normal")  # igual que test_live: umbrales de siempre para el semáforo

import tempfile
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from scanner.util import ET
from live import flujo, halts, runner

DAY = datetime(2026, 10, 7, tzinfo=ET)  # miércoles


def mk(path, vols, start_m=570, day=DAY):
    """Velas de 1 min: la apertura es el cierre anterior y las mechas son fijas (sin azar)."""
    rows, idx, prev = [], [], path[0]
    for i, c in enumerate(path):
        v = vols[i] if hasattr(vols, "__getitem__") else vols
        rows.append((prev, max(prev, c) * 1.002, min(prev, c) * 0.998, c, v))
        idx.append(day.replace(hour=0, minute=0) + timedelta(minutes=start_m + i))
        prev = c
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=pd.DatetimeIndex(idx))


def run_path(n=240, seed=7, start=1.9, end=7.7, spikes=(60, 120, 180, 200), spike_x=8):
    """Corredor tipo SXTC: sube de `start` a `end` con ruido y algunas ráfagas de volumen."""
    rng = np.random.default_rng(seed)
    drift = np.linspace(np.log(start), np.log(end), n)
    noise = np.cumsum(rng.normal(0, 0.012, n))
    noise -= np.linspace(0, noise[-1], n)
    vols = rng.lognormal(12.5, 0.6, n).astype(int)
    for i in spikes:
        if i < n:
            vols[i] *= spike_x
    return np.exp(drift + noise), vols


def at(n, hour=9, minute=30):
    """Hora en que la vela n (0 = 9:30) ya está completa y la siguiente se está formando."""
    return DAY.replace(hour=hour, minute=minute) + timedelta(minutes=n)


def ctx(**k):
    c = {"phase": "open", "name": "Penny Runner", "spread": 0.8, "avg_vol": 1.5e6, "enrich_ok": True}
    c.update(k)
    return c


def green(n=240):
    path, vols = run_path(n)
    m = flujo.metrics(mk(path, vols), 1.25, float(path[-1]), at(n))
    assert m is not None
    return m


# ---------------------------------------------------------------- flujo estimado
def test_flow_split():
    n = 240
    path, vols = run_path(n)
    up = flujo.metrics(mk(path, vols), 1.25, float(path[-1]), at(n))
    fl = up["flow"]
    # conservación: lo repartido entre entradas y salidas es todo el dinero de las velas completas
    b = mk(path, vols).iloc[:-1]
    total = float((((b["High"] + b["Low"] + b["Close"]) / 3) * b["Volume"]).sum())
    assert abs(fl["in"] + fl["out"] - total) <= 2, (fl["in"] + fl["out"], total)
    assert fl["bars"] == n - 1 and fl["ratio"] >= 1.6 and fl["net"] > 20 and fl["vfav"] >= 2, fl
    assert fl["ratio15"] is not None and fl["large"]["buy"] >= 1 and fl["large"]["in"] > fl["large"]["out"], fl
    # el mismo día al revés: las salidas mandan
    down = flujo.metrics(mk(path[::-1].copy(), vols), 8.0, float(path[0]), at(n))["flow"]
    assert down["ratio"] <= 0.65 and down["net"] < -20 and down["vfav"] < 1, down
    # sin tendencia: parejo
    for seed in (1, 2, 3):
        rng = np.random.default_rng(seed)
        flat = 5 * np.exp(np.cumsum(rng.normal(0, 0.004, n)) - np.linspace(0, 0, n))
        f = flujo.metrics(mk(flat, vols), 5.0, float(flat[-1]), at(n))["flow"]
        assert 0.7 <= f["ratio"] <= 1.45, (seed, f["ratio"])
    # el volumen pesa: el mismo recorrido con el dinero cargado en las velas que suben da más entradas que cargado en las que bajan
    r = np.diff(np.log(path), prepend=np.log(path[0]))
    v_up, v_dn = vols * np.where(r > 0, 3, 1), vols * np.where(r < 0, 3, 1)
    a = flujo.metrics(mk(path, v_up), 1.25, float(path[-1]), at(n))["flow"]["ratio"]
    d = flujo.metrics(mk(path, v_dn), 1.25, float(path[-1]), at(n))["flow"]["ratio"]
    assert a > 1.5 * d, (a, d)


def test_large_bars():
    n = 120
    path = np.full(n, 5.0)
    path = path * np.exp(0.0005 * np.sin(np.arange(n)))  # casi plano
    vols = np.full(n, 60000)  # ≈ US$300 k por minuto
    path = path.copy()
    vols[70], vols[90] = 600000, 600000
    path[70:] *= 1.03   # vela ballena que sube
    path[90:] *= 0.97   # vela ballena que baja
    m = flujo.metrics(mk(path, vols), 4.0, float(path[-1]), at(n))
    lg = m["flow"]["large"]
    assert lg["n"] == 2 and lg["buy"] == 1 and lg["sell"] == 1, lg
    assert lg["in"] > 2e6 and lg["out"] > 2e6 and lg["last"]["t"] == "11:00" and lg["last"]["side"] == "sell", lg
    # debajo del piso en dólares no es ballena aunque sea 10× lo normal
    cheap = np.full(n, 0.5)
    v2 = np.full(n, 2000)
    v2[70] = 20000
    lg2 = flujo.metrics(mk(cheap, v2), 0.4, 0.5, at(n))["flow"]["large"]
    assert lg2["n"] == 0 and lg2["last"] is None, lg2


def test_metrics_edges():
    path, vols = run_path(60)
    b = mk(path, vols)
    assert flujo.metrics(None, 1.25, 3.0, at(60)) is None
    assert flujo.metrics(b, None, 3.0, at(60)) is None and flujo.metrics(b, 0, 3.0, at(60)) is None
    assert flujo.metrics(b.iloc[:flujo.MIN_BARS], 1.25, 3.0, at(8)) is None  # pocas velas
    old = mk(path, vols, day=DAY - timedelta(days=1))
    assert flujo.metrics(old, 1.25, 3.0, at(60)) is None  # velas de ayer no cuentan
    # la última vela de Yahoo llega a medio llenar: el flujo la deja fuera, la estructura no
    a = flujo.metrics(b, 1.25, float(path[-1]), at(60))
    b2 = b.copy()
    b2.iloc[-1, b2.columns.get_loc("Volume")] = int(b2["Volume"].iloc[-1]) * 1000
    c = flujo.metrics(b2, 1.25, float(path[-1]), at(60))
    assert a["flow"]["in"] == c["flow"]["in"] and a["flow"]["bars"] == 59 == c["flow"]["bars"]
    assert c["usd_vol"] > a["usd_vol"]
    # precio en vivo de la cotización manda sobre el último cierre
    live = flujo.metrics(b, 1.25, float(path[-1]) * 1.1, at(60))
    assert abs(live["px"] - float(path[-1]) * 1.1) < 1e-9 and live["chg"] > a["chg"]
    assert a["age_min"] == 1 and a["bars"] == 60


# ---------------------------------------------------------------- decisión
def test_decide_green_and_gates():
    m = green()
    r = flujo.decide("RUN", m, ctx())
    assert r["decision"] == "VERDE" and r["falta"] == [] and r["score"] >= 70, (r["decision"], r["falta"], r["score"])
    assert r["rvol"] and r["rvol"] > 30 and r["pend"] is False and r["large"]["buy"] >= 1
    # no entra al radar
    assert flujo.decide("X", {**m, "prev": 6.0}, ctx()) is None            # no era penny
    assert flujo.decide("X", {**m, "chg": 12.0}, ctx()) is None            # no subió lo suficiente
    assert flujo.decide("X", {**m, "px": 0.1}, ctx()) is None              # polvo
    assert flujo.decide("X", {**m, "usd_vol": 1e5}, ctx()) is None         # casi no se negoció
    # ROJO: evitar
    for c, txt in ((ctx(offer30=["424B5 2026-10-02"]), "dilución"), (ctx(offer=True), "dilución"),
                   (ctx(halted={"code": "T12", "time": "10:01:00"}), "halt regulatorio"), (ctx(spread=5.0), "spread")):
        x = flujo.decide("X", m, c)
        assert x["decision"] == "ROJO" and txt in x["reason"], (x["decision"], x["reason"])
    assert flujo.decide("X", {**m, "dd": 35.0}, ctx())["decision"] == "ROJO"
    slow = {**m, "flow": {**m["flow"], "ratio": 1.0, "net": 0.0}}
    assert "sin flujo comprador" in flujo.decide("X", slow, ctx())["reason"]
    turn = {**m, "ext": -2.0, "flow": {**m["flow"], "ratio15": 0.6}}
    assert "se dio vuelta" in flujo.decide("X", turn, ctx())["reason"]
    # AMARILLO: falta una pieza y el aviso dice cuál
    def amarillo(mm, c=None, txt=""):
        x = flujo.decide("X", mm, c or ctx())
        assert x["decision"] == "AMARILLO" and any(txt in f for f in x["falta"]), (x["decision"], x["falta"], txt)
        return x
    amarillo({**m, "flow": {**m["flow"], "ratio": 1.3}}, txt="entradas ÷ salidas")
    amarillo({**m, "flow": {**m["flow"], "ratio15": 1.0}}, txt="últimos 15 min")
    none_buy = {**m["flow"]["large"], "buy": 0, "in": 0}
    amarillo({**m, "flow": {**m["flow"], "large": none_buy}}, txt="sin ballenas compradoras")
    even = {**m["flow"]["large"], "in": 1_000_000, "out": 900_000}
    amarillo({**m, "flow": {**m["flow"], "large": even}}, txt="ballenas parejas")
    amarillo({**m, "px": m["vwap"] * 0.98, "ext": -2.0}, txt="debajo del VWAP")
    amarillo({**m, "dd": 15.0}, txt="del máximo")
    amarillo(m, ctx(avg_vol=m["vol"] / 1.5), txt="lo normal")
    amarillo({**m, "usd_vol": 4e5}, txt="negociados")
    amarillo({**m, "age_min": 10}, txt="sin operaciones")
    amarillo(m, ctx(halted={"code": "LUDP", "time": "10:01:00"}), txt="halt LUDP")
    p = amarillo(m, ctx(enrich_ok=False), txt="dilución")
    assert p["pend"] is True and p["reason"].startswith("falta revisar dilución")
    p2 = amarillo({**m, "dd": 15.0}, ctx(enrich_ok=False), txt="del máximo")
    assert p2["pend"] is False  # hay otra razón además: no vale gastar la consulta inline
    # pre-market: el piso de dinero baja al 30 %
    mid = {**m, "usd_vol": 5e5}
    assert flujo.decide("X", mid, ctx(phase="pre"))["decision"] == "VERDE"
    assert flujo.decide("X", mid, ctx(phase="open"))["decision"] == "AMARILLO"
    # el estante de ofertas (S-3/F-3) resta puntos pero no veta
    assert flujo.decide("X", m, ctx(shelf=True))["score"] == r["score"] - 5
    # más flujo, más fuerza
    weak = {**m, "flow": {**m["flow"], "ratio": 1.6, "ratio15": 1.3, "vfav": 1.2}}
    assert flujo.decide("X", weak, ctx())["score"] < r["score"] - 10


def test_sxtc_panel_numbers():
    """Con las cifras del panel de la app del 7-oct (cierre 1.25 → 7.79, vol. 108.53 M, entradas 29.4 M contra salidas 18.29 M,
    ballenas 0.14 M contra 0.00) las reglas dan VERDE. Vienen de la captura: px, prev, chg, vol, in/out y large. Supuestos
    (no se ven en la captura): VWAP, flujo de 15 min y distancia al máximo."""
    m = {"px": 7.79, "prev": 1.25, "chg": 522.8, "chg5": 3.0, "chg15": 9.0, "vwap": 4.9, "ext": 59.0, "hod": 8.0, "dd": 2.6,
         "vol": 108.53e6, "usd_vol": 4.9e8, "now_m": 13 * 60 + 56, "bars": 500, "age_min": 1, "rng1m": 1.5,
         "flow": {"in": 29.4e6, "out": 18.29e6, "ratio": 1.61, "net": 23.3, "in15": 3e6, "out15": 1.5e6, "ratio15": 2.0,
                  "vfav": 2.0, "sigma": 1.0, "bars": 499,
                  "large": {"n": 3, "buy": 3, "sell": 0, "in": 140000, "out": 0, "ratio": 99.0, "share": 0.3, "rec_buy": 1,
                            "last": None}}}
    r = flujo.decide("SXTC", m, ctx(avg_vol=2e6))
    assert r["decision"] == "VERDE" and r["rvol"] == 54.3, (r["decision"], r["falta"], r["rvol"])
    txt = flujo.alert_text(r)
    assert "🔥 FLUJO SXTC" in txt and "+523%" in txt and "1.6×" in txt and "ESTIMADO" in txt and "Solo aviso" in txt, txt


# ---------------------------------------------------------------- candidatas
def test_pick_and_query():
    import yfinance as yf
    q = lambda px, prev, chg=None, vol=5e6, **k: {"regularMarketPrice": px, "regularMarketPreviousClose": prev,
                                                  "regularMarketChangePercent": chg, "regularMarketVolume": vol, **k}
    quotes = {"SXTC": q(7.79, 1.25, 522.8, 108e6), "BIG": q(30.0, 20.0, 50.0), "SMALL": q(1.05, 1.0, 5.0),
              "NOPRV": q(3.0, None, 100.0), "BRK-B": q(2.0, 1.0, 100.0), "DUST": q(0.05, 0.02, 150.0),
              "MID": q(2.4, 1.5, 60.0, 2e6), "NOCHG": q(2.4, 2.0, None, 1e6)}
    got = flujo.pick(quotes)
    assert got[0] == "SXTC" and set(got) == {"SXTC", "NOPRV", "MID", "NOCHG"}, got  # NOCHG sube 20 %: pasa el filtro flojo
    assert flujo.pick(quotes, n=2) == got[:2]
    assert flujo.pick({}) == []
    d = flujo.screen_query(yf.EquityQuery, yf.EquityQuery("is-in", ["exchange", "NMS", "NCM"])).to_dict()
    ops = {(o["operator"], o["operands"][0]) for o in d["operands"] if o["operator"] != "OR"}
    assert ("GT", "percentchange") in ops and ("GT", "dayvolume") in ops and ("GTE", "intradayprice") in ops, ops
    assert ("LTE", "intradayprice") in ops, ops


# ---------------------------------------------------------------- registro de señales
def test_book():
    with tempfile.TemporaryDirectory() as tmp:
        path = _os.path.join(tmp, "flujo.json")
        bk = flujo.Book(path)
        bk.roll("2026-10-07")
        m = green()
        r = flujo.decide("RUN", m, ctx())
        amar = flujo.decide("YEL", {**m, "dd": 15.0}, ctx())
        new = bk.sign([r, amar], 13 * 60 + 32)
        assert [s["t"] for s in new] == ["RUN"] and new[0]["time"] == "13:32" and new[0]["status"] == "abierta"
        assert bk.sign([r], 13 * 60 + 40) == [], "una sola señal por ticker y día"
        # seguimiento: sube +10 % y baja −4 % después del aviso
        px0 = new[0]["px"]
        after = mk([px0 * 1.04, px0 * 1.10, px0 * 0.96, px0 * 1.02], 50000, start_m=13 * 60 + 33)
        bk.follow({"RUN": after}, DAY.date())
        s = bk.sigs["RUN"]
        assert s["mfe"] >= 10.0 and s["mae"] <= -4.0 and abs(s["last"] - px0 * 1.02) < 1e-9, s
        bk.follow({"RUN": mk([px0 * 1.0], 50000, start_m=13 * 60 + 40)}, DAY.date())
        assert bk.sigs["RUN"]["mfe"] >= 10.0 and bk.sigs["RUN"]["mae"] <= -4.0, "máx y mín nunca retroceden"
        assert flujo.Book(path).sigs["RUN"]["mfe"] == bk.sigs["RUN"]["mfe"], "sobrevive reinicios"
        # tope diario de avisos
        many = [flujo.decide(f"T{i}", m, ctx()) for i in range(20)]
        got = bk.sign(many, 14 * 60)
        assert len(bk.sigs) == flujo.MAX_ALERTS and len(got) == flujo.MAX_ALERTS - 1
        # cierre: resumen y estado
        txt = bk.finish("2026-10-07")
        assert txt.startswith("🔥 Flujo 2026-10-07: %d aviso(s)" % flujo.MAX_ALERTS) and "RUN 13:32" in txt and "cierre" in txt, txt
        # la última vela vista (13:40) cotizaba al precio del aviso: el cierre se calcula con ese último precio, no con el máximo
        assert bk.sigs["RUN"]["status"] == "cierre" and abs(bk.sigs["RUN"]["close_pct"]) < 1e-6, bk.sigs["RUN"]
        assert bk.finish("2026-10-08") is None
        # el día siguiente archiva y limpia
        bk.roll("2026-10-08")
        assert bk.sigs == {} and "2026-10-07" in bk.history and len(bk.history["2026-10-07"]) == flujo.MAX_ALERTS
        assert bk.hist_listing()[0]["day"] == "2026-10-07"
        assert flujo.Book(path).day == "2026-10-08"


# ---------------------------------------------------------------- ciclo completo del semáforo con el radar conectado
def _sources(today, quotes, now, hist_calls, quote_calls, sec=None, subs=None):
    """Fuentes falsas (sin red). Devuelve lo original para restaurarlo."""
    from scanner.sources import other, yahoo
    hist5 = {s: pd.concat([mk([10.0] * 390, 10000, day=DAY - timedelta(days=k)) for k in range(1, 5)]) for s in ("RUN", "SPY", "QQQ")}
    daily = pd.DataFrame({"Open": 10.0, "High": 10.5, "Low": 10.0, "Close": 10.2, "Volume": 1e6},
                         index=pd.date_range("2026-06-01", periods=60))

    def history(syms, period="1y", interval="1d", prepost=False):
        hist_calls.append((tuple(syms), period, interval))
        if interval == "1d":
            return {s: daily for s in syms}
        return {s: (hist5 if period == "5d" else today)[s] for s in syms if s in (hist5 if period == "5d" else today)}

    def batch_quotes(syms):
        quote_calls.append(tuple(syms))
        return {s: quotes[s] for s in syms if s in quotes}

    patches = {(yahoo, "history"): history, (yahoo, "batch_quotes"): batch_quotes,
               (yahoo, "news"): lambda s, count=10: [{"title": f"{s} update", "ts": now.timestamp() - 1800, "url": None}],
               (yahoo, "option_chains"): lambda s, max_days=30: ([], False),
               (other, "sec_ticker_map"): lambda: sec or {},
               (other, "sec_submissions"): lambda cik: (subs or {}).get(cik),
               (halts, "fetch"): lambda: None}
    old = {k: getattr(*k) for k in patches}
    for (mod, name), fn in patches.items():
        setattr(mod, name, fn)
    return old


def _radar(tmp, now):
    r = runner.Radar(notify=False)
    r.trades, r.arms, r.sent = {}, {}, set()
    r.universe, r.universe_ts = ["RUN"], 1e18
    r.flujo = flujo.Book(_os.path.join(tmp, "flujo.json"))
    r.flujo_cand = ["PENNY"]
    msgs = []
    r._emit = lambda key, text, wait=True, private=False, markup=None: msgs.append((key, text))
    return r, msgs


def _world(n=40):
    """RUN (un corredor normal del semáforo) y PENNY (de US$1.25 a ~3.4: +170 %, compras fuertes, ráfagas de volumen)."""
    path, vols = run_path(n, seed=11, start=1.9, end=3.4, spikes=(25, 33))
    vols = (vols * 4).astype(int)
    run_p = list(np.linspace(10.0, 10.3, 8)) + list(np.linspace(10.3, 10.05, 7)) + list(10.3 + 0.012 * np.arange(n - 15))
    today = {"RUN": mk(run_p[:n], 40000), "PENNY": mk(path, vols),
             "SPY": mk(list(np.linspace(500, 502, n)), 50000), "QQQ": mk(list(np.linspace(400, 402, n)), 50000)}
    q = lambda s, prev, **k: {"symbol": s, "regularMarketPrice": float(today[s]["Close"].iloc[-1]), "regularMarketPreviousClose": prev,
                              "quoteType": "EQUITY", "shortName": s, **k}
    quotes = {"RUN": q("RUN", 9.6), "SPY": q("SPY", 499.0), "QQQ": q("QQQ", 399.0),
              "PENNY": q("PENNY", 1.25, bid=float(path[-1]) * 0.998, ask=float(path[-1]) * 1.002, averageDailyVolume10Day=2e5,
                         marketCap=1.2e7, shortName="Penny Runner Inc")}
    return today, quotes, path


def test_cycle_flujo():
    n = 40
    now = at(n - 1).astimezone(ET)  # 10:09
    today, quotes, path = _world(n)
    assert flujo.ON
    with tempfile.TemporaryDirectory() as tmp:
        hist, qc = [], []
        old = _sources(today, quotes, now, hist, qc)
        try:
            r, msgs = _radar(tmp, now)
            r.cycle(now)
            snap = r.snapshot
            assert snap["status"] == "ok" and [x["t"] for x in snap["rows"]] == ["RUN"], [x["t"] for x in snap["rows"]]
            fl = snap["flujo"]
            assert fl["status"] == "ok" and fl["counts"] == {"VERDE": 1, "AMARILLO": 0, "ROJO": 0}, fl
            row = fl["rows"][0]
            assert row["t"] == "PENNY" and row["decision"] == "VERDE" and row["prev"] == 1.25 and row["chg"] > 100, row
            assert row["ratio"] >= flujo.RATIO_GO and row["large"]["buy"] >= 1 and row["rvol"] > 3, row
            assert [k for k, _ in msgs if k.startswith("flujo")] == ["flujo:PENNY"], msgs
            text = dict(msgs)["flujo:PENNY"]
            assert text.startswith("🔥 FLUJO PENNY") and "Solo aviso" in text, text
            assert len(fl["sigs"]) == 1 and fl["sigs"][0]["t"] == "PENNY" and fl["cfg"]["ratio"] == flujo.RATIO_GO
            # PENNY se descargó junto con lo del semáforo, pero el replay no la ve
            assert any("PENNY" in c[0] and c[2] == "1m" and c[1] == "1d" for c in hist), hist
            assert "PENNY" not in r.last_bars and "RUN" in r.last_bars
            assert not any(c[1] == "5d" and "PENNY" in c[0] for c in hist), "no pide 5 días de velas de las extra"
            # el semáforo sigue haciendo lo suyo: JSON estricto de toda la foto
            import json
            from live.app import clean
            json.dumps(clean(snap), allow_nan=False)
            # segundo ciclo, 3 min después: sin aviso repetido y con seguimiento de máximo y mínimo
            px0 = r.flujo.sigs["PENNY"]["px"]
            today["PENNY"] = pd.concat([today["PENNY"], mk([px0 * 1.06, px0 * 1.09, px0 * 0.97], 900000, start_m=570 + n)])
            quotes["PENNY"] = {**quotes["PENNY"], "regularMarketPrice": px0 * 0.97}
            r.flujo_cand = []  # ya no sale en la pantalla de Yahoo: el aviso de hoy se sigue midiendo igual
            r.cycle(now + timedelta(minutes=3))
            assert [k for k, _ in msgs if k.startswith("flujo")] == ["flujo:PENNY"], "un solo aviso por ticker y día"
            sg = r.flujo.sigs["PENNY"]
            assert sg["mfe"] > 5 and sg["mae"] <= 0, sg
            # cierre: resumen una sola vez
            r._flujo_eod(at(0, 16, 5).astimezone(ET))
            r._flujo_eod(at(0, 16, 6).astimezone(ET))
            assert [k for k, _ in msgs if k == "flujo-eod:2026-10-07"] == ["flujo-eod:2026-10-07"], msgs
            assert r.flujo.sigs["PENNY"]["status"] == "cierre"
            # el cierre del mercado conserva el radar en la foto
            r.cycle(at(0, 16, 30).astimezone(ET))
            assert r.snapshot["status"] == "mercado cerrado" and r.snapshot["flujo"]["status"] == "ok"
        finally:
            for (mod, name), fn in old.items():
                setattr(mod, name, fn)


def test_cycle_flujo_dilution_and_isolation():
    n = 40
    now = at(n - 1).astimezone(ET)
    today, quotes, path = _world(n)
    filing = {"filings": {"recent": {"form": ["424B5"], "filingDate": [(now.date() - timedelta(days=3)).isoformat()]}}}
    with tempfile.TemporaryDirectory() as tmp:
        hist, qc = [], []
        old = _sources(today, quotes, now, hist, qc, sec={"PENNY": 4242}, subs={4242: filing})
        try:
            # una oferta de acciones reciente en la SEC la pone ROJO y no hay aviso
            r, msgs = _radar(tmp, now)
            r.cycle(now)
            row = r.snapshot["flujo"]["rows"][0]
            assert row["decision"] == "ROJO" and "dilución" in row["reason"] and "424B5" in row["reason"], row
            assert not msgs and r.flujo.sigs == {}, msgs
            # si el radar falla, el semáforo sigue entero
            r2, msgs2 = _radar(tmp, now)
            boom = flujo.metrics
            flujo.metrics = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("falla de prueba"))
            try:
                r2.cycle(now)
            finally:
                flujo.metrics = boom
            assert r2.snapshot["status"] == "ok" and r2.snapshot["rows"][0]["t"] == "RUN", r2.snapshot["status"]
            assert r2.snapshot["flujo"]["status"] == "error" and "falla de prueba" in r2.snapshot["flujo"]["error"]
            # apagado con FLUJO=0: no baja ni calcula nada
            r3, msgs3 = _radar(tmp, now)
            hist.clear()
            qc.clear()
            flujo.ON = False
            try:
                r3.cycle(now)
            finally:
                flujo.ON = True
            assert r3.snapshot["status"] == "ok" and r3.snapshot["flujo"]["status"] == "sin correr"
            assert hist and qc, "el semáforo sí descargó lo suyo"
            assert not any("PENNY" in c[0] for c in hist) and not any("PENNY" in c for c in qc), (hist, qc)
            # sin datos de la acción: se cuenta, no rompe
            r4, _ = _radar(tmp, now)
            today.pop("PENNY")
            r4.cycle(now)
            assert r4.snapshot["flujo"]["sin_datos"] == 1 and r4.snapshot["flujo"]["rows"] == []
        finally:
            for (mod, name), fn in old.items():
                setattr(mod, name, fn)


def test_universe_hook():
    """_refresh_universe arma la lista del radar con su pantalla; si la pantalla falla, el universo del semáforo sigue."""
    from scanner.sources import yahoo
    now = at(30).astimezone(ET)
    pantalla = {"quotes": [{"symbol": "PENNY", "regularMarketPrice": 3.4, "regularMarketPreviousClose": 1.25,
                            "regularMarketChangePercent": 172.0, "regularMarketVolume": 9e6, "quoteType": "EQUITY"},
                           {"symbol": "BIG", "regularMarketPrice": 50.0, "regularMarketPreviousClose": 40.0,
                            "regularMarketChangePercent": 25.0, "regularMarketVolume": 9e6, "quoteType": "EQUITY"}]}
    calls = []

    def screen(q, size=100, sortField=None, sortAsc=False, count=None):
        calls.append(q if not isinstance(q, str) else q)
        return pantalla if not isinstance(q, str) else {"quotes": []}

    old = (yahoo.yf.screen, yahoo.batch_quotes)
    yahoo.yf.screen, yahoo.batch_quotes = screen, lambda syms: {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            r, _ = _radar(tmp, now)
            r._refresh_universe(now, "open", {})
            assert r.flujo_cand == ["PENNY"] and "PENNY" in r.flujo_q, (r.flujo_cand, r.flujo_q.keys())
            assert any(not isinstance(c, str) for c in calls) and len(calls) >= 5
            # la pantalla del radar falla: el resto no se entera
            def boom(q, size=100, sortField=None, sortAsc=False, count=None):
                if isinstance(q, str):
                    return {"quotes": []}
                raise RuntimeError("yahoo cayó")
            yahoo.yf.screen = boom
            r._refresh_universe(now, "open", {})
            assert r.universe and r.flujo_cand == ["PENNY"] and "PENNY" in r.flujo_q, (r.universe, r.flujo_cand)
            # y cuando la pantalla vuelve sin ella, deja de ser candidata
            yahoo.yf.screen = lambda q, size=100, sortField=None, sortAsc=False, count=None: {"quotes": []}
            r._refresh_universe(now, "open", {})
            assert r.flujo_cand == [], r.flujo_cand
    finally:
        yahoo.yf.screen, yahoo.batch_quotes = old


def test_api_health_and_page():
    """/health y /api/signals traen el radar de flujo; la página trae su pestaña y no repite la declaración de `pc`
    (un `const pc` junto a un `function pc` dejaba la página en «Cargando…» sin mostrar nada)."""
    import re
    _os.environ.update(NO_LOOP="1", NO_NOTIFY="1")
    from fastapi.testclient import TestClient
    from live import app as A
    cl = TestClient(A.app)
    A.radar.flujo = flujo.Book(None)
    h = cl.get("/health").json()["flujo"]
    assert h == {"on": True, "status": "sin correr", "error": None, "counts": {}, "sigs": 0}, h
    A.radar.flujo.snap = {**A.radar.flujo.snap, "status": "error", "error": "ValueError: x"}
    assert cl.get("/health").json()["flujo"]["status"] == "error"
    A.radar.snapshot = {**A.radar.snapshot, "flujo": {"status": "ok", "rows": [], "counts": {"VERDE": 0}}}
    assert cl.get("/api/signals").json()["flujo"]["status"] == "ok"
    html = cl.get("/").text
    assert 'id="vflu"' in html and "Flujo penny" in html and "renderFlu()" in html
    assert not (re.search(r"const pc\s*=", html) and re.search(r"function pc\s*\(", html)), "pc declarada dos veces"


if __name__ == "__main__":
    test_flow_split()
    test_large_bars()
    test_metrics_edges()
    test_decide_green_and_gates()
    test_sxtc_panel_numbers()
    test_pick_and_query()
    test_book()
    test_cycle_flujo()
    test_cycle_flujo_dilution_and_isolation()
    test_universe_hook()
    test_api_health_and_page()
    print("OK · flujo")
