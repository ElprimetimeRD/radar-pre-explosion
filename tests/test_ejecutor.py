"""Pruebas del ejecutor paper (bridge/ejecutor_paper.py) con un IBKR falso: candado, orden con sus dos hijas, límites,
llenados, salidas, vencimientos, llenados que llegan tarde, posición sin stop, ventas de más, cierre de las 15:55,
/cerrar, rechazos y reinicios. Sin red y sin IBKR. Uso: PYTHONPATH=. python tests/test_ejecutor.py"""
import copy
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bridge"))
import ejecutor_paper as E  # noqa: E402

if E.Order is None:  # sin ib_async instalado: lo mínimo de Order y Stock
    class _Obj:
        def __init__(self, *a, **kw):
            self.orderId, self.conId, self.orderRef, self.auxPrice, self.tif, self.goodTillDate = 0, 0, "", None, "", ""
            self.parentId, self.ocaGroup, self.transmit = 0, "", True
            if a:
                self.symbol, self.exchange, self.currency = a
            self.__dict__.update(kw)
    E.Order = E.Stock = _Obj

NY = ZoneInfo("America/New_York")
T0 = datetime(2026, 10, 5, 10, 0, tzinfo=NY).timestamp()   # lunes 10:00 ET


def at(h, m):
    return datetime(2026, 10, 5, h, m, tzinfo=NY).timestamp()


class Ev:
    def __init__(self):
        self.h = []

    def __iadd__(self, f):
        self.h.append(f)
        return self

    def emit(self, *a):
        for f in self.h:
            f(*a)


