"""Pruebas del ejecutor de Claude (bridge/ejecutor_claude.py) con un IBKR y un lector de Yahoo falsos: la orden con stop
fijo y objetivo, que no toca nada de otro ejecutor (ni de Priamo a mano), que sin contacto con el semáforo o en pausa no
opera, que cancela la compra si la jugada se rompe, el diario, el cierre de las 15:55 y los reinicios.
Uso: PYTHONPATH=. python tests/test_claude_ejecutor.py"""
import copy
import csv
import os
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bridge"))
sys.path.insert(0, HERE)
import ejecutor_claude as C  # noqa: E402
import ejecutor_paper as E  # noqa: E402
import estrategia_claude as S  # noqa: E402
import test_ejecutor as T  # noqa: E402
from test_claude_estrategia import barras_lider  # noqa: E402

NY = ZoneInfo("America/New_York")
HOY = "2026-10-05"
T1 = datetime(2026, 10, 5, 10, 24, tzinfo=NY).timestamp()      # lunes 10:24 ET; la última vela completa es la de las 10:22


def at(h, m, s=0):
    return datetime(2026, 10, 5, h, m, s, tzinfo=NY).timestamp()


class FakeIB(T.FakeIB):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.ajenas, self.extra_pos = [], []

    def reqAllOpenOrders(self):
        return list(self._trades.values()) + list(self.ajenas)

    def positions(self):
        neto = {}
        for f in self._fills:
            s = f.contract.symbol
            neto[s] = neto.get(s, 0) + (f.execution.shares if f.execution.side == "BOT" else -f.execution.shares)
        return [NS(contract=NS(symbol=s), position=q) for s, q in neto.items() if q] + list(self.extra_pos)


class FakeFeed:
    def __init__(self):
        self.ult_barrido, self.n_ok, self.n_total, self.error = 100.0, 10, 10, None
        self.activo = True
        self.datos = {}
        bars = [(570 + i, 500.0, 500.6, 499.8, 500.0 + i * 0.03, 1e6) for i in range(53)]
        self.spy = {"bars": bars, "dia": HOY, "prev": 499.0, "ts": T1}

    def poner(self, t, bars, prev=100.0, adv=10e6, atr=0.03):
        self.datos[t] = {"bars": bars, "dia": HOY, "ts": T1, "prev": prev, "adv": adv, "atr": atr}

    def ok(self, now=None):
        return self.activo

    def snapshot(self, hoy=None):
        return dict(self.datos), dict(self.spy)

    def estado(self, now=None):
        return {"feed_ok": self.activo, "feed_n": 10, "feed_de": 10, "feed_edad_s": 5, "feed_error": None}

    def nueva_lectura(self):
        self.ult_barrido += 60


def escala(bars, k):
    return [(m, o * k, h * k, low * k, c * k, v) for m, o, h, low, c, v in bars]


def nuevo(cuentas=("DUR233329",), now=T1, path=None, lideres=("NVDA",)):
    ib, clock, posted, feed = FakeIB(cuentas), T.Clock(now), [], FakeFeed()
    base, _ = barras_lider()
    for t in lideres:
        feed.poner(t, base)
    resp = {"r": {}}

    def post(url, token, payload, timeout=10):
        assert url == "https://sem.test/api/claude/sync" and token == "clave", url
        posted.append(copy.deepcopy(payload))
        if isinstance(resp["r"], Exception):
            raise resp["r"]
        return resp["r"]
    path = path or tempfile.mktemp(suffix=".json")
    ex = C.EjecutorClaude({"SEMAFORO_URL": "https://sem.test/", "BRIDGE_TOKEN": "clave"}, ib, feed, post=post,
                          reloj=clock, estado_path=path)
    return ex, ib, clock, posted, resp, feed


def evs(posted, ev=None, ex=None):
    return T.evs(posted, ev, ex)


def vuelta(ex, clock, resp, feed=None, dt=1, r=None, nueva=False):
    clock.t += dt
    resp["r"] = r if r is not None else {}
    if nueva and feed:
        feed.nueva_lectura()
    return ex.paso()


