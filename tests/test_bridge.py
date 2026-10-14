"""Pruebas del puente IBKR (bridge/puente_ibkr.py) con un IBKR falso: sin red, sin TWS y sin órdenes.
Uso: NO_LOOP=1 NO_NOTIFY=1 PYTHONPATH=. python tests/test_bridge.py"""
import os as _os
_os.environ.setdefault("RIESGO", "normal")  # pruebas con los umbrales de siempre; el perfil alto tiene su prueba
import asyncio
import http.server
import json
import math
import os
import sys
import tempfile
import threading
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


class FakeIB:
    """Lo mínimo de ib_async.IB que usa el puente, con sus mañas: sin conexión, pedir datos revienta
    (ConnectionError, como la librería real). placeOrder revienta siempre: el puente nunca debe operar."""

    def __init__(self, scans=None, fail=0):
        self.connected, self.fail = False, fail
        self.errorEvent, self.pendingTickersEvent, self.disconnectedEvent = Ev(), Ev(), Ev()
        self.scans, self.lines, self.calls = scans or {}, {}, []
        self.silent, self.scan_delay = set(), 0.0
        self.active_scans, self.cancelled, self._rid = set(), [], 0
        self.generic = {}

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
        self.generic[c.symbol] = generic
        t = NS(contract=c, last=math.nan, bid=math.nan, ask=math.nan, high=math.nan, ticks=[], rtVolume=math.nan)
        self.lines[c.symbol] = t
        return t

    def cancelMktData(self, c):
        if not self.connected:
            raise ConnectionError("Not connected")
        self.lines.pop(c.symbol, None)

    def placeOrder(self, *a):
        raise AssertionError("el puente nunca debe mandar órdenes")


class Server:
    def __init__(self):
        self.got, self.fail, self.during = [], None, None
        self.resp = {"ok": True, "phase": "open", "armed": {}, "fired": []}

    def __call__(self, url, token, payload):
        if self.during:
            self.during()
        if self.fail:
            raise self.fail
        json.dumps(payload, allow_nan=False)  # lo mismo que hace http_post: nada de NaN
        self.got.append(payload)
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
    assert srv.got[-1]["quotes"] == {"AAA": {"last": 9.99, "high": None, "bid": 9.98, "ask": None, "vol": None}}, srv.got[-1]
    assert ib.generic["AAA"] == "233"                           # con RT Volume: las operaciones una por una
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


def test_side_of():
    """Lee-Ready: contra el bid/ask de ese momento; en el medio (o sin cotización), por el tick; si no cambió, el lado de
    la operación anterior."""
    f = P.side_of
    assert f(10.05, 10.00, 10.05, None, 0) == 1 and f(10.06, 10.00, 10.05, None, 0) == 1      # en el ask o arriba
    assert f(10.00, 10.00, 10.05, None, 0) == -1 and f(9.99, 10.00, 10.05, None, 0) == -1     # en el bid o abajo
    assert f(10.04, 10.00, 10.05, None, 0) == 1 and f(10.01, 10.00, 10.05, None, 0) == -1    # arriba/abajo del medio
    assert f(10.025, 10.00, 10.05, 10.02, -1) == 1 and f(10.025, 10.00, 10.05, 10.03, 1) == -1  # medio: el tick
    assert f(10.025, 10.00, 10.05, 10.025, -1) == -1                                          # sin cambio: el anterior
    assert f(5.0, None, None, 4.9, 0) == 1 and f(5.0, 5.1, 5.0, None, 0) == 0                  # sin bid/ask válido


def _tick(tt, price, size=0.0, minute=29_000_000):
    from datetime import datetime, timezone
    return NS(tickType=tt, price=price, size=size, time=datetime.fromtimestamp(minute * 60 + 7, timezone.utc))


