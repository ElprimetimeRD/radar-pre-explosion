"""Pruebas del puente IBKR (bridge/puente_ibkr.py) con un IBKR falso: sin red, sin TWS y sin órdenes.
Uso: NO_LOOP=1 NO_NOTIFY=1 PYTHONPATH=. python tests/test_bridge.py"""
import asyncio
import http.server
import json
import logging
import math
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bridge"))
import puente_ibkr as P  # noqa: E402


class Ev:
    def __init__(self):
        self.h = []

    def __iadd__(self, f):
        self.h.append(f)
        return self

    def emit(self, *a):
        for f in self.h:
            f(*a)


class FakeScan(list):
    def __init__(self, req_id):
        super().__init__()
        self.reqId, self.updateEvent = req_id, Ev()


class FakeBars(list):
    def __init__(self, req_id, contract):
        super().__init__()
        self.reqId, self.contract, self.updateEvent = req_id, contract, Ev()


class FakeIB:
    """Lo mínimo de ib_async.IB que usa el puente, con sus mañas: sin conexión, pedir datos revienta
    (ConnectionError, como la librería real). placeOrder revienta siempre: el puente nunca debe operar."""

    def __init__(self, scans=None, fail=0):
        self.connected, self.fail = False, fail
        self.errorEvent, self.pendingTickersEvent, self.disconnectedEvent = Ev(), Ev(), Ev()
        self.scans, self.lines, self.calls = scans or {}, {}, []
        self.silent, self.scan_delay = set(), 0.0
        self.active_scans, self.cancelled, self._rid = set(), [], 0
        self.hist, self.hist_fail, self.bar_reqs = {}, set(), {}  # velas: lo que devuelve, las que fallan, las abiertas

    def isConnected(self):
        return self.connected

    async def connectAsync(self, host, port, clientId, timeout, readonly, fetchFields):
        self.calls.append(("connect", readonly))
        if self.fail:
            self.fail -= 1
            raise ConnectionRefusedError()
        self.connected = True

    def reqMarketDataType(self, n):
        self.calls.append(("datos", n))

    async def reqScannerParametersAsync(self):
        return "<ScanParameterResponse>" + "".join(f"<scanCode>{c}</scanCode>" for c in ("TOP_PERC_GAIN", "HOT_BY_VOLUME"))

    def reqScannerSubscription(self, sub):
        if not self.connected:
            raise ConnectionError("Not connected")
        self.calls.append(("scan", sub.scanCode, sub.locationCode, sub.abovePrice, sub.aboveVolume))
        self._rid += 1
        d = FakeScan(self._rid)
        self.active_scans.add(d.reqId)
        if sub.scanCode not in self.silent:
            def fill():
                d.extend(NS(contractDetails=NS(contract=P.Stock(s, "NASDAQ", "USD")))
                         for s in self.scans.get(sub.scanCode, []))
                d.updateEvent.emit(d)
            asyncio.get_running_loop().call_later(self.scan_delay, fill)
        return d

    def cancelScannerSubscription(self, d):
        self.active_scans.discard(d.reqId)
        self.cancelled.append(d.reqId)

    async def qualifyContractsAsync(self, c):
        self.calls.append(("buscar", c.symbol))
        return [None] if c.symbol == "NOPE" else [c]

    def reqMktData(self, c, generic, snapshot, regulatory):
        if not self.connected:
            raise ConnectionError("Not connected")
        assert not snapshot  # streaming (las consultas sueltas cuestan US$0.01 cada una)
        t = NS(contract=c, last=math.nan, bid=math.nan, ask=math.nan, high=math.nan)
        self.lines[c.symbol] = t
        return t

    def cancelMktData(self, c):
        if not self.connected:
            raise ConnectionError("Not connected")
        self.lines.pop(c.symbol, None)

    async def reqHistoricalDataAsync(self, contract, endDateTime, durationStr, barSizeSetting, whatToShow, useRTH,
                                     formatDate=1, keepUpToDate=False, chartOptions=(), timeout=60):
        """Como ib_async: si IBKR da error, avisa por errorEvent y devuelve la lista vacía."""
        if not self.connected:
            raise ConnectionError("Not connected")
        self.calls.append(("velas", contract.symbol, endDateTime, durationStr, barSizeSetting, whatToShow, useRTH,
                           formatDate, keepUpToDate, contract.exchange))
        self._rid += 1
        b = FakeBars(self._rid, contract)
        if contract.symbol in self.hist_fail:
            self.errorEvent.emit(b.reqId, 162, "Historical Market Data Service error message:No market data "
                                 "permissions for NASDAQ STK", contract)
            return b
        b.extend(self.hist.get(contract.symbol, []))
        if keepUpToDate:
            self.bar_reqs[b.reqId] = b
        return b

    def cancelHistoricalData(self, b):
        if not self.connected:
            raise ConnectionError("Not connected")
        self.bar_reqs.pop(b.reqId, None)
        self.calls.append(("cancelar velas", b.contract.symbol))

    def placeOrder(self, *a):
        raise AssertionError("el puente nunca debe mandar órdenes")