def test_orden_con_stop_fijo():
    ex, ib, clock, posted, resp, feed = nuevo()
    ex.paso()
    assert ib.connects == [("127.0.0.1", 4002, 42)]                       # paper, y su propio clientId
    assert len(ib.placed) == 3, ib.placed
    (c, e), (_, t), (_, o) = ib.placed[:3]
    j, _ = S.evaluar("NVDA", feed.datos["NVDA"]["bars"], 100.0, 10e6, 0.03)
    assert c.symbol == "NVDA" and e.orderRef.startswith("cla-") and e.orderRef.endswith("-e")
    assert (e.action, e.orderType, e.totalQuantity, e.lmtPrice, e.tif) == ("BUY", "LMT", j["qty"], j["limite"], "GTD")
    assert e.goodTillDate == "20261005 10:34:00 US/Eastern" and e.transmit is False       # espera 10 min
    assert (t.action, t.orderType, t.auxPrice, t.parentId, t.tif, t.transmit) == ("SELL", "STP", j["stop"], e.orderId, "DAY", False)
    assert t.orderRef == e.orderRef[:-1] + "t"
    assert (o.action, o.orderType, o.lmtPrice, o.parentId, o.transmit) == ("SELL", "LMT", j["objetivo"], e.orderId, True)
    puesta = evs(posted, "puesta", ex)
    assert len(puesta) == 1 and "stop" in puesta[0]["detalle"] and "pullback" in puesta[0]["detalle"], puesta
    est = (posted or [{"estado": {}}])[-1]["estado"]
    assert est.get("estrategia") == "pullback con tendencia" and est["feed_ok"] is True
    vuelta(ex, clock, resp, feed)                                          # sin lectura nueva: no repite
    vuelta(ex, clock, resp, feed, nueva=True)                              # con lectura nueva: ya hay una orden en NVDA
    assert len(ib.placed) == 3
    assert posted[-1]["estado"]["vivas_t"] == ["NVDA"] and posted[-1]["estado"]["comprometido"] > 0
    assert not [e_ for e_ in evs(posted, "rechazada", ex)]                  # lo omitido no hace ruido


def test_no_toca_lo_ajeno():
    ex, ib, clock, posted, resp, feed = nuevo()
    ib.ajenas = [NS(contract=NS(symbol="NVDA"), order=NS(orderRef="sem-aaaaaaaa-e"))]       # orden del ejecutor de Priamo
    ex.paso()
    assert ib.placed == [] and not evs(posted, "rechazada", ex)
    ex2, ib2, c2, p2, r2, f2 = nuevo()
    ib2.extra_pos = [NS(contract=NS(symbol="NVDA"), position=7)]                              # posición que no es mía
    ex2.paso()
    assert ib2.placed == []
    ex3, ib3, c3, p3, r3, f3 = nuevo()
    ib3.ajenas = [NS(contract=NS(symbol="AMD"), order=NS(orderRef="sem-aaaaaaaa-e"))]        # en otra acción: sin problema
    ex3.paso()
    assert len(ib3.placed) == 3
    ex4, ib4, c4, p4, r4, f4 = nuevo()
    ib4.reqAllOpenOrders = lambda: (_ for _ in ()).throw(RuntimeError("sin red"))            # si no puede comprobar, no opera
    ex4.paso()
    assert ib4.placed == []


def test_no_se_mezcla_con_sem():
    """Lo del ejecutor de Priamo (sem-…) no cuenta en mis posiciones ni mis límites, y yo no vendo ni cancelo nada suyo."""
    ex, ib, clock, posted, resp, feed = nuevo(lideres=("NVDA",))
    ajena = ib.placeOrder(NS(symbol="MU"), E.Order(action="BUY", totalQuantity=5, orderType="LMT", orderRef="sem-aaaaaaaa-e"))
    ib.fill(ajena.order.orderId, 5, 100.0)
    ex.paso()
    assert "aaaaaaaa" not in ex.ord and posted[-1]["estado"]["posiciones"] == []
    clock.t = at(15, 55)
    ex.paso()
    vuelta(ex, clock, resp, dt=3)
    assert not any(p[1].orderType == "MKT" for p in ib.placed)               # no vende lo de sem
    assert ajena.order.orderId not in ib.cancels


def test_sin_contacto_pausa_y_horario():
    ex, ib, clock, posted, resp, feed = nuevo()
    resp["r"] = ConnectionError("el semáforo no responde")                    # sin el botón de parada no opera
    ex.paso()
    assert ib.placed == []
    vuelta(ex, clock, resp, feed, nueva=True)
    assert len(ib.placed) == 3                                                # vuelve el contacto: opera
    ex2, ib2, c2, p2, r2, f2 = nuevo()
    r2["r"] = {"pausa": True}
    ex2.paso()
    assert ib2.placed == []
    ex3, ib3, c3, p3, r3, f3 = nuevo()
    f3.activo = False                                                         # Yahoo sin datos
    ex3.paso()
    assert ib3.placed == [] and ex3.ult_sin_datos == c3.t                      # y lo deja en el registro...
    c3.t += 60
    ex3.paso()
    assert ex3.ult_sin_datos == c3.t - 60                                     # ...una vez cada 5 minutos
    c3.t += 300
    ex3.paso()
    assert ex3.ult_sin_datos == c3.t and ib3.placed == []
    for h, m_ in ((9, 40), (15, 20)):                                         # fuera de 9:50–15:15
        ex4, ib4, c4, p4, r4, f4 = nuevo(now=at(h, m_))
        ex4.paso()
        assert ib4.placed == [], (h, m_)
    ex5, ib5, c5, p5, r5, f5 = nuevo(now=datetime(2026, 10, 3, 10, 24, tzinfo=NY).timestamp())   # sábado
    ex5.paso()
    assert ib5.placed == []
    ex6, ib6, c6, p6, r6, f6 = nuevo()
    f6.spy = {**f6.spy, "prev": 520.0}                                       # SPY -4 %: pánico
    ex6.paso()
    assert ib6.placed == []
    ex7, ib7, c7, p7, r7, f7 = nuevo(now=T1 + 300)                            # los datos se quedaron viejos (>4 min)
    ex7.paso()
    assert ib7.placed == []
    ex8, ib8, c8, p8, r8, f8 = nuevo(cuentas=("U1234567",))                   # el candado también vale aquí
    ex8.paso()
    assert ib8.placed == [] and "no es paper" in ex8.bloqueado