async def _flow():
    """Flujo real: lo que pide el semáforo se sigue con RT Volume; cada operación se clasifica contra el bid/ask de ese
    momento y se suma a su minuto; se manda lo que cambió y se suelta al confirmarse; la unidad la ajusta el semáforo."""
    M = 29_000_000
    cfg = dict(P.DEFAULTS, BRIDGE_TOKEN="t", SCANS="", MAX_LINES="3", LARGE_USD="25000")
    ib = FakeIB()
    srv = Server()
    srv.resp = {"ok": True, "phase": "open", "armed": {"ARM": {"level": 5.0, "entry": 5.01}}, "fired": [],
                "follow": ["BIO", "PEN", "ARM", "XTRA", "ba d"]}
    p = P.Puente(cfg, ib, srv)
    await p.step()                                   # conecta y aprende qué seguir
    await p.step()
    assert list(p.tickers) == ["ARM", "BIO", "PEN"] and p.follow == ["BIO", "PEN", "ARM", "XTRA"], (p.tickers, p.follow)
    assert all(g == "233" for g in ib.generic.values())             # tope de líneas: la armada primero, luego el orden
    t = ib.lines["BIO"]
    t.last, t.bid, t.ask, t.rtVolume = 2.00, 1.99, 2.01, 1_000_000
    t.ticks = [_tick(1, 1.99), _tick(2, 2.01, minute=M),
               _tick(48, 2.01, 100, M),                  # en el ask: compra
               _tick(48, 1.99, 300, M),                  # en el bid: venta
               _tick(48, 2.00, 50, M),                   # justo en el medio, subió desde 1.99: compra
               _tick(1, 2.00), _tick(2, 2.02),
               _tick(48, 2.02, 20000, M),                # en el ask y US$40 k: compra grande
               _tick(48, 2.01, 10, M + 1)]               # minuto siguiente, medio, bajó: venta
    ib.pendingTickersEvent.emit([t])
    r0, r1 = p.flow["BIO"][M], p.flow["BIO"][M + 1]
    assert r0[4] == 20450 and r0[5] == 4 and abs(r0[6] - (201 + 100 + 40400)) < 1e-6 and abs(r0[7] - 597) < 1e-6, r0
    assert r0[9] == 40400 and r0[11] == 1 and r0[12] == 0 and r0[13] == 40400 and r0[14] == 1 and r0[15] == 1.0, r0
    assert (r0[0], r0[1], r0[2], r0[3]) == (2.01, 2.02, 1.99, 2.02) and abs(r1[7] - 20.1) < 1e-9 and r1[5] == 1, (r0, r1)
    p.last_push = -1e9
    await p.step()
    sent = srv.got[-1]
    rows = sent["flow"]["BIO"]
    assert [r[0] for r in rows] == [M, M + 1] and len(rows[0]) == len(P.FLOW_COLS), rows
    assert rows[0][P.FLOW_COLS.index("bbuy")] == 40400 and rows[0][P.FLOW_COLS.index("u")] == 1.0, rows[0]
    assert sent["quotes"]["BIO"]["vol"] == 1_000_000 and sent["info"]["large_usd"] == 25000, sent
    assert p.flow_ver == {} and "flow" not in (await _push_again(p, srv))        # confirmado: no se repite
    # Llega otra operación al minuto M+1 mientras tanto: solo ese minuto viaja otra vez (completo)
    t.ticks = [_tick(48, 2.03, 100, M + 1)]
    ib.pendingTickersEvent.emit([t])
    p.last_push = -1e9
    await p.step()
    assert [r[0] for r in srv.got[-1]["flow"]["BIO"]] == [M + 1] and srv.got[-1]["flow"]["BIO"][0][6] == 2, srv.got[-1]
    # El semáforo confirma que IBKR manda lotes de 100: lo nuevo se decide con esa unidad
    srv.resp["unit"] = 100
    p.last_push = -1e9
    await p.step()
    assert p.unit == 100.0
    t.ticks = [_tick(48, 2.05, 200, M + 2)]          # 200 lotes = 20 000 acciones = US$41 k: grande
    ib.pendingTickersEvent.emit([t])
    assert p.flow["BIO"][M + 2][15] == 100.0 and p.flow["BIO"][M + 2][11] == 1, p.flow["BIO"][M + 2]
    # Una acción que IBKR rechaza (sin suscripción para su bolsa) queda fuera 10 min y libera la línea
    ib.errorEvent.emit(7, 10089, "Requested market data requires additional subscription", P.Stock("PEN", "SMART", "USD"))
    assert "PEN" not in p.tickers and "PEN" in p.bad and p.error == "falta suscripción para la API (10089)", p.error
    await p.step()
    assert "PEN" not in p.tickers and "XTRA" in p.tickers, p.tickers  # la línea libre la toma la siguiente
    # Tope de líneas de IBKR (101): sigue menos y vuelve a pedir en orden
    p.max_lines = 10
    ib.errorEvent.emit(9, 101, "Max number of tickers has been reached", None)
    assert p.max_lines == 5 and p.tickers == {} and p.wake.is_set()
    # Minutos viejos se sueltan (20 h)
    p.prune(M + 2 + P.FLOW_KEEP_MIN + 1)
    assert "BIO" not in p.flow and all(k[0] != "BIO" for k in p.flow_ver)
    # Un semáforo viejo (sin "follow") no cambia la lista; FLOW=0 vuelve a solo las armadas
    srv.resp.pop("follow")
    p.last_push = -1e9
    await p.step()
    assert p.follow == ["BIO", "PEN", "ARM", "XTRA"]
    q = P.Puente(dict(cfg, FLOW="0"), FakeIB(), srv)
    srv.resp["follow"] = ["BIO"]
    await q.step()
    await q.step()
    assert list(q.tickers) == ["ARM"] and q.ib.generic["ARM"] == "", q.tickers