class Server:
    """El semáforo falso: guarda lo recibido y, como el de verdad, responde la hora de la última vela que ya tiene de
    cada acción que pide."""

    def __init__(self):
        self.got, self.fail, self.during = [], None, None
        self.resp = {"ok": True, "phase": "open", "armed": {}, "fired": []}
        self.bars: dict[str, dict] = {}

    def __call__(self, url, token, payload):
        if self.during:
            self.during()
        if self.fail:
            raise self.fail
        json.dumps(payload, allow_nan=False)  # lo mismo que hace http_post: nada de NaN
        self.got.append(payload)
        for s, rows in (payload.get("bars") or {}).items():
            self.bars.setdefault(s, {}).update({r[0]: r for r in rows})
        want = self.resp.get("bars")
        if isinstance(want, dict):
            for s in want:
                want[s] = max(self.bars.get(s) or [0])
        return json.loads(json.dumps(self.resp))


def test_config():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "puente.env")
        tok = P.create_env(path)
        cfg = P.read_env(path)
        assert len(tok) >= 40 and cfg["BRIDGE_TOKEN"] == tok and cfg["IB_PORT"] == "4001", cfg
        assert P.create_env(path) == tok                       # no cambia la clave si ya existe
        with open(path, "a", encoding="utf-8") as f:
            f.write("IB_PORT = 7496   # TWS\n")
        assert P.read_env(path)["IB_PORT"] == "7496"
        os.environ["IB_PORT"] = "4002"
        try:
            assert P.read_env(path)["IB_PORT"] == "4002"         # la variable de entorno manda
        finally:
            os.environ.pop("IB_PORT")
        with open(path, "w", encoding="utf-8-sig") as f:        # guardado por el Bloc de notas (con BOM)
            f.write("SEMAFORO_URL=https://x.test\nBRIDGE_TOKEN=abc\n")
        cfg = P.read_env(path)
        assert cfg["SEMAFORO_URL"] == "https://x.test" and cfg["BRIDGE_TOKEN"] == "abc", cfg


def test_http_errors():
    """http_post traduce las respuestas del semáforo a mensajes claros (y nunca deja escapar otra excepción)."""
    codes = iter([200, 401, 503, 500, "html"])

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            code = next(codes)
            self.send_response(200 if code == "html" else code)
            self.end_headers()
            if code == 200:
                self.wfile.write(json.dumps({"ok": True, "echo": body, "tok": self.headers["X-Token"]}).encode())
            elif code == "html":
                self.wfile.write(b"<html>Render: servicio reiniciando</html>")

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/api/bridge"
    try:
        r = P.http_post(url, "clave", {"v": 1})
        assert r["ok"] and r["tok"] == "clave" and r["echo"] == {"v": 1}, r
        for exc, word in ((PermissionError, "no es igual"), (PermissionError, "Falta BRIDGE_TOKEN"),
                          (ConnectionError, "500"), (ConnectionError, "sin conexión")):
            try:
                P.http_post(url, "clave", {"v": 1})
            except exc as e:
                assert word in str(e), e
            else:
                raise AssertionError(f"debió fallar con {exc.__name__}")
        try:
            P.http_post(url, "clave", {"x": float("nan")})
        except ValueError:
            pass
        else:
            raise AssertionError("no debe mandar NaN")
    finally:
        srv.shutdown()
    try:
        P.http_post("http://127.0.0.1:9/api/bridge", "clave", {}, timeout=2)
    except ConnectionError as e:
        assert "sin conexión" in str(e)
    else:
        raise AssertionError("sin servidor debe fallar con ConnectionError")