def test_llena_y_sale():
    for sale, por, signo in (("stop", "stop", -1), ("o", "objetivo", 1)):
        ex, ib, clock, posted, resp, feed = nuevo()
        ex.paso()
        oid = next(iter(ex.ord))
        oids = ex.ord[oid]["oids"]
        e, qty = ib._trades[oids["e"]].order, ib._trades[oids["e"]].order.totalQuantity
        ib.fill(oids["e"], qty, e.lmtPrice)
        vuelta(ex, clock, resp, feed)
        vuelta(ex, clock, resp, feed)
        (ll,) = evs(posted, "llena", ex)
        assert ll["qty"] == qty and ex.ord[oid]["estado"] == "llena"
        assert len(ib.placed) == 3 and not ib.cancels                         # con su stop vivo no toca nada
        rol = "t" if sale == "stop" else "o"
        px = ib._trades[oids[rol]].order.auxPrice if rol == "t" else ib._trades[oids[rol]].order.lmtPrice
        ib.fill(oids[rol], qty, px)
        vuelta(ex, clock, resp, feed)
        vuelta(ex, clock, resp, feed)
        (s,) = evs(posted, "salida", ex)
        assert s["por"] == por and s["px"] == px and (s["pnl"] > 0) == (signo > 0), s
        est = posted[-1]["estado"]
        assert est["ops"] == 1 and est["gan"] == (1 if signo > 0 else 0) and est["posiciones"] == [], est
        with open(ex.diario, encoding="utf-8") as f:
            filas = list(csv.DictReader(f))
        assert len(filas) == 1 and filas[0]["ejecutor"] == "claude" and filas[0]["evento"] == "salida", filas
        assert filas[0]["simbolo"] == "NVDA" and float(filas[0]["R"]) != 0 and filas[0]["por"] == por
        assert "pullback" in filas[0]["setup"]


def test_se_cancela_si_la_jugada_se_rompe():
    ex, ib, clock, posted, resp, feed = nuevo()
    ex.paso()
    oid = next(iter(ex.ord))
    e_id = ex.ord[oid]["oids"]["e"]
    stop = ex.ord[oid]["stop"]
    bars = list(feed.datos["NVDA"]["bars"])
    bars.append((623, stop + 0.5, stop + 0.6, stop - 0.3, stop - 0.2, 60_000))     # cierra bajo el stop sin haberse llenado
    feed.poner("NVDA", bars)
    clock.t = at(10, 25)
    ex.paso()
    assert e_id in ib.cancels and ex.ord[oid]["estado"] == "cancelando"
    vuelta(ex, clock, resp, feed)
    assert ex.ord[oid]["estado"] == "cancelada"
    (c,) = evs(posted, "cancelada", ex)
    assert "perdió el stop" in c["motivo"]
    # pánico del mercado: se cancela lo pendiente
    ex2, ib2, c2, p2, r2, f2 = nuevo()
    ex2.paso()
    f2.spy = {**f2.spy, "prev": 540.0}
    vuelta(ex2, c2, r2, f2, nueva=True)
    assert ib2.cancels and "0.5" in ex2.ord[next(iter(ex2.ord))]["motivo"]
    # una compra ya llenada NO se cancela por esto: la cuida su stop
    ex3, ib3, c3, p3, r3, f3 = nuevo()
    ex3.paso()
    o3 = ex3.ord[next(iter(ex3.ord))]
    ib3.fill(o3["oids"]["e"], o3["qty"], o3["limite"])
    f3.spy = {**f3.spy, "prev": 540.0}
    vuelta(ex3, c3, r3, f3, nueva=True)
    vuelta(ex3, c3, r3, f3)
    assert o3["oids"]["t"] not in ib3.cancels and o3["estado"] == "llena"