class FakeIB:
    """Lo que usa el ejecutor de ib_async.IB, con sus mañas: desde un manejador de eventos no se puede esperar
    (sleep revienta, como la librería real sin nest_asyncio)."""
    def __init__(self, cuentas=("DUR233329",)):
        self.cuentas, self.conn, self.in_handler = list(cuentas), False, False
        self.errorEvent, self.disconnectedEvent = Ev(), Ev()
        self.placed, self.cancels, self.connects = [], [], []
        self._trades, self._fills, self.next_id = {}, [], 100

    def connect(self, host, port, clientId, timeout):
        self.connects.append((host, port, clientId))
        self.conn = True

    def isConnected(self):
        return self.conn

    def managedAccounts(self):
        return self.cuentas

    def disconnect(self):
        self.conn = False

    def qualifyContracts(self, c):
        if c.symbol != "NOPE":
            c.conId = 1000 + len(c.symbol)
        return [c]

    def placeOrder(self, c, o):
        if not o.orderId:
            o.orderId = self.next_id
            self.next_id += 1
        self.placed.append((c, copy.copy(o)))
        tr = self._trades.get(o.orderId)
        if tr is None:
            self._trades[o.orderId] = NS(order=o, contract=c, orderStatus=NS(status="Submitted"))
        else:
            tr.order = o
        return self._trades[o.orderId]

    def cancelOrder(self, o):
        self.cancels.append(o.orderId)
        tr = self._trades.get(o.orderId)
        if tr and tr.orderStatus.status != "Filled":
            tr.orderStatus.status = "Cancelled"

    def trades(self):
        return list(self._trades.values())

    def reqOpenOrders(self):
        return [t for t in self._trades.values() if t.orderStatus.status in E.ACTIVOS]

    def fills(self):
        return list(self._fills)

    def sleep(self, s):
        if self.in_handler:
            raise RuntimeError("This event loop is already running")

    # para las pruebas
    def fill(self, order_id, qty, px):
        tr = self._trades[order_id]
        self._fills.append(NS(contract=tr.contract, execution=NS(
            orderId=order_id, orderRef=tr.order.orderRef, shares=qty, price=px,
            side="BOT" if tr.order.action == "BUY" else "SLD")))
        done = sum(f.execution.shares for f in self._fills if f.execution.orderId == order_id)
        if done >= tr.order.totalQuantity:
            tr.orderStatus.status = "Filled"

    def kill(self, order_id):
        """IBKR cancela una orden por su cuenta (p. ej. una hija tras el llenado parcial de la otra)."""
        self._trades[order_id].orderStatus.status = "Cancelled"

    def error(self, req_id, code, msg):
        self.in_handler = True
        try:
            self.errorEvent.emit(req_id, code, msg, None)
        finally:
            self.in_handler = False


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def orden(oid="a1b2c3d4", t="ABC", px=10.0, tipo="stp", qty=None, hasta=None, **kw):
    lim = round(px * 1.003, 2)
    o = {"id": oid, "t": t, "tipo": tipo, "gatillo": px if tipo == "stp" else None, "limite": lim,
         "trail": round(px * 0.03, 2), "objetivo": round(px * 1.05, 2), "qty": qty or int(500 // lim),
         "hasta": hasta or T0 + 3600}
    o.update(kw)
    return o


def orden_penny(oid="ab12cd34", t="PNY", px=2.0, pct=10.0, **kw):
    """Oferta del estilo penny: US$1,000, Trailing en % que sube, sin objetivo."""
    lim = round(px * 1.003, 2)
    o = {"id": oid, "t": t, "tipo": "stp", "gatillo": px, "limite": lim, "trail": max(0.01, round(lim * pct / 100, 2)),
         "trail_pct": pct, "objetivo": None, "qty": int(1000 // lim), "hasta": T0 + 3600}
    o.update(kw)
    return o


def nuevo(cuentas=("DUR233329",), now=T0, path=None, penny=False):
    ib, clock, posted = FakeIB(cuentas), Clock(now), []
    resp = {"r": {}}

    def post(url, token, payload, timeout=10):
        assert url == "https://sem.test/api/paper/sync" and token == "clave"
        posted.append(copy.deepcopy(payload))
        if isinstance(resp["r"], Exception):
            raise resp["r"]
        return resp["r"]
    path = path or tempfile.mktemp(suffix=".json")
    ex = E.Ejecutor({"SEMAFORO_URL": "https://sem.test/", "BRIDGE_TOKEN": "clave"}, ib, post=post, reloj=clock,
                    estado_path=path)
    if not penny:   # las pruebas de siempre corren con los topes de antes (500 / 15 / 1,000 / 100); el estilo penny trae los suyos
        ex.ORDEN_USD, ex.RIESGO_USD, ex.MAX_ABIERTO, ex.PERDIDA_MAX = 500.0, 15.0, 1000.0, 100.0
    return ex, ib, clock, posted, resp


def evs(posted, ev=None, ex=None):
    out = [e for p in posted for e in p["eventos"]] + (list(ex.eventos) if ex else [])
    return [e for e in out if ev is None or e["ev"] == ev]


def vuelta(ex, clock, resp, dt=1, r=None):
    clock.t += dt
    resp["r"] = r or {}
    return ex.paso()


def test_candado():
    assert E.PUERTO == 4002 and not hasattr(E, "IB_PORT")              # el puerto no se configura
    ex, ib, clock, posted, resp = nuevo(cuentas=("U1234567",))
    resp["r"] = {"ordenes": [orden()]}
    assert ex.paso() == 10
    assert ib.connects == [("127.0.0.1", 4002, 41)] and not ib.conn     # se conectó a paper, vio cuenta real y salió
    assert ib.placed == [] and "no es paper" in ex.bloqueado
    assert posted[-1]["estado"]["paper"] is False and "no es paper" in posted[-1]["estado"]["bloqueado"]
    ex, ib, clock, posted, resp = nuevo(cuentas=("DUR233329", "U7654321"))  # mezcla: tampoco
    resp["r"] = {"ordenes": [orden()]}
    ex.paso()
    assert ib.placed == [] and not ex.paper


def test_orden_con_hijas():
    ex, ib, clock, posted, resp = nuevo()
    resp["r"] = {"ordenes": [orden(), orden("b2c3d4e5", "XYZ", 20.0, tipo="lmt", hasta=T0 + 120)]}
    ex.paso()
    assert len(ib.placed) == 6, ib.placed
    (c, e), (_, t), (_, o) = ib.placed[:3]
    assert c.symbol == "ABC" and c.conId
    assert (e.action, e.orderType, e.totalQuantity, e.auxPrice, e.lmtPrice) == ("BUY", "STP LMT", 49, 10.0, 10.03)
    assert e.tif == "GTD" and e.goodTillDate == "20261005 11:00:00 US/Eastern" and e.transmit is False
    assert e.orderRef == "sem-a1b2c3d4-e"
    assert (t.action, t.orderType, t.totalQuantity, t.auxPrice, t.parentId) == ("SELL", "TRAIL", 49, 0.3, e.orderId)
    assert t.transmit is False and t.tif == "DAY" and not t.ocaGroup and not o.ocaGroup   # bracket estándar, sin OCA
    assert (o.action, o.orderType, o.lmtPrice, o.parentId, o.transmit) == ("SELL", "LMT", 10.5, e.orderId, True)
    e2 = ib.placed[3][1]
    assert e2.orderType == "LMT" and e2.lmtPrice == 20.06 and e2.totalQuantity == 24
    assert not (0 < (e2.auxPrice or 0) < 1e300) and e2.goodTillDate == "20261005 10:02:00 US/Eastern"
    vuelta(ex, clock, resp, r={"ordenes": [orden()]})                      # el semáforo la repite: no se duplica
    assert len(ib.placed) == 6
    puestas = evs(posted, "puesta")
    assert [p["id"] for p in puestas] == ["a1b2c3d4", "b2c3d4e5"] and "Trailing 0.30" in puestas[0]["detalle"]
    vuelta(ex, clock, resp)
    assert [a["estado"] for a in evs(posted, "ack")] == ["puesta"]        # le repite al semáforo cómo va
    est = posted[-1]["estado"]
    assert est["comprometido"] == round(49 * 10.03 + 24 * 20.06, 2) and est["vivas_t"] == ["ABC", "XYZ"], est
    ex2, ib2, c2, p2, r2 = nuevo()                                        # vencimiento: nunca después de las 15:30
    r2["r"] = {"ordenes": [orden(hasta=at(16, 0))]}
    ex2.paso()
    assert ib2.placed[0][1].goodTillDate == "20261005 15:30:00 US/Eastern"
    ex2b, ib2b, c2b, p2b, r2b = nuevo()                                   # y si el semáforo cierra las compras antes, esa hora
    r2b["r"] = {"limites": {"entrada_fin_m": 12 * 60}, "ordenes": [orden(hasta=at(16, 0))]}
    ex2b.paso()
    assert ib2b.placed[0][1].goodTillDate == "20261005 12:00:00 US/Eastern", [p[1].goodTillDate for p in ib2b.placed]


def test_llena_y_sale():
    ex, ib, clock, posted, resp = nuevo()
    resp["r"] = {"ordenes": [orden()]}
    ex.paso()
    oids = ex.ord["a1b2c3d4"]["oids"]
    ib.fill(oids["e"], 49, 10.02)
    vuelta(ex, clock, resp)
    vuelta(ex, clock, resp)
    (ll,) = evs(posted, "llena")
    assert ll["px"] == 10.02 and ll["qty"] == 49
    assert posted[-1]["estado"]["posiciones"] == [{"t": "ABC", "qty": 49, "px": 10.02}]
    for _ in range(3):
        vuelta(ex, clock, resp)
    assert len(ib.placed) == 3 and not ib.cancels                         # con su stop vivo no toca nada
    ib.fill(oids["t"], 49, 10.45)                                         # vendió el stop que sube
    vuelta(ex, clock, resp)
    vuelta(ex, clock, resp)
    (s,) = evs(posted, "salida")
    assert s["por"] == "trailing" and s["px"] == 10.45 and s["pnl"] == 21.07 and s["px_e"] == 10.02, s
    est = posted[-1]["estado"]
    assert est["pnl_dia"] == 21.07 and est["comprometido"] == 0 and est["posiciones"] == [] and est["vivas_t"] == []


def test_limites():
    ex, ib, clock, posted, resp = nuevo()
    resp["r"] = {"ordenes": [orden("00000001", "AAA", qty=50),                      # US$501.5: pasa de 500
                             orden("00000002", "BBB"), orden("00000003", "CCC"),    # 491 + 491
                             orden("00000004", "DDD"),                               # pasaría de 1,000
                             orden("00000005", "BBB", 5.0),                          # BBB ya tiene orden
                             orden("00000006", "EEE", objetivo=9.0),                 # objetivo bajo la compra
                             orden("00000007", "FFF", gatillo=10.5),                 # gatillo sobre el límite
                             orden("00000008", "bad!"), orden("XYZ", "GGG"),         # símbolo / id inválidos
                             orden("0000000a", "HHH", hasta=T0 - 1)]}               # ya vencida
    ex.paso()
    vuelta(ex, clock, resp)
    rech = {e["id"]: e["motivo"] for e in evs(posted, "rechazada")}
    assert "por operación" in rech["00000001"] and "1,000" in rech["00000004"], rech
    assert "ya hay una orden" in rech["00000005"] and "inválida" in rech["00000006"]
    assert "tipo de orden" in rech["00000007"] and "símbolo inválido" in rech["00000008"]
    assert "venció" in rech["0000000a"] and "XYZ" not in rech
    assert {p[1].orderRef[4:12] for p in ib.placed} == {"00000002", "00000003"}
    exn, ibn, cn, pn, rn = nuevo()
    rn["r"] = {"ordenes": [orden("00000009", "NOPE")]}                    # IBKR no conoce el símbolo
    exn.paso()
    vuelta(exn, cn, rn)
    assert ibn.placed == [] and "no reconoce" in evs(pn, "rechazada")[0]["motivo"]
    for h, m_ in ((15, 31), (9, 29)):                                    # fuera de 9:30–15:30 ET
        ex2, ib2, c2, p2, r2 = nuevo(now=at(h, m_))
        r2["r"] = {"ordenes": [orden(hasta=at(h, m_) + 600)]}
        ex2.paso()
        vuelta(ex2, c2, r2)
        assert ib2.placed == [] and "horario" in evs(p2, "rechazada")[0]["motivo"]
    for h, m_ in ((12, 1), (14, 45), (15, 29)):                          # la tarde también vale (ya no se corta a las 12:00)
        ex2, ib2, c2, p2, r2 = nuevo(now=at(h, m_))
        r2["r"] = {"ordenes": [orden(hasta=at(h, m_) + 600)]}
        ex2.paso()
        assert len(ib2.placed) == 3 and not evs(p2, "rechazada"), (h, m_, [e["motivo"] for e in evs(p2, "rechazada")])
    ex2, ib2, c2, p2, r2 = nuevo(now=at(12, 1))                           # si el semáforo cierra las compras a las 12:00, manda esa hora
    r2["r"] = {"limites": {"entrada_fin_m": 12 * 60}, "ordenes": [orden(hasta=at(12, 1) + 600)]}
    ex2.paso()
    vuelta(ex2, c2, r2)
    assert ib2.placed == [] and "9:30–12:00" in evs(p2, "rechazada")[0]["motivo"]
    sab = datetime(2026, 10, 3, 10, 0, tzinfo=NY).timestamp()
    ex3, ib3, c3, p3, r3 = nuevo(now=sab)
    r3["r"] = {"ordenes": [orden(hasta=sab + 600)]}
    ex3.paso()
    assert ib3.placed == []                                               # sábado
    ex4, ib4, c4, p4, r4 = nuevo()
    r4["r"] = {"pausa": True, "ordenes": [orden()]}
    ex4.paso()
    assert ib4.placed == []                                               # en pausa
    ex5, ib5, c5, p5, r5 = nuevo()                                        # límites del semáforo más estrictos
    r5["r"] = {"limites": {"orden_usd": 300}}
    ex5.paso()
    vuelta(ex5, c5, r5, r={"limites": {"orden_usd": 300}, "ordenes": [orden()]})
    assert ib5.placed == []
    vuelta(ex5, c5, r5, r={"limites": {"orden_usd": 5000, "max_abierto": 99999}, "ordenes": [orden("bbbbbbbb", qty=100)]})
    assert ib5.placed == []                                               # los propios nunca se aflojan
    ex6, ib6, c6, p6, r6 = nuevo()                                        # cancelada antes de llegar
    r6["r"] = {"cancelar": ["cccccccc"]}
    ex6.paso()
    vuelta(ex6, c6, r6, r={"cancelar": ["cccccccc"]})                     # el semáforo insiste: un solo aviso
    vuelta(ex6, c6, r6, r={"ordenes": [orden("cccccccc")]})
    vuelta(ex6, c6, r6)
    canc = evs(p6, "cancelada")
    assert ib6.placed == [] and [e["id"] for e in canc] == ["cccccccc", "cccccccc"], canc   # la avisa y no la pone
    assert "antes de llegar" in canc[0]["motivo"]


def test_perdida_maxima():
    ex, ib, clock, posted, resp = nuevo()
    resp["r"] = {"ordenes": [orden("00000001", "AAA", 50.0, qty=9), orden("00000002", "BBB", 10.0)]}
    ex.paso()
    a = ex.ord["00000001"]["oids"]
    ib.fill(a["e"], 9, 50.0)
    ib.fill(a["t"], 9, 40.0)                                              # pierde 90
    vuelta(ex, clock, resp, r={"ordenes": [orden("00000003", "CCC", 10.0)]})   # 90 + 14.7 de riesgo > 100
    vuelta(ex, clock, resp)
    r3 = evs(posted, "rechazada")[-1]
    assert r3["id"] == "00000003" and "pérdida máxima" in r3["motivo"], r3
    b = ex.ord["00000002"]["oids"]
    ib.fill(b["e"], 49, 10.03)
    ib.fill(b["t"], 49, 9.80)                                             # pierde 11.27 más: 101.27
    vuelta(ex, clock, resp)
    assert ex.parado and evs(posted, "parada", ex)
    vuelta(ex, clock, resp, r={"ordenes": [orden("00000004", "DDD", 2.0)]})
    vuelta(ex, clock, resp)
    assert "máximo del día" in evs(posted, "rechazada")[-1]["motivo"]


def test_vence_y_parcial():
    ex, ib, clock, posted, resp = nuevo()
    resp["r"] = {"ordenes": [orden("00000001", "AAA", hasta=T0 + 60), orden("00000002", "BBB", 20.0, hasta=T0 + 60)]}
    ex.paso()
    a, b = ex.ord["00000001"]["oids"], ex.ord["00000002"]["oids"]
    ib.fill(b["e"], 10, 20.06)                                            # BBB: 10 de 24
    vuelta(ex, clock, resp, dt=63)
    vuelta(ex, clock, resp)
    assert a["e"] in ib.cancels and b["e"] in ib.cancels
    c = evs(posted, "cancelada")
    assert c[0]["id"] == "00000001" and "venció" in c[0]["motivo"], c
    ll = evs(posted, "llena")[0]
    assert ll["id"] == "00000002" and ll["qty"] == 10 and ll["parcial"]
    t, o = ib._trades[b["t"]].order, ib._trades[b["o"]].order
    assert t.totalQuantity == 10 and o.totalQuantity == 10 and t.transmit is True and o.transmit is True  # salen a IBKR
    ex, ib, clock, posted, resp = nuevo()                                 # parcial con el stop que ya vendió una parte
    resp["r"] = {"ordenes": [orden(hasta=T0 + 60)]}
    ex.paso()
    o = ex.ord["a1b2c3d4"]["oids"]
    ib.fill(o["e"], 10, 10.02)
    ib.fill(o["t"], 4, 9.9)
    vuelta(ex, clock, resp, dt=63)
    assert ib._trades[o["t"]].order.totalQuantity == 4 + 6                # cubre las 6 que quedan, no las 10 compradas
    vuelta(ex, clock, resp, r={"cancelar": ["a1b2c3d4"]})                 # el semáforo pide cancelar algo ya resuelto
    assert [a["estado"] for a in evs(posted, "ack", ex)] == ["llena"]


def test_llenado_tardio():
    """El llenado llega justo cuando se cancela (en IBKR se llenó primero): es una posición y se cuida."""
    ex, ib, clock, posted, resp = nuevo()
    resp["r"] = {"ordenes": [orden()]}
    ex.paso()
    oids = ex.ord["a1b2c3d4"]["oids"]
    vuelta(ex, clock, resp, r={"cancelar": ["a1b2c3d4"]})                 # el semáforo la cancela...
    assert ex.ord["a1b2c3d4"]["estado"] == "cancelando" and oids["e"] in ib.cancels
    ib._trades[oids["e"]].orderStatus.status = "Submitted"               # ...pero IBKR ya la había llenado
    ib.fill(oids["e"], 49, 10.02)
    vuelta(ex, clock, resp)
    assert ex.ord["a1b2c3d4"]["estado"] == "llena" and posted[-1]["estado"]["vivas_t"] == ["ABC"]
    ex2, ib2, c2, p2, r2 = nuevo()                                        # llega después de darla por cancelada
    r2["r"] = {"ordenes": [orden()]}
    ex2.paso()
    o2 = ex2.ord["a1b2c3d4"]["oids"]
    vuelta(ex2, c2, r2, r={"cancelar": ["a1b2c3d4"]})
    vuelta(ex2, c2, r2)
    assert ex2.ord["a1b2c3d4"]["estado"] == "cancelada"
    ib2._trades[o2["t"]].orderStatus.status = "Submitted"                # la hija sigue viva en IBKR
    ib2.fill(o2["e"], 49, 10.02)
    vuelta(ex2, c2, r2)
    vuelta(ex2, c2, r2)
    tarde = evs(p2, "llena")
    assert tarde and tarde[0]["tarde"]
    assert p2[-1]["estado"]["vivas_t"] == ["ABC"] and p2[-1]["estado"]["comprometido"] > 0
    vuelta(ex2, c2, r2, r={"ordenes": [orden("b0b0b0b0", "ABC")]})        # no se acepta otra en ABC
    vuelta(ex2, c2, r2)
    assert "ya hay una orden" in evs(p2, "rechazada")[-1]["motivo"]
    c2.t = at(15, 55)                                                     # y a las 15:55 se vende
    ex2.paso()
    vuelta(ex2, c2, r2, dt=2)
    assert ib2.placed[-1][1].orderType == "MKT" and ib2.placed[-1][1].totalQuantity == 49


def test_sin_stop_y_de_mas():
    ex, ib, clock, posted, resp = nuevo()
    resp["r"] = {"ordenes": [orden()]}
    ex.paso()
    o = ex.ord["a1b2c3d4"]["oids"]
    ib.fill(o["e"], 49, 10.02)
    vuelta(ex, clock, resp)
    ib.fill(o["o"], 20, 10.5)                                             # el objetivo vendió 20 de 49...
    ib.kill(o["t"])                                                       # ...e IBKR canceló el stop
    vuelta(ex, clock, resp)
    assert not any(p[1].orderType == "MKT" for p in ib.placed)            # una vuelta de margen
    vuelta(ex, clock, resp)
    assert any(e["ev"] == "error" and "sin su stop" in e["motivo"] for e in evs(posted, ex=ex))
    vuelta(ex, clock, resp, dt=2)
    venta = ib.placed[-1][1]
    assert (venta.orderType, venta.totalQuantity, venta.orderRef) == ("MKT", 29, "sem-a1b2c3d4-x")
    ib.fill(venta.orderId, 29, 10.4)
    vuelta(ex, clock, resp)
    vuelta(ex, clock, resp)
    s = evs(posted, "salida")[-1]
    assert s["por"] == "cierre" and s["qty"] == 49
    # tamaño distinto: se ajusta el stop (con lo ya vendido por esa hija incluido)
    ex, ib, clock, posted, resp = nuevo()
    resp["r"] = {"ordenes": [orden()]}
    ex.paso()
    o = ex.ord["a1b2c3d4"]["oids"]
    ib.fill(o["e"], 49, 10.02)
    ib.fill(o["o"], 20, 10.5)                                             # IBKR no redujo el stop (sigue en 49)
    vuelta(ex, clock, resp)
    vuelta(ex, clock, resp)
    vuelta(ex, clock, resp)
    assert ib._trades[o["t"]].order.totalQuantity == 29 and ib._trades[o["t"]].order.transmit is True
    assert ib._trades[o["o"]].order.totalQuantity == 49                   # 20 vendidas + 29 que faltan
    # vendió de más (quedó en corto): recompra la diferencia
    ex, ib, clock, posted, resp = nuevo()
    resp["r"] = {"ordenes": [orden()]}
    ex.paso()
    o = ex.ord["a1b2c3d4"]["oids"]
    ib.fill(o["e"], 10, 10.02)
    vuelta(ex, clock, resp)
    ib.fill(o["t"], 49, 10.3)                                             # el stop vendió 49 teniendo 10
    vuelta(ex, clock, resp)
    rec = ib.placed[-1][1]
    assert (rec.action, rec.orderType, rec.totalQuantity, rec.orderRef) == ("BUY", "MKT", 39, "sem-a1b2c3d4-c")
    assert any("vendió de más" in e["motivo"] for e in evs(posted, "error", ex))
    vuelta(ex, clock, resp)
    assert sum(1 for p in ib.placed if p[1].orderRef.endswith("-c")) == 1   # una recompra a la vez


def test_cierre_1555_y_cerrar():
    ex, ib, clock, posted, resp = nuevo()
    resp["r"] = {"ordenes": [orden("00000001", "AAA", hasta=at(12, 0)), orden("00000002", "BBB", 20.0)]}
    ex.paso()
    a, b = ex.ord["00000001"]["oids"], ex.ord["00000002"]["oids"]
    ib.fill(b["e"], 24, 20.06)
    ajena = ib.placeOrder(NS(symbol="MSFT"), E.Order(action="BUY", totalQuantity=5, orderType="LMT", orderRef=""))
    ib.fill(ajena.order.orderId, 5, 400.0)                                # orden tuya, manual: no se toca
    vuelta(ex, clock, resp)
    clock.t = at(15, 55)
    ex.paso()
    assert a["e"] in ib.cancels and b["t"] in ib.cancels and b["o"] in ib.cancels
    assert ex.cerrado_hoy and evs(posted, "cierre", ex)
    vuelta(ex, clock, resp, dt=2)                                         # vende cuando las hijas ya cancelaron
    venta = ib.placed[-1][1]
    assert (venta.action, venta.orderType, venta.totalQuantity, venta.orderRef) == ("SELL", "MKT", 24, "sem-00000002-x")
    assert ajena.order.orderId not in ib.cancels
    n = len(ib.placed)
    vuelta(ex, clock, resp, dt=60)
    assert len(ib.placed) == n                                            # no vende dos veces
    ib.fill(venta.orderId, 24, 20.50)
    vuelta(ex, clock, resp)
    vuelta(ex, clock, resp)
    assert evs(posted, "salida")[-1]["por"] == "cierre"
    # /cerrar desde Telegram: se ejecuta una vez; uno viejo (de antes de arrancar) no
    ex, ib, clock, posted, resp = nuevo()
    resp["r"] = {"ordenes": [orden()]}
    ex.paso()
    vuelta(ex, clock, resp, r={"cerrar_id": "c1", "cerrar_hace_s": 3})
    e1 = ex.ord["a1b2c3d4"]["oids"]["e"]
    assert ib.cancels.count(e1) == 1 and evs(posted, "cierre", ex)[-1]["motivo"] == "lo pediste con /cerrar"
    vuelta(ex, clock, resp, r={"cerrar_id": "c1", "cerrar_hace_s": 4})
    assert ib.cancels.count(e1) == 1                                      # mismo pedido: no se repite
    ex2, ib2, c2, p2, r2 = nuevo(path=ex.path)                            # reinicio: recuerda que ya lo hizo
    r2["r"] = {"cerrar_id": "c1", "cerrar_hace_s": 30}
    ex2.paso()
    assert ib2.cancels == []
    vuelta(ex2, c2, r2, r={"cerrar_id": "c2", "cerrar_hace_s": 900})      # viejo: no
    assert ib2.cancels == [] and ex2.cerrar_visto == "c2"


def test_rechazos():
    ex, ib, clock, posted, resp = nuevo()
    resp["r"] = {"ordenes": [orden("00000001", "AAA"), orden("00000002", "BBB", 20.0),
                             orden("00000003", "CCC", 5.0, qty=5)]}                   # 491 + 481 + 25 < 1,000
    ex.paso()
    a, b, c = (dict(ex.ord[k]["oids"]) for k in ("00000001", "00000002", "00000003"))
    ib.error(a["e"], 201, "Order rejected - reason:<br>fondos insuficientes")
    ib.error(b["e"], 201, "Order rejected - reason: Invalid goodTillDate format")    # GTD no aceptado: va DAY
    ib.error(c["t"], 201, "Order rejected - reason: trailing amount")    # el stop rechazado: no hay compra sin stop
    ib.error(a["e"], 10349, "Order TIF was set to DAY based on order preset.")       # aviso: no cambia nada
    ib.error(9999, 201, "orden ajena")
    assert ex.err_q and len(ib.placed) == 9                               # el manejador solo anota
    vuelta(ex, clock, resp)
    assert ex.ord["00000001"]["estado"] == "rechazada"
    nueva = ex.ord["00000002"]["oids"]
    assert nueva["e"] != b["e"] and ib._trades[nueva["e"]].order.tif == "DAY" and ex.ord["00000002"]["estado"] == "puesta"
    assert c["e"] in ib.cancels and ex.ord["00000003"]["estado"] == "cancelada"
    ib.error(b["t"], 201, "Order rejected - reason: parent rejected")    # la hija vieja de BBB: no cancela la nueva
    vuelta(ex, clock, resp)
    assert ex.ord["00000002"]["estado"] == "puesta" and nueva["e"] not in ib.cancels
    vuelta(ex, clock, resp)
    m = {e["id"]: e for e in evs(posted)}
    assert "fondos insuficientes" in m["00000001"]["motivo"] and "<br>" not in m["00000001"]["motivo"]
    assert "stop" in m["00000003"]["motivo"]
    # IB Gateway en solo lectura (IBC lo vuelve a marcar al entrar): la orden se da por rechazada y se dice por qué
    exr, ibr, cr, pr, rr = nuevo()
    rr["r"] = {"ordenes": [orden()]}
    exr.paso()
    ibr.error(exr.ord["a1b2c3d4"]["oids"]["e"], 321,
              "Error validating request.-'bW' : cause - The API interface is currently in Read-Only mode.")
    vuelta(exr, cr, rr)
    vuelta(exr, cr, rr)
    assert exr.ord["a1b2c3d4"]["estado"] == "rechazada" and "Read-Only API" in evs(pr, "rechazada")[0]["motivo"]
    # hija rechazada después de comprar → vende la posición (sin esperar dentro del manejador)
    ex, ib, clock, posted, resp = nuevo()
    resp["r"] = {"ordenes": [orden()]}
    ex.paso()
    o = ex.ord["a1b2c3d4"]["oids"]
    ib.fill(o["e"], 49, 10.02)
    vuelta(ex, clock, resp)
    ib.error(o["o"], 201, "Order rejected - reason: price")
    vuelta(ex, clock, resp)
    assert o["t"] in ib.cancels and any(e["ev"] == "error" for e in evs(posted, ex=ex))
    vuelta(ex, clock, resp, dt=2)
    assert ib.placed[-1][1].orderType == "MKT" and ib.placed[-1][1].totalQuantity == 49


def test_red_y_reinicio():
    path = tempfile.mktemp(suffix=".json")
    ex, ib, clock, posted, resp = nuevo(path=path)
    resp["r"] = {"ordenes": [orden()]}
    ex.paso()
    resp["r"] = ConnectionError("sin conexión con el semáforo")
    oids = ex.ord["a1b2c3d4"]["oids"]
    ib.fill(oids["e"], 49, 10.02)
    clock.t += 1
    ex.paso()
    assert "llena" in [e["ev"] for e in ex.eventos]                       # no se pierde: espera a la red
    vuelta(ex, clock, resp)
    assert ex.eventos == [] and evs(posted, "llena")
    ex2 = E.Ejecutor({"SEMAFORO_URL": "https://sem.test", "BRIDGE_TOKEN": "clave"}, ib, post=lambda *a, **k: {},
                     reloj=clock, estado_path=path)                       # reinicio con el archivo
    assert ex2.ord["a1b2c3d4"]["estado"] == "llena" and ex2.por_oid[oids["t"]] == ("a1b2c3d4", "t")
    ex3 = E.Ejecutor({"SEMAFORO_URL": "https://sem.test", "BRIDGE_TOKEN": "clave"}, ib, post=lambda *a, **k: {},
                     reloj=clock, estado_path=tempfile.mktemp(suffix=".json"))   # sin archivo: por la referencia
    ib.conn = False
    ex3.paso()
    r = ex3.ord["a1b2c3d4"]
    assert r["t"] == "ABC" and r["estado"] == "llena" and r["qty"] == 49 and r["trail"] == 0.3, r
    ib.fill(oids["o"], 49, 10.5)
    clock.t += 1
    ex3.paso()
    assert ex3.ord["a1b2c3d4"]["estado"] == "cerrada"
    ex4, ib4, c4, p4, r4 = nuevo()                                        # Ctrl+C: cancela las compras sin llenar
    r4["r"] = {"ordenes": [orden()]}
    ex4.paso()
    ex4.salir()
    assert ex4.ord["a1b2c3d4"]["oids"]["e"] in ib4.cancels
    ex5, ib5, c5, p5, r5 = nuevo(path=path)                               # pausa y modo sobreviven reinicios
    r5["r"] = {"pausa": True, "modo": "auto"}
    ex5.paso()
    ex6 = E.Ejecutor({"SEMAFORO_URL": "https://sem.test", "BRIDGE_TOKEN": "clave"}, ib5, post=lambda *a, **k: {},
                     reloj=c5, estado_path=path)
    assert ex6.pausa is True and ex6.modo == "auto"


class IBCaido(FakeIB):
    """IB Gateway reiniciándose: rechaza las conexiones las primeras `fallar` veces."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fallar = 0

    def connect(self, host, port, clientId, timeout):
        if self.fallar > 0:
            self.fallar -= 1
            raise ConnectionRefusedError("IB Gateway no contesta")
        super().connect(host, port, clientId, timeout)


def test_reconexion_tras_reinicio_del_gateway():
    """El Gateway se reinicia solo cada noche (23:30). El ejecutor debe reconectar solo y seguir con lo suyo."""
    ib = IBCaido()
    clock, resp = Clock(T0), {"r": {"ordenes": [orden()]}}
    ex = E.Ejecutor({"SEMAFORO_URL": "https://sem.test/", "BRIDGE_TOKEN": "clave"}, ib, post=lambda *a, **k: resp["r"],
                    reloj=clock, estado_path=tempfile.mktemp(suffix=".json"))
    ex.paso()
    oids = dict(ex.ord["a1b2c3d4"]["oids"])
    ib.fill(oids["e"], 49, 10.02)
    clock.t += 1
    ex.paso()
    assert ex.ord["a1b2c3d4"]["estado"] == "llena" and ex.paper
    colocadas = len(ib.placed)
    # Corte: el Gateway se va y tarda 8 intentos en volver. Con el mercado abierto se insiste cada 10 s.
    ib.conn, ib.fallar = False, 8
    resp["r"] = {}
    esperas = []
    for _ in range(8):
        clock.t += 10
        esperas.append(ex.paso())
        assert not ex.paper and ex.fallos_ib == len(esperas)
    assert esperas == [10] * 8, esperas
    clock.t += 10
    ex.paso()                                                           # vuelve: reconoce lo suyo y sigue
    assert ex.paper and ib.isConnected() and ex.fallos_ib == 0
    assert ex.ord["a1b2c3d4"]["estado"] == "llena" and ex.por_oid[oids["t"]] == ("a1b2c3d4", "t")
    assert len(ib.placed) == colocadas, "reconectar no debe poner órdenes nuevas"
    ib.fill(oids["o"], 49, 10.5)                                        # y sigue gestionando la posición
    vuelta(ex, clock, resp)
    assert ex.ord["a1b2c3d4"]["estado"] == "cerrada"
    # De noche (domingo 22:00 ET, mercado cerrado): 10 s los primeros 6 intentos y luego 30 s, sin llenar el registro.
    ib2 = IBCaido()
    ib2.fallar = 9
    dom = datetime(2026, 10, 4, 22, 0, tzinfo=NY).timestamp()
    ex2 = E.Ejecutor({"SEMAFORO_URL": "https://sem.test/", "BRIDGE_TOKEN": "clave"}, ib2, post=lambda *a, **k: {},
                     reloj=Clock(dom), estado_path=tempfile.mktemp(suffix=".json"))
    esp = [ex2.paso() for _ in range(9)]
    assert esp == [10] * 6 + [30] * 3, esp
    assert ex2.paso() == 10 and ex2.paper                                # al fin conecta (domingo: ritmo de reposo)
    assert ex2.fallos_ib == 0


def _bucle_con(pasos, sleeps):
    """Corre E.bucle con un ejecutor y un ib falsos que van lanzando lo indicado; termina con Ctrl+C simulado."""
    llamadas = {"paso": 0, "sleep": 0, "esperas": []}

    class Ej:
        def paso(self):
            i = llamadas["paso"]
            llamadas["paso"] += 1
            r = pasos[i] if i < len(pasos) else 0
            if isinstance(r, BaseException):
                raise r
            return r

    class Ib:
        def sleep(self, s):
            i = llamadas["sleep"]
            llamadas["sleep"] += 1
            llamadas["esperas"].append(s)
            r = sleeps[i] if i < len(sleeps) else KeyboardInterrupt()
            if isinstance(r, BaseException):
                raise r
    old = E.time.sleep
    E.time.sleep = lambda s: None                                       # sin esperas de verdad
    try:
        try:
            E.bucle(Ej(), Ib(), "ejecutor de prueba")
        except KeyboardInterrupt:
            llamadas["ctrl_c"] = True
    finally:
        E.time.sleep = old
    return llamadas


def test_el_corte_de_conexion_no_mata_el_bucle():
    """Lo que pasó de verdad a las 23:30: el Gateway se reinicia, la librería lanza ConnectionError desde ib.sleep() y el
    ejecutor se cerraba sin avisar. Ahora el bucle sigue y solo Ctrl+C lo detiene."""
    import asyncio
    # 1) el error sale de la espera (el caso real)
    ll = _bucle_con([5, 5, 5], [ConnectionError("Socket disconnect"), None, RuntimeError("raro"), KeyboardInterrupt()])
    assert ll["ctrl_c"] and ll["paso"] == 4 and ll["sleep"] == 4, ll
    # 2) la cancelación de asyncio también (otra forma en que la librería corta una espera)
    ll = _bucle_con([5, 5], [asyncio.CancelledError(), KeyboardInterrupt()])
    assert ll["ctrl_c"] and ll["paso"] == 2, ll
    # 3) el corte llega a mitad de un paso (p. ej. mientras pide las órdenes abiertas): reintenta pronto, sin traceback
    ll = _bucle_con([ConnectionError("Socket disconnect"), 5, asyncio.CancelledError(), 5], [None, None, None, KeyboardInterrupt()])
    assert ll["ctrl_c"] and ll["esperas"][:3] == [3, 5, 3], ll
    # 4) un error cualquiera en un paso: reintenta en 5 s
    ll = _bucle_con([ValueError("x"), 7], [None, KeyboardInterrupt()])
    assert ll["esperas"][0] == 5 and ll["esperas"][1] == 7, ll
    # 5) Ctrl+C sale (no se traga): también si llega durante un paso
    ll = _bucle_con([KeyboardInterrupt()], [])
    assert ll["ctrl_c"] and ll["sleep"] == 0, ll
    # 6) cerrar() desconecta aunque falle algo al despedirse
    ib = FakeIB()
    ib.conn = True
    ex = E.Ejecutor({"SEMAFORO_URL": "https://sem.test/", "BRIDGE_TOKEN": "clave"}, ib, post=lambda *a, **k: {},
                    reloj=Clock(T0), estado_path=tempfile.mktemp(suffix=".json"))
    ex.salir = lambda: (_ for _ in ()).throw(ConnectionError("Socket disconnect"))
    E.cerrar(ex, ib)
    assert not ib.isConnected()


def test_el_error_real_de_ib_async():
    """Con la librería de verdad: un corte de conexión sale como ConnectionError de ib.sleep(); esperar() lo absorbe y la
    librería sigue sirviendo para la siguiente espera. (Sin ib_async instalado no se prueba.)"""
    if E.IB is None:
        return
    from ib_async import util
    ib = E.IB()
    loop = util.getLoop()

    def corte():
        util.globalErrorEvent.emit(ConnectionError("Socket disconnect"))
    loop.call_later(0.1, corte)
    try:
        ib.sleep(1)
    except ConnectionError:
        pass
    else:
        raise AssertionError("la librería ya no lanza ConnectionError desde sleep(): revisa esperar()/bucle()")
    old = E.time.sleep
    E.time.sleep = lambda s: None
    try:
        loop.call_later(0.1, corte)
        E.esperar(ib, 1)                                                # no lanza
        assert ib.sleep(0.05) in (True, None)                           # y la siguiente espera funciona
    finally:
        E.time.sleep = old


def _puerto_libre():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_una_sola_copia():
    """El cerrojo (puerto local): la primera copia lo toma y saluda a quien se conecte, la segunda ve que ya hay otra, y al cerrar
    la primera se libera. Si el puerto no se puede reservar y quien contesta NO es otro ejecutor (nadie, o un programa cualquiera),
    se sigue sin el seguro: un seguro roto no debe dejar al ejecutor sin arrancar."""
    import socket
    p = _puerto_libre()
    c1, otro1 = E.tomar_cerrojo(p, "paper")
    assert isinstance(c1, E.Cerrojo) and otro1 is False
    c2, otro2 = E.tomar_cerrojo(p, "paper")
    assert c2 is None and otro2 is True                                   # ya hay otra copia
    assert E.ya_hay_otro(p, "ejecutor de prueba") == 3                    # y su código de salida es 3
    t0 = time.time()
    c1.close()
    assert time.time() - t0 < 1.4                                         # cerrar no se queda esperando
    c3, otro3 = E.tomar_cerrojo(p, "paper")                               # liberado: se puede volver a tomar (reinicio)
    assert c3 is not None and otro3 is False
    c3.close()
    # puerto que el sistema no deja reservar, sin nadie escuchando: no es "otra copia"
    p4 = _puerto_libre()
    real = E.socket.socket

    class Bloqueado(real):
        def bind(self, addr):
            raise PermissionError(13, "acceso denegado")
    E.socket.socket = Bloqueado
    try:
        c4, otro4 = E.tomar_cerrojo(p4)
    finally:
        E.socket.socket = real
    assert c4 is None and otro4 is False
    # puerto ocupado por un programa cualquiera (calla o contesta otra cosa): no es un ejecutor, se sigue sin el seguro
    for saludo in (None, b"HTTP/1.1 400 Bad Request\r\n"):
        ajeno = socket.socket()
        ajeno.bind(("127.0.0.1", 0))
        ajeno.listen(5)
        parar = []

        def servir(srv=ajeno, msg=saludo):
            srv.settimeout(0.3)
            while not parar:
                try:
                    c, _ = srv.accept()
                except OSError:
                    continue
                if msg:
                    c.sendall(msg)
                c.close()
        th = threading.Thread(target=servir, daemon=True)
        th.start()
        try:
            c5, otro5 = E.tomar_cerrojo(ajeno.getsockname()[1])
        finally:
            parar.append(1)
            th.join(2)
            ajeno.close()
        assert c5 is None and otro5 is False, (saludo, c5, otro5)


def test_windows_bloquea_archivos():
    """En Windows, el diario abierto en Excel no deja añadirle filas y el estado puede fallar un instante al reemplazarse. Ninguna fila se
    pierde (esperan y entran todas, en orden, sin repetir la cabecera, cuando se libera) y el estado se guarda al reintentar."""
    import csv
    import json
    import logging
    ex, ib, clock, posted, resp = nuevo()
    ex.diario = tempfile.mktemp(suffix=".csv")
    avisos = []

    class Cap(logging.Handler):
        def emit(self, rec):
            avisos.append(rec.getMessage())
    cap = Cap(level=logging.WARNING)
    E.log.addHandler(cap)
    real_open = open

    def excel(path, *a, **kw):                                           # el CSV está abierto en Excel: Windows no deja añadir
        if str(path) == ex.diario:
            raise PermissionError(13, "Permission denied", path)
        return real_open(path, *a, **kw)
    try:
        E.open = excel
        ex.diario_fila("cancelada", orden("11111111", "AAA"), {"motivo": "venció sin activarse"})
        ex.diario_fila("cancelada", orden("22222222", "BBB"), {"motivo": "venció sin activarse"})
        ex.diario_vaciar()                                               # sigue bloqueado: no pierde nada ni repite el aviso
        assert len(ex.diario_pend) == 2 and not os.path.exists(ex.diario)
        assert len([a for a in avisos if "diario" in a]) == 1, avisos      # un solo aviso, no uno por vuelta
        ex.salir()                                                       # si se cierra el programa así, las filas quedan en el registro
        assert len([a for a in avisos if "sin escribir" in a]) == 2 and any("AAA" in a for a in avisos), avisos
        del E.open                                                       # Excel se cerró: en la siguiente vuelta entran las dos
        vuelta(ex, clock, resp)
        assert ex.diario_pend == [] and ex.diario_aviso is False
        ex.diario_fila("cancelada", orden("33333333", "CCC"), {"motivo": "venció sin activarse"})   # y las siguientes, directas
        with real_open(ex.diario, encoding="utf-8", newline="") as f:
            filas = list(csv.DictReader(f))
        assert [r["simbolo"] for r in filas] == ["AAA", "BBB", "CCC"], filas   # en orden, y una sola cabecera
        # el estado: un bloqueo momentáneo del reemplazo se supera reintentando
        real_replace, sleep, n = E.os.replace, E.time.sleep, {"n": 0}

        def replace_flojo(a, b):
            n["n"] += 1
            if n["n"] <= 2:
                raise PermissionError(5, "Access is denied")             # WinError 5
            return real_replace(a, b)
        E.os.replace, E.time.sleep = replace_flojo, lambda s: None
        try:
            ex.pausa = True
            ex.guardar()
            assert n["n"] == 3
            with real_open(ex.path, encoding="utf-8") as f:
                assert json.load(f)["pausa"] is True                     # quedó guardado, pese a los dos bloqueos
            n["n"] = -100                                                # bloqueado todo el rato: se rinde sin lanzar nada
            avisos.clear()
            ex.guardar()
            assert n["n"] == -96 and any("No pude guardar" in a for a in avisos), (n, avisos)
        finally:
            E.os.replace, E.time.sleep = real_replace, sleep
    finally:
        E.log.removeHandler(cap)
        E.__dict__.pop("open", None)


def test_riesgo_por_operacion():
    """Tope de lo que se pierde si salta el stop (acciones × distancia del stop): propio US$15; el semáforo lo baja a US$6 y
    nunca lo sube. Más volatilidad (un stop más ancho) no puede ser más dinero en riesgo."""
    assert E.RIESGO_USD == 105 and E.Ejecutor.RIESGO_USD == 105          # v1.9: los de Priamo (penny); el de Claude queda en 15
    assert (E.ORDEN_USD, E.MAX_ABIERTO, E.PERDIDA_MAX) == (1000.0, 3000.0, 300.0)
    ex, ib, clock, posted, resp = nuevo()
    resp["r"] = {"ordenes": [orden("00000001", "AAA"),                              # 49 × 0.30 = US$14.70: pasa
                             orden("00000002", "BBB", trail=0.5)]}                  # 49 × 0.50 = US$24.50: más de US$15
    ex.paso()
    vuelta(ex, clock, resp)
    rech = {e["id"]: e["motivo"] for e in evs(posted, "rechazada")}
    assert "00000001" not in rech and "stop" in rech["00000002"] and "US$24.50" in rech["00000002"] \
        and "US$15 por operación" in rech["00000002"], rech
    assert {p[1].orderRef[4:12] for p in ib.placed} == {"00000001"}
    ex2, ib2, c2, p2, r2 = nuevo()                                                   # el semáforo manda US$6
    r2["r"] = {"limites": {"riesgo_usd": 6},
               "ordenes": [orden("00000003", "CCC"),                                 # 49 × 0.30 = 14.70: ya no
                           orden("00000004", "DDD", qty=20),                         # 20 × 0.30 = 6.00: justo
                           orden("00000005", "EEE", px=50.0, trail=3.0, qty=2)]}     # 2 × 3.00 = 6.00 (un stop de 6 %)
    ex2.paso()
    vuelta(ex2, c2, r2)
    rech = {e["id"]: e["motivo"] for e in evs(p2, "rechazada")}
    assert list(rech) == ["00000003"] and "US$14.70" in rech["00000003"] and "US$6 por operación" in rech["00000003"], rech
    assert {p[1].orderRef[4:12] for p in ib2.placed} == {"00000004", "00000005"}
    ex3, ib3, c3, p3, r3 = nuevo()                                                   # nunca lo sube
    r3["r"] = {"limites": {"riesgo_usd": 500}, "ordenes": [orden("00000006", "FFF", trail=0.5)]}
    ex3.paso()
    vuelta(ex3, c3, r3)
    assert ib3.placed == [] and "por operación" in evs(p3, "rechazada")[0]["motivo"]
    ex4, ib4, c4, p4, r4 = nuevo()                                                   # el stop llega redondeado a centavos: 5 % de margen
    r4["r"] = {"ordenes": [orden("00000007", "GGG", px=5.0, qty=99, trail=0.15)]}    # 99 × 0.15 = 14.85
    ex4.paso()
    assert len(ib4.placed) == 3 and not evs(p4, "rechazada")


def test_espera_ib():
    """v1.8: las llamadas bloqueantes a IBKR tienen tope de espera (sin él, un Gateway que no contesta cuelga al ejecutor)."""
    ex, ib, clock, posted, resp = nuevo()
    ex.conectar()
    assert ib.RequestTimeout == E.ESPERA_IB_S == 20


def test_anota_limites():
    """Los límites que manda el semáforo y los que quedan vigentes quedan en el registro (solo cuando cambian)."""
    import logging
    ex, ib, clock, posted, resp = nuevo()
    lineas = []

    class H(logging.Handler):
        def emit(self, record):
            lineas.append(record.getMessage())
    h, nivel = H(), E.log.level
    E.log.addHandler(h)
    E.log.setLevel(logging.INFO)
    try:
        estrictos = {"orden_usd": 300, "riesgo_usd": 6, "max_abierto": 800, "perdida_max": 80, "entrada_ini_m": 570,
                     "entrada_fin_m": 720, "cierre_m": 955}
        resp["r"] = {"limites": estrictos}
        ex.paso()
        vuelta(ex, clock, resp, r={"limites": dict(estrictos)})           # lo mismo otra vez: no se repite
        vuelta(ex, clock, resp, r={"limites": {"orden_usd": 5000, "riesgo_usd": 99, "max_abierto": 99999,
                                               "perdida_max": 9999, "entrada_ini_m": 570, "entrada_fin_m": 930,
                                               "cierre_m": 955}})
        vuelta(ex, clock, resp, r={})                                     # sin límites: no inventa ninguna línea
    finally:
        E.log.removeHandler(h)
        E.log.setLevel(nivel)
    l = [x for x in lineas if x.startswith("Límites del semáforo")]
    assert len(l) == 2, lineas
    assert "US$300 por operación (pérdida si salta el stop US$6)" in l[0] and "compras 9:30–12:00 ET" in l[0], l[0]
    assert "(el más estricto de los dos): US$300 (US$6) · US$800 · US$80 · compras 9:30–12:00 ET, cierre 15:55" in l[0], l[0]
    assert "US$500 (US$15) · US$1,000 · US$100 · compras 9:30–15:30 ET" in l[1], l[1]   # más holgado: rigen los propios


def test_penny_trailing_porcentaje():
    """v1.9 (estilo penny de Priamo): compra de ~US$1,000 con Trailing en % nativo de IBKR (el stop sube con el precio),
    sin orden de objetivo; se reconstruye tras un reinicio; los topes propios son 1,000 / 105 / 3,000 / 300."""
    ex, ib, clock, posted, resp = nuevo(penny=True)
    resp["r"] = {"ordenes": [orden_penny()]}
    ex.paso()
    assert len(ib.placed) == 2, ib.placed                                   # compra + Trailing; no hay hija de objetivo
    (c, e), (_, t) = ib.placed
    assert (e.action, e.orderType, e.totalQuantity, e.auxPrice, e.lmtPrice, e.transmit) == ("BUY", "STP LMT", 497, 2.0, 2.01, False)
    assert (t.action, t.orderType, t.totalQuantity, t.trailingPercent, t.parentId) == ("SELL", "TRAIL", 497, 10.0, e.orderId)
    assert t.transmit is True and t.tif == "DAY" and not (0 < (t.auxPrice or 0) < 1e300)   # transmite al final; IBKR calcula el monto
    oids = ex.ord["ab12cd34"]["oids"]
    assert set(oids) == {"e", "t"} and e.orderRef == "sem-ab12cd34-e" and t.orderRef == "sem-ab12cd34-t"
    vuelta(ex, clock, resp)
    (pu,) = evs(posted, "puesta")
    assert "Trailing 10 %" in pu["detalle"] and "sin objetivo" in pu["detalle"], pu
    assert posted[-1]["estado"]["comprometido"] == round(497 * 2.01, 2)
    ib.fill(oids["e"], 497, 2.00)
    vuelta(ex, clock, resp)
    vuelta(ex, clock, resp)
    assert len(ib.placed) == 2 and not ib.cancels                           # con su Trailing vivo no toca nada
    ib.fill(oids["t"], 497, 2.40)                                           # el Trailing subió con el precio y vendió
    vuelta(ex, clock, resp)
    vuelta(ex, clock, resp)
    (sa,) = evs(posted, "salida")
    assert sa["por"] == "trailing" and sa["px"] == 2.40 and sa["pnl"] == 198.8 and sa["px_e"] == 2.0, sa
    # reinicio sin archivo de estado: reconoce la orden por su referencia y se queda con el Trailing en %
    ex2, ib2, c2, p2, r2 = nuevo(penny=True)
    r2["r"] = {"ordenes": [orden_penny()]}
    ex2.paso()
    ib2.fill(ex2.ord["ab12cd34"]["oids"]["e"], 497, 2.0)
    ex3 = E.Ejecutor({"SEMAFORO_URL": "https://sem.test", "BRIDGE_TOKEN": "clave"}, ib2, post=lambda *a, **k: {},
                     reloj=c2, estado_path=tempfile.mktemp(suffix=".json"))
    ib2.conn = False
    ex3.paso()
    r = ex3.ord["ab12cd34"]
    assert r["t"] == "PNY" and r["estado"] == "llena" and r["qty"] == 497 and r["trail_pct"] == 10.0 and r["trail"] == 0.2, r
    # validaciones del ejecutor (por si el semáforo mandara algo raro)
    ex4, ib4, c4, p4, r4 = nuevo(penny=True)
    r4["r"] = {"ordenes": [orden_penny("00000001", "AAA"),
                           orden_penny("00000002", "BBB", pct=30.0),                # más del 25 %
                           orden_penny("00000003", "CCC", trail=0.50),               # el monto no concuerda con el %
                           orden_penny("00000004", "DDD", objetivo=2.5),             # con % no hay objetivo
                           orden_penny("00000005", "EEE", qty=600)]}                 # US$1,206 > 1,000
    ex4.paso()
    vuelta(ex4, c4, r4)
    rech = {x["id"]: x["motivo"] for x in evs(p4, "rechazada")}
    assert "Trailing en % inválido" in rech["00000002"] and "Trailing en % inválido" in rech["00000003"], rech
    assert "incompleta" in rech["00000004"] and "1,000 por operación" in rech["00000005"] and "00000001" not in rech, rech
    ex5, ib5, c5, p5, r5 = nuevo()                                                   # con los topes de antes (US$500) no pasa
    r5["r"] = {"ordenes": [orden_penny()]}
    ex5.paso()
    vuelta(ex5, c5, r5)
    assert ib5.placed == [] and "500 por operación" in evs(p5, "rechazada")[0]["motivo"]
    # hasta 3 abiertas (US$3,000 comprometidos) y no más
    ex6, ib6, c6, p6, r6 = nuevo(penny=True)
    r6["r"] = {"ordenes": [orden_penny(f"0000000{i}", t) for i, t in enumerate(("AAA", "BBB", "CCC", "DDD"), 1)]}
    ex6.paso()
    vuelta(ex6, c6, r6)
    assert len(ib6.placed) == 6 and "3,000 comprometidos" in evs(p6, "rechazada")[0]["motivo"]
    # el monto del Trailing va redondeado a centavos, pero el riesgo se cuenta con el 10 % exacto: tres de 295 acc a 3.38 caben
    ex8, ib8, c8, p8, r8 = nuevo(penny=True)
    r8["r"] = {"ordenes": [orden_penny(f"0000000{i}", t, px=3.37) for i, t in enumerate(("AAA", "BBB", "CCC"), 1)]}
    ex8.paso()
    assert len(ib8.placed) == 6 and not evs(p8, "rechazada", ex8), [x.get("motivo") for x in ex8.eventos]
    # 3 salidas a pérdida completa (~US$99 cada una) y la cuarta orden se rechaza por la pérdida del día (US$300)
    ex7, ib7, c7, p7, r7 = nuevo(penny=True)
    r7["r"] = {"ordenes": [orden_penny(f"0000000{i}", t) for i, t in enumerate(("AAA", "BBB", "CCC"), 1)]}
    ex7.paso()
    for i in (1, 2, 3):
        o7 = ex7.ord[f"0000000{i}"]["oids"]
        ib7.fill(o7["e"], 497, 2.0)
        ib7.fill(o7["t"], 497, 1.80)
    vuelta(ex7, c7, r7, r={"ordenes": [orden_penny("00000009", "ZZZ", px=1.0)]})
    vuelta(ex7, c7, r7)
    assert "pérdida máxima" in evs(p7, "rechazada")[-1]["motivo"] or "máximo del día" in evs(p7, "rechazada")[-1]["motivo"]


def test_hora_et():
    import datetime as dt
    u = dt.datetime(2026, 11, 2, 15, 0, tzinfo=dt.timezone.utc)          # después del cambio de hora
    assert E._ny_offset(u) == -5 and E._ny_offset(dt.datetime(2026, 10, 5, 15, 0, tzinfo=dt.timezone.utc)) == -4
    assert E._ny_offset(dt.datetime(2027, 3, 14, 6, 59, tzinfo=dt.timezone.utc)) == -5
    assert E._ny_offset(dt.datetime(2027, 3, 14, 7, 0, tzinfo=dt.timezone.utc)) == -4
    old = E.NY
    try:
        E.NY = None                                                      # Windows sin base de zonas
        assert E.et(T0).hour == 10 and E.gtd(T0) == "20261005 10:00:00 US/Eastern"
        assert E.et_ts(T0, 12 * 60) == at(12, 0)
    finally:
        E.NY = old


if __name__ == "__main__":
    test_candado()
    test_orden_con_hijas()
    test_llena_y_sale()
    test_limites()
    test_perdida_maxima()
    test_vence_y_parcial()
    test_llenado_tardio()
    test_sin_stop_y_de_mas()
    test_cierre_1555_y_cerrar()
    test_rechazos()
    test_red_y_reinicio()
    test_reconexion_tras_reinicio_del_gateway()
    test_el_corte_de_conexion_no_mata_el_bucle()
    test_el_error_real_de_ib_async()
    test_una_sola_copia()
    test_windows_bloquea_archivos()
    test_riesgo_por_operacion()
    test_espera_ib()
    test_anota_limites()
    test_penny_trailing_porcentaje()
    test_hora_et()
    print("OK · ejecutor paper")