async def _scenario():
    cfg = dict(P.DEFAULTS, BRIDGE_TOKEN="t", SCAN_EVERY="0", SCAN_TIMEOUT="0.2")
    ib = FakeIB(scans={"TOP_PERC_GAIN": ["AAA", "BBB"], "HOT_BY_VOLUME": ["CCC", "AAA"]}, fail=1)
    srv = Server()
    p = P.Puente(cfg, ib, srv)
    # 1) TWS/IB Gateway cerrado: el semáforo ve "desconectado" y el puente reintenta en 15 s
    assert await p.step() == 15 and ib.calls == [("connect", True)], ib.calls
    assert srv.got[-1]["info"]["ib"] == "desconectado" and "TWS" in srv.got[-1]["info"]["error"], srv.got[-1]
    # 2) Conecta en solo lectura, pide datos en vivo y lanza los escaneos en segundo plano (no frenan nada)
    assert await p.step() == 5 and p.scan_task is not None
    assert ("datos", 1) in ib.calls and p.codes == ["TOP_PERC_GAIN", "HOT_BY_VOLUME"], (ib.calls, p.codes)
    await p.scan_task
    assert p.scan_new and p.wake.is_set() and ib.active_scans == set() and len(ib.cancelled) == 2, ib.cancelled
    assert ("scan", "TOP_PERC_GAIN", "STK.US.MAJOR", 1.0, 100000) in ib.calls
    p.cfg["SCAN_EVERY"] = "999"
    await p.step()
    assert srv.got[-1]["scan"] == {"TOP_PERC_GAIN": ["AAA", "BBB"], "HOT_BY_VOLUME": ["CCC", "AAA"]}, srv.got[-1]
    assert srv.got[-1]["info"]["ib"] == "conectado" and srv.got[-1]["info"]["error"] is None and not p.scan_new
    # 3) El semáforo arma AAA (y una que IBKR no reconoce): precio al instante de AAA, consolidado (SMART)
    srv.resp["armed"] = {"AAA": {"level": 10.0, "entry": 10.01}, "NOPE": {"level": 5.0, "entry": 5.01}}
    p.last_push = -1e9
    assert await p.step() == 1
    assert set(ib.lines) == {"AAA"} and ib.lines["AAA"].contract.exchange == "SMART" and "NOPE" in p.bad, ib.lines
    n_buscar = ib.calls.count(("buscar", "NOPE"))
    await p.step()
    assert ib.calls.count(("buscar", "NOPE")) == n_buscar                # no la busca otra vez a cada segundo
    # 4) Precio bajo el gatillo: se manda una vez; si no cambia, no se repite
    t = ib.lines["AAA"]
    t.last, t.bid, t.ask = 9.99, 9.98, float("nan")
    ib.pendingTickersEvent.emit([t])
    assert not p.wake.is_set()
    await p.step()
    assert srv.got[-1]["quotes"] == {"AAA": {"last": 9.99, "high": None, "bid": 9.98, "ask": None}}, srv.got[-1]
    n = len(srv.got)
    await p.step()
    assert len(srv.got) == n                                    # nada nuevo: no molesta al servidor
    # 5) Cruza el gatillo: despierta el envío inmediato
    t.last = 10.02
    ib.pendingTickersEvent.emit([t])
    assert p.wake.is_set() and "AAA" in p.crossed
    await p.step()
    assert srv.got[-1]["quotes"]["AAA"]["last"] == 10.02 and not p.wake.is_set()
    # 6) Errores de IBKR con su explicación; se limpian cuando vuelven los precios o la conexión
    ib.errorEvent.emit(-1, 10197, "No market data during competing live session", None)
    assert p.error == "sesión en otra plataforma (10197)"
    p.last_push = -1e9
    await p.step()
    assert srv.got[-1]["info"]["error"] == "sesión en otra plataforma (10197)"
    t.last = 10.03
    ib.pendingTickersEvent.emit([t])
    assert p.error is None                                      # volvieron a llegar precios
    ib.errorEvent.emit(-1, 2104, "Market data farm connection is OK", None)
    ib.errorEvent.emit(-1, 1100, "Connectivity between IB and TWS has been lost", None)
    assert p.error == "IBKR sin conexión (1100)"
    ib.errorEvent.emit(-1, 1101, "Connectivity restored - data lost", None)
    assert p.error is None and ib.lines == {} and p.tickers == {} and p.wake.is_set()  # hay que volver a pedirlos
    await p.step()
    assert "AAA" in ib.lines and "AAA" in p.tickers                # …y se vuelven a pedir
    # 7) El semáforo la desarma: se suelta la línea de datos
    srv.resp["armed"] = {}
    p.last_push = -1e9
    await p.step()
    assert ib.lines == {} and p.tickers == {} and p.crossed == set(), (ib.lines, p.crossed)
    # 8) Se cae TWS justo mientras se avisa al semáforo y llega algo nuevo armado: no revienta
    srv.resp["armed"] = {"DDD": {"level": 3.0, "entry": 3.01}}

    def drop():
        ib.connected = False
        ib.disconnectedEvent.emit()
    srv.during = drop
    p.last_push = -1e9
    await p.step()
    srv.during = None
    assert p.state == "desconectado" and p.tickers == {} and not ib.connected
    await p.step()                                              # se reconecta y pide el precio de DDD
    assert ib.connected and "DDD" in ib.lines and ib.calls.count(("connect", True)) == 3, ib.calls
    # 9) Sin TWS y con un cruce pendiente: no gira en vacío (antes reintentaba miles de veces por segundo)
    ib.connected, ib.fail = False, 1
    p.wake.set()
    assert await p.step() == 15 and not p.wake.is_set()
    # 10) Clave equivocada en Render: espera 30 s; sin conexión con Render en sesión: reintenta en 3 s
    srv.fail = PermissionError("El semáforo rechazó la clave")
    p.last_push = -1e9
    assert await p.step() == 30
    srv.fail = ConnectionError("sin conexión")
    p.last_push = -1e9
    assert await p.step() == 3
    srv.fail = None
    # 11) Un escáner que no responde se cancela igual; otro lento no atrasa el aviso de un cruce
    p.cfg.update(SCAN_EVERY="0", SCAN_TIMEOUT="1")
    ib.silent, ib.scan_delay = {"HOT_BY_VOLUME"}, 0.5
    p.last_push = -1e9
    await p.step()
    task = p.scan_task
    assert task is not None and not task.done() and "DDD" in ib.lines
    d = ib.lines["DDD"]
    d.last = 3.02
    ib.pendingTickersEvent.emit([d])
    await p.step()
    assert srv.got[-1]["quotes"]["DDD"]["last"] == 3.02 and not task.done()  # el cruce salió con el escáner corriendo
    await task
    assert p.scan == {"TOP_PERC_GAIN": ["AAA", "BBB"]} and ib.active_scans == set(), (p.scan, ib.active_scans)
    # 12) Fuera de sesión: sin escaneos ni precios, latido cada 30 s
    srv.resp.update(phase="closed", armed={})
    p.last_push = -1e9
    await p.step()
    calls = len(ib.calls)
    assert await p.step() == 30 and len(ib.calls) == calls and ib.lines == {}
    # 13) Un error inesperado no tumba el bucle
    seen = {"n": 0}

    async def flaky():
        seen["n"] += 1
        if seen["n"] == 1:
            p.wake.set()
            raise RuntimeError("falla de prueba")
        p.stopping = True
        return 0
    p.step = flaky
    await asyncio.wait_for(p.run(), 5)
    assert seen["n"] == 2