async def _push_again(p, srv):
    n = len(srv.got)
    p.last_push = -1e9
    await p.step()
    return srv.got[-1] if len(srv.got) > n else {}


def test_flow():
    asyncio.run(_flow())


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
        r._emit = lambda key, text, wait=True, private=False, markup=None: (sent.append(text), real(key, text, wait, private, markup))

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
        # Flujo real: el semáforo pide seguir BIO; sus operaciones llegan clasificadas y el semáforo las suma
        import time as _time
        r.ib_follow = ["BIO"]
        p.last_push = -1e9
        await p.step()                       # recibe la lista
        await p.step()                       # se suscribe
        assert "BIO" in ib.lines and ib.generic["BIO"] == "233", ib.lines
        mnow = int(_time.time() // 60)
        b = ib.lines["BIO"]
        b.last, b.bid, b.ask, b.rtVolume = 4.0, 3.99, 4.01, 500
        b.ticks = [_tick(1, 3.99, minute=mnow), _tick(2, 4.01, minute=mnow), _tick(48, 4.01, 10000, mnow),
                   _tick(48, 3.99, 2000, mnow)]
        ib.pendingTickersEvent.emit([b])
        p.last_push = -1e9
        await p.step()
        f = r.bridge.flow_stats("BIO")
        assert f and f["in"] == 40100 and f["out"] == 7980 and f["large"]["buy"] == 1 and f["src"] == "ibkr", f
        assert r.bridge.quote("BIO")["bid"] == 3.99 and cl.get("/health").json()["bridge"]["flow"] == 1
        for path in ("/api/signals", "/health", "/api/trades"):
            assert "3.99" not in cl.get(path).text and "4.01" not in cl.get(path).text, path
        p.cfg["BRIDGE_TOKEN"] = "otra"
        p.last_push = -1e9
        assert await p.step() == 30          # clave equivocada: avisa y espera
    finally:
        runner.phase_of = old_phase
        A.radar.armed, A.radar.arms, A.radar.ib_follow = {}, {}, []
        os.environ.pop("BRIDGE_TOKEN", None)


def test_end_to_end():
    asyncio.run(_end_to_end())


if __name__ == "__main__":
    test_config()
    test_http_errors()
    test_scenario()
    test_side_of()
    test_flow()
    test_end_to_end()
    print("OK · puente IBKR")