def test_topes_y_vetos():
    ex, ib, clock, posted, resp, feed = nuevo(lideres=("AAA", "BBB", "CCC"))
    base, _ = barras_lider()
    feed.poner("BBB", escala(base, 0.5), prev=50.0, adv=10e6)
    feed.poner("CCC", escala(base, 0.25), prev=25.0, adv=10e6)
    ex.paso()
    entradas = [p for p in ib.placed if p[1].orderRef.endswith("-e")]
    assert len(entradas) == 2, [p[1].orderRef for p in ib.placed]            # máximo dos compras pendientes (y US$1,000)
    # IBKR rechaza una acción que no conoce: queda vetada un rato, sin repetir el ruido cada minuto
    ex2, ib2, c2, p2, r2, f2 = nuevo(lideres=("NOPE",))
    ex2.paso()
    vuelta(ex2, c2, r2, f2)
    assert ib2.placed == [] and len(evs(p2, "rechazada", ex2)) == 1
    vuelta(ex2, c2, r2, f2, nueva=True)
    vuelta(ex2, c2, r2, f2, dt=60, nueva=True)
    assert len(evs(p2, "rechazada", ex2)) == 1 and len(ex2.ord) == 1, len(ex2.ord)
    # máximo dos entradas por acción al día
    ex3, ib3, c3, p3, r3, f3 = nuevo()
    for i in range(3):
        c3.t = T1 + 70 * i
        f3.nueva_lectura()
        ex3.paso()
        vivas = [o for o in ex3.ord.values() if o["estado"] == "puesta"]
        for o in vivas:
            ex3.cancelar(o["id"], "prueba")
        ib3.kill(vivas[0]["oids"]["e"]) if vivas else None
        c3.t += 2
        ex3.paso()
    assert sum(1 for p in ib3.placed if p[1].orderRef.endswith("-e")) == 2


def test_cierre_1555_y_reinicio():
    path = tempfile.mktemp(suffix=".json")
    ex, ib, clock, posted, resp, feed = nuevo(path=path)
    ex.paso()
    oid = next(iter(ex.ord))
    oids = ex.ord[oid]["oids"]
    qty = ib._trades[oids["e"]].order.totalQuantity
    ib.fill(oids["e"], qty, ib._trades[oids["e"]].order.lmtPrice)
    vuelta(ex, clock, resp, feed)
    clock.t = at(15, 55)
    ex.paso()
    assert oids["t"] in ib.cancels and oids["o"] in ib.cancels and ex.cerrado_hoy
    vuelta(ex, clock, resp, dt=2)
    venta = ib.placed[-1][1]
    assert (venta.action, venta.orderType, venta.totalQuantity, venta.orderRef) == ("SELL", "MKT", qty, f"cla-{oid}-x")
    ib.fill(venta.orderId, qty, 103.0)
    vuelta(ex, clock, resp)
    vuelta(ex, clock, resp)
    assert evs(posted, "salida", ex)[-1]["por"] == "cierre"
    # reinicio sin el archivo de estado: reconoce lo suyo por la referencia, con su stop fijo
    ex2, ib2, c2, p2, r2, f2 = nuevo()
    ib2.ajenas = []
    ib2.next_id = 500
    ex2.paso()
    oid2 = next(iter(ex2.ord))
    o2 = ex2.ord[oid2]
    ib2.fill(o2["oids"]["e"], o2["qty"], o2["limite"])
    ex3 = C.EjecutorClaude({"SEMAFORO_URL": "https://sem.test", "BRIDGE_TOKEN": "clave"}, ib2, f2,
                           post=lambda *a, **k: {}, reloj=c2, estado_path=tempfile.mktemp(suffix=".json"))
    ib2.conn = False
    ex3.paso()
    r = ex3.ord[oid2]
    assert r["t"] == "NVDA" and r["qty"] == o2["qty"] and r["stop"] == o2["stop"] and r["trail"] == o2["trail"], r
    assert r["limite"] == o2["limite"] and r["estado"] == "llena"
    assert not any(p[1].orderType == "MKT" for p in ib2.placed)               # con su stop vivo no vende nada
    # la lista de acciones
    p = tempfile.mktemp(suffix=".txt")
    with open(p, "w") as f:
        f.write("# mi lista\nnvda\nAMD  # comentario\nmal!\n\nNVDA\n")
    assert C.cargar_lista(p) == ["NVDA", "AMD"] and "NVDA" in C.cargar_lista("no_existe.txt")


if __name__ == "__main__":
    test_orden_con_stop_fijo()
    test_no_toca_lo_ajeno()
    test_no_se_mezcla_con_sem()
    test_sin_contacto_pausa_y_horario()
    test_llena_y_sale()
    test_se_cancela_si_la_jugada_se_rompe()
    test_topes_y_vetos()
    test_cierre_1555_y_reinicio()
    print("OK · ejecutor de Claude")