def test_scenario():
    asyncio.run(_scenario())


def _bar(t, c, v=1000):
    return NS(date=t, open=c, high=round(c + 0.05, 4), low=round(c - 0.05, 4), close=c, volume=v)


def test_bar_row():
    t = datetime(2026, 9, 29, 13, 30, tzinfo=timezone.utc)
    assert P.bar_row(_bar(t, 10.123456, 1234.6)) == [int(t.timestamp()), 10.1235, 10.1735, 10.0735, 10.1235, 1235]
    assert P.bar_row(NS(date=t, open=math.nan, high=1, low=1, close=1, volume=1)) is None
    assert P.bar_row(NS(date=t.date(), open=1, high=1, low=1, close=1, volume=1)) is None    # vela diaria: no
    assert P.bar_row(NS(date=t, open=1, high=1, low=1, close=1, volume=None)) is None
    assert P.bar_row(NS()) is None


def test_quiet_log():
    """El filtro del registro solo saca los dos avisos normales de la librería."""
    q = P.Quiet()

    def rec(name, msg):
        return logging.LogRecord(name, logging.ERROR, __file__, 1, msg, None, None)
    assert not q.filter(rec("ib_async.wrapper", "Error 162, reqId 5: API scanner subscription cancelled: 5"))
    assert not q.filter(rec("ib_async.wrapper", "Error 366, reqId 9: No historical data query found for ticker id:9"))
    assert q.filter(rec("ib_async.wrapper", "Error 162, reqId 9: Historical Market Data Service error message:No "
                                            "market data permissions for NASDAQ STK"))
    assert q.filter(rec("puente", "API scanner subscription cancelled"))


async def _bars_scenario():
    """Velas de 1 min: solo las que pide el semáforo; el día completo una vez y después solo lo nuevo; una historia
    nunca se parte; suelta lo que ya no se pide; errores de IBKR con reintento; el aviso 162 de los escáneres no es un
    error ni ensucia el registro."""
    t4 = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)                     # 4:00 ET
    t0 = int(t4.timestamp())

    def day(c):
        return [_bar(t4 + timedelta(minutes=i), c) for i in range(360)]       # 4:00–9:59 ET
    ib = FakeIB()
    ib.hist = {"AAA": [_bar(t4 - timedelta(hours=12), 9.0)] + day(10.0), "BBB": day(20.0), "CCC": day(30.0),
               "DDD": day(40.0)}
    ib.hist_fail = {"EEE"}
    srv = Server()
    p = P.Puente(dict(P.DEFAULTS, BRIDGE_TOKEN="t", SCAN_EVERY="999", MAX_BARS="4"), ib, srv)
    seen = []
    h = logging.Handler()
    h.emit = lambda rec: seen.append(rec.getMessage())
    P.log.addHandler(h)
    try:
        await p.step()                                                     # conecta; aún no se piden velas
        assert p.bar_subs == {} and "bars" not in srv.got[-1]
        # 1) El semáforo pide AAA y BBB, una que IBKR no encuentra y una sin permiso de datos (162)
        srv.resp.update(bars={"AAA": 0, "BBB": 0, "NOPE": 0, "EEE": 0, "bad sym": 0}, bars_t0=t0)
        p.last_push = -1e9
        await p.step()
        await p.bars_task                                                  # se piden en segundo plano
        assert set(p.bar_subs) == {"AAA", "BBB"} and {"NOPE", "EEE"} <= set(p.bar_bad), (p.bar_subs, p.bar_bad)
        assert ("velas", "AAA", "", "1 D", "1 min", "TRADES", False, 2, True, "SMART") in ib.calls, ib.calls
        assert p.error == P.NO_VELAS and ("cancelar velas", "EEE") in ib.calls  # la vacía no queda abierta
        assert p.wake.is_set() and p.bars_pending()
        # 2) Manda el día completo de las dos (desde las 4:00 ET: la vela de ayer no va)
        await p.step()
        got = srv.got[-1]["bars"]
        assert set(got) == {"AAA", "BBB"} and len(got["AAA"]) == 360 and got["AAA"][0][0] == t0, list(got)
        assert got["AAA"][-1] == [t0 + 359 * 60, 10.0, 10.05, 9.95, 10.0, 1000], got["AAA"][-1]
        assert p.bar_want["AAA"] == t0 + 359 * 60 and not p.bars_pending()
        # 3) Sin cambios no repite nada; cambia la vela que se forma: va solo esa; llega una nueva: van las dos
        p.last_push = -1e9
        await p.step()
        assert "bars" not in srv.got[-1]
        aaa = p.bar_subs["AAA"]
        aaa[-1] = _bar(t4 + timedelta(minutes=359), 10.2, 1500)
        p.last_push = -1e9
        await p.step()
        assert srv.got[-1]["bars"] == {"AAA": [[t0 + 359 * 60, 10.2, 10.25, 10.15, 10.2, 1500]]}, srv.got[-1]["bars"]
        aaa.append(_bar(t4 + timedelta(minutes=360), 10.3, 700))
        p.last_push = -1e9
        await p.step()
        assert [r[0] for r in srv.got[-1]["bars"]["AAA"]] == [t0 + 359 * 60, t0 + 360 * 60], srv.got[-1]["bars"]
        # 4) Cambia lo que pide: suelta BBB; con poco espacio por envío, una historia entera por envío
        old_budget = P.BAR_BUDGET
        P.BAR_BUDGET = 500
        try:
            srv.resp["bars"] = {"AAA": 0, "CCC": 0, "DDD": 0}
            p.last_push = -1e9
            await p.step()
            assert ("cancelar velas", "BBB") in ib.calls and "BBB" not in p.bar_subs
            await p.bars_task
            assert {"CCC", "DDD"} <= set(p.bar_subs) and p.error is None    # volvieron a llegar velas: sin error
            await p.step()
            first = srv.got[-1]["bars"]
            await p.step()
            second = srv.got[-1]["bars"]
            assert set(first) == {"CCC"} and set(second) == {"DDD"} and len(second["DDD"]) == 360, (first, second)
        finally:
            P.BAR_BUDGET = old_budget
        # 5) IBKR corta las velas de una acción: se sueltan y se reintentan a los 2 min
        ib.errorEvent.emit(p.bar_subs["CCC"].reqId, 10182, "Failed to request live updates (disconnected).", None)
        assert "CCC" not in p.bar_subs and p.bar_bad["CCC"] > time.monotonic() + 100
        assert ("cancelar velas", "CCC") in ib.calls
        # 6) El aviso 162 de cada escaneo cancelado: ni error ni línea en el registro
        n = len(seen)
        ib.errorEvent.emit(7, 162, "API scanner subscription cancelled: 7", None)
        assert len(seen) == n and p.error is None, seen[n:]
        # 7) Se cae la conexión: las velas mueren con ella y al volver se piden de nuevo
        ib.connected = False
        ib.disconnectedEvent.emit()
        assert p.bar_subs == {}
        await p.step()
        await p.bars_task
        assert set(p.bar_subs) == {"AAA", "DDD"}, p.bar_subs                # CCC espera sus 2 min
        # 8) El semáforo ya no pide velas (mercado cerrado o IBKR_BARS=0): las suelta todas
        srv.resp["bars"] = {}
        p.last_push = -1e9
        await p.step()
        assert p.bar_subs == {} and ib.calls.count(("cancelar velas", "AAA")) == 1, ib.calls
    finally:
        P.log.removeHandler(h)


def test_bars_scenario():
    asyncio.run(_bars_scenario())


async def _end_to_end():
    """El puente contra el servidor de verdad (FastAPI en memoria): escáner al universo, ⚡ al cruzar y sin publicar
    el precio de IBKR."""
    os.environ.update(NO_LOOP="1", NO_NOTIFY="1", BRIDGE_TOKEN="clave-prueba")
    from datetime import datetime

    from fastapi.testclient import TestClient

    from live import app as A
    from live import runner
    from scanner.util import ET
    old_phase = runner.phase_of
    runner.phase_of = lambda now: "open"
    try:
        cl = TestClient(A.app)
        r = A.radar
        a = {"level": 10.0, "entry": 10.01, "stop": 9.9, "t1": 10.21, "t2": 10.51, "risk": 1.1, "score": 70, "px": 9.95}
        r.sent, r.breaks, r.armed = set(), {}, {"AAA": dict(a)}
        r.arms = {"AAA": runner.new_arm("AAA", a, "2026-09-29", "10:00", 600, 720)}
        sent = []
        real = r._emit
        r._emit = lambda key, text, wait=True, private=False: (sent.append(text), real(key, text, wait, private))

        def post(url, token, payload):
            res = cl.post("/api/bridge", json=payload, headers={"X-Token": token})
            if res.status_code == 401:
                raise PermissionError("clave")
            return res.json()
        ib = FakeIB(scans={"HOT_BY_VOLUME": ["AAA", "ZZZ"]})
        p = P.Puente(dict(P.DEFAULTS, BRIDGE_TOKEN="clave-prueba", SCAN_EVERY="0"), ib, post)
        await p.step()                       # conecta y aprende la fase
        await p.step()                       # lanza el escaneo y se suscribe a AAA
        await p.scan_task
        p.cfg["SCAN_EVERY"] = "999"
        await p.step()                       # manda el escaneo
        assert r.bridge.scan_symbols(5) == ["AAA", "ZZZ"] and "AAA" in ib.lines, (r.bridge.scan, ib.lines)
        t = ib.lines["AAA"]
        t.last = 10.0317
        ib.pendingTickersEvent.emit([t])
        await p.step()
        assert any("⚡ AAA rompe 10.00 ahora (10.03, IBKR)" in x for x in sent), sent
        assert r.breaks["AAA"]["break_src"] == "ibkr"
        r._track_arms([], {}, datetime(2026, 9, 29, 10, 1, tzinfo=ET))
        for path in ("/api/trades", "/health"):
            assert "10.0317" not in cl.get(path).text, path        # el precio de IBKR no se publica
        h = cl.get("/health").json()["bridge"]
        assert h["on"] and h["ib"] == "conectado" and h["scan"] == {"TOP_PERC_GAIN": 0, "HOT_BY_VOLUME": 2}, h
        p.cfg["BRIDGE_TOKEN"] = "otra"
        p.last_push = -1e9
        assert await p.step() == 30          # clave equivocada: avisa y espera
    finally:
        runner.phase_of = old_phase
        A.radar.armed, A.radar.arms = {}, {}
        os.environ.pop("BRIDGE_TOKEN", None)


def test_end_to_end():
    asyncio.run(_end_to_end())


async def _bars_end_to_end():
    """Velas contra el servidor de verdad: el semáforo pide las de su candidata, el puente manda el día y después lo
    nuevo, y /health solo cuenta acciones (ni las velas ni sus precios salen en lo público)."""
    os.environ.update(NO_LOOP="1", NO_NOTIFY="1", BRIDGE_TOKEN="clave-prueba")
    from fastapi.testclient import TestClient

    from live import app as A
    from live import runner
    from scanner.util import ET
    old = (runner.phase_of, runner.IBKR_BARS)
    runner.phase_of = lambda now: "open"
    runner.IBKR_BARS = 3
    r = A.radar
    try:
        cl = TestClient(A.app)
        r.bar_want, r.armed = ["AAA"], {}
        r.bridge.reset_bars()
        e = datetime.now(timezone.utc).astimezone(ET)
        t4 = datetime(e.year, e.month, e.day, 4, 0, tzinfo=ET)          # lo que el servidor manda como bars_t0
        ib = FakeIB()
        ib.hist = {"AAA": [_bar(t4 + timedelta(minutes=i), 10.4321) for i in range(120)]}

        def post(url, token, payload):
            return cl.post("/api/bridge", json=payload, headers={"X-Token": token}).json()
        p = P.Puente(dict(P.DEFAULTS, BRIDGE_TOKEN="clave-prueba", SCAN_EVERY="999"), ib, post)
        await p.step()                                   # conecta y aprende qué velas quiere el semáforo
        assert p.bar_want == {"AAA": 0} and p.bar_t0 == int(t4.timestamp()), (p.bar_want, p.bar_t0)
        await p.bars_task
        await p.step()                                   # manda el día completo
        last = int((t4 + timedelta(minutes=119)).timestamp())
        assert len(r.bridge.bars["AAA"]) == 120 and p.bar_want == {"AAA": last}, p.bar_want
        p.bar_subs["AAA"].append(_bar(t4 + timedelta(minutes=120), 10.4321))
        p.last_push = -1e9
        await p.step()                                   # solo lo nuevo
        assert len(r.bridge.bars["AAA"]) == 121 and len(p.bar_sent) == 1
        assert cl.get("/health").json()["bridge"]["bars"] == 1
        r.snapshot = {**r.snapshot, "bridge": r.bridge.status()}
        for path in ("/health", "/api/signals", "/api/trades"):
            assert "10.4321" not in cl.get(path).text, path      # ni velas ni precios de IBKR en lo público
    finally:
        runner.phase_of, runner.IBKR_BARS = old
        r.bar_want = []
        r.bridge.reset_bars()
        os.environ.pop("BRIDGE_TOKEN", None)


def test_bars_end_to_end():
    asyncio.run(_bars_end_to_end())


if __name__ == "__main__":
    test_config()
    test_http_errors()
    test_bar_row()
    test_quiet_log()
    test_scenario()
    test_bars_scenario()
    test_end_to_end()
    test_bars_end_to_end()
    print("OK · puente IBKR")
