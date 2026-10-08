"""Pruebas sin red de las órdenes a mano por Telegram (live/ordenes_tg.py + live/tgbot.py + live/paper.py): el plan con su OK,
las validaciones, los topes por variable de entorno, posiciones / ordenes / cancelar, y que nada se envíe sin confirmar.
Uso: NO_LOOP=1 NO_NOTIFY=1 PYTHONPATH=. python tests/test_ordenes_tg.py"""
import os as _os
_os.environ.setdefault("RIESGO", "normal")
_os.environ.setdefault("PAPER_ESTILO", "normal")
_os.environ.setdefault("NO_LOOP", "1")
_os.environ.setdefault("NO_NOTIFY", "1")

import sys
from datetime import datetime
from unittest import mock

sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from scanner.util import ET  # noqa: E402
from live import paper as P  # noqa: E402
from live.ordenes_tg import OrdenesTg  # noqa: E402
from live.tgbot import TgIn  # noqa: E402
from test_paper import FakeHTTP, OK, Clock  # noqa: E402

T0 = datetime(2026, 10, 5, 10, 0, tzinfo=ET).timestamp()   # lunes 10:00 ET
ORD = "XYZ 10.00 stop 9.50 tp 12.00 riesgo 50"


def nuevo(env=None, conectado=True):
    c = Clock(T0)
    p = P.Paper(modo="boton", clock=c)
    if conectado:
        p.sync({**OK, "puerto": 4002}, [])
    return p, OrdenesTg(p, clock=c, env=env or {}), c


def di(ot, c, texto):
    resp, atendido = ot.manejar(texto)
    return "\n".join(resp), atendido


def penny():
    return mock.patch.object(P, "ESTILO", "penny")      # topes 1,000 / 100 / 3,000 / 300 (los de la orden de US$1,000)


def test_plan_ok_y_envio():
    with penny():
        p, ot, c = nuevo()
        txt, at = di(ot, c, ORD)
        assert at and "PAPER" in txt and "XYZ · comprar 100 acc" in txt, txt
        assert "Entrada límite 10 · Stop 9.5 · Objetivo 12" in txt and "Riesgo real US$50.00" in txt, txt
        assert "Valor US$1,000.00" in txt and "Premio/riesgo 4.0 : 1" in txt and "Responde OK" in txt and "cancela" in txt, txt
        assert p.ofertas == {} and p.sync(OK, [])["ordenes"] == []                # nada se envía sin OK
        txt, at = di(ot, c, "OK")
        assert at and "Enviada a IBKR paper" in txt and "PAPER" in txt, txt
        (o,) = p.ofertas.values()
        assert (o["kind"], o["t"], o["tipo"], o["limite"], o["stop"], o["objetivo"], o["qty"], o["trail"], o["estado"]) == \
            ("manual", "XYZ", "lmt", 10.0, 9.5, 12.0, 100, 0.5, "cola"), o
        assert o["hasta"] == P.et_ts(T0, P.ENTRADA_FIN_M)
        r = p.sync(OK, [])                                                         # al ejecutor llega con stop fijo
        (x,) = r["ordenes"]
        assert x["stop"] == 9.5 and x["objetivo"] == 12.0 and x["setup"] == "manual (Telegram)" and x["gatillo"] is None, x
        assert x["tipo"] == "lmt" and x["qty"] == 100 and x["trail"] == 0.5 and x["trail_pct"] is None, x
        assert di(ot, c, "OK")[0].endswith("No hay nada pendiente que confirmar.")  # un segundo OK no repite la orden
        assert len(p.ofertas) == 1
        # lo que cuenta Telegram cuando el ejecutor avisa
        p.sync(OK, [{"id": o["id"], "ev": "puesta", "t": "XYZ"}])
        p.sync({**OK, "comprometido": 1000.0}, [{"id": o["id"], "ev": "llena", "px": 10.0, "qty": 100}])
        p.sync(OK, [{"id": o["id"], "ev": "salida", "px": 9.5, "qty": 100, "por": "stop", "pnl": -50.0, "px_e": 10.0}])
        m = [x[1] for x in p.tomar_msgs()]
        assert "stop 9.50 · objetivo 12.00" in m[0] and "Lo cuidan el stop 9.50 y el objetivo 12.00" in m[1], m
        assert "vendí 100 XYZ a 9.50 por el stop" in m[2] and "-50.00 US$" in m[2], m


def test_validaciones():
    with penny():
        p, ot, c = nuevo()
        casos = [("XYZ 10.00 stop 10.00 tp 12.00 riesgo 50", "stop (10.00) debe estar por debajo"),
                 ("XYZ 10.00 stop 10.50 tp 12.00 riesgo 50", "debe estar por debajo"),
                 ("XYZ 10.00 stop 9.50 tp 10.00 riesgo 50", "debe estar por encima"),
                 ("XYZ 10.00 stop 9.50 tp 9.00 riesgo 50", "debe estar por encima"),
                 ("XYZ 10.00 stop 9.50 tp 12.00 riesgo 0.40", "no alcanza ni para 1 acción"),
                 ("XYZ 10.00 stop 9.50 tp 12.00 riesgo 0", "mayores que 0"),
                 ("XYZ 10.005 stop 9.50 tp 12.00 riesgo 50", "demasiados decimales"),
                 ("XYZ 10.00 stop 9.50 tp 12.00 riesgo 60", "máximo por orden"),         # 120 acc × 10 = US$1,200 > 1,000
                 ("XYZ 10.00 stop 9.00 tp 12.00 riesgo 150", "riesgo máximo")]            # 150 acc → US$1,500 > 1,000 primero
        for t, frag in casos[:-1]:
            txt, at = di(ot, c, t)
            assert at and txt.startswith("✋") and frag in txt and "PAPER" in txt and ot.pend is None, (t, txt)
        for t in ("ABCDEF 10 stop 9 tp 12 riesgo 50", "AB1 10 stop 9 tp 12 riesgo 50", "XYZ 10 stop 9.5", "XYZ 10,00 stop 9.5 tp 12 riesgo 50"):
            txt, at = di(ot, c, t)
            assert at and "No entendí la orden" in txt and EJEMPLO in txt, (t, txt)
        assert di(ot, c, "hola")[1] is False and di(ot, c, "hola")[0] == ""        # charla normal: nada
        txt, at = di(ot, c, "xyz 10 stop 9.5 tp 12 riesgo 50")                      # minúsculas y sin decimales valen
        assert "XYZ · comprar 100 acc" in txt
        # el riesgo real manda, no el pedido: riesgo 150 con stop a 0.10 → 1,500 acc (US$15,000) pasa de MAX_NOTIONAL
        txt, _ = di(ot, c, "XYZ 10.00 stop 9.90 tp 12.00 riesgo 150")
        assert "máximo por orden" in txt and "cabrían 100 acc" in txt, txt
        # riesgo máximo: tope propio de US$100 (penny); con MAX_RISK de Render más bajo, ese
        p2, ot2, c2 = nuevo(env={"MAX_RISK": "30"})
        txt, _ = di(ot2, c2, "XYZ 10.00 stop 9.50 tp 12.00 riesgo 50")
        assert "riesgo máximo por orden" in txt and "MAX_RISK US$30.00" in txt, txt
        p3, ot3, c3 = nuevo(env={"MAX_NOTIONAL": "500"})
        txt, _ = di(ot3, c3, "XYZ 10.00 stop 9.50 tp 12.00 riesgo 25")               # 50 acc = US$500: pasa
        assert "comprar 50 acc" in txt
        txt, _ = di(ot3, c3, "XYZ 10.00 stop 9.50 tp 12.00 riesgo 50")               # 100 acc = US$1,000 > 500
        assert "máximo por orden" in txt and "MAX_NOTIONAL US$500.00" in txt and "cabrían 50 acc" in txt, txt
        p4, ot4, c4 = nuevo(env={"MAX_NOTIONAL": "99999", "MAX_RISK": "99999"})      # las variables nunca suben los topes
        assert ot4.topes() == {"orden_usd": 1000.0, "riesgo_usd": 100.0}, ot4.topes()
        txt, _ = di(ot4, c4, "XYZ 10.00 stop 9.50 tp 12.00 riesgo 60")
        assert "máximo por orden" in txt
        txt, _ = di(ot4, c4, "ABC 0.4567 stop 0.4000 tp 0.60 riesgo 20")             # bajo US$1: hasta 4 decimales
        assert "comprar 352 acc" in txt, txt
        # sin ejecutor, en pausa o fuera de horario: se dice antes del OK, no después
        p5, ot5, c5 = nuevo(conectado=False)
        txt, _ = di(ot5, c5, ORD)
        assert "No la puedo enviar ahora" in txt and "ejecutor de tu PC no está conectado" in txt and ot5.pend is None, txt
        p6, ot6, c6 = nuevo()
        p6.comando("/pausa")
        assert "pausa" in di(ot6, c6, ORD)[0]
        p7, ot7, c7 = nuevo()
        c7.t = datetime(2026, 10, 5, 13, 0, tzinfo=ET).timestamp()
        p7.sync(OK, [])
        assert "fuera del horario" in di(ot7, c7, ORD)[0]
        p8, ot8, c8 = nuevo()                                                        # una sola por acción
        di(ot8, c8, ORD)
        di(ot8, c8, "OK")
        assert "ya hay una orden o posición en XYZ" in di(ot8, c8, ORD)[0]
        for k, t in enumerate(("AAA", "BBB", "CCC")):                                # XYZ + 2 más = US$3,000 comprometidos
            plan = di(ot8, c8, f"{t} 10.00 stop 9.50 tp 12.00 riesgo 50")[0]
            if k < 2:
                assert "Responde OK" in plan and "Enviada" in di(ot8, c8, "OK")[0], (t, plan)
            else:                                                                    # la tercera ya no cabe: se dice antes del OK
                assert plan.startswith("✋") and "3,000 comprometidos" in plan and ot8.pend is None, plan


EJEMPLO = "XYZ 10.00 stop 9.50 tp 12.00 riesgo 50"


def test_ok_vence_y_cualquier_otro_mensaje_cancela():
    with penny():
        p, ot, c = nuevo()
        assert "No hay nada pendiente" in di(ot, c, "ok")[0]
        di(ot, c, ORD)
        c.t += 121                                                                   # el plan vale 2 min
        txt, at = di(ot, c, "OK")
        assert at and "venció" in txt and p.ofertas == {}, txt
        p.sync({**OK, "puerto": 4002}, [])                                           # el ejecutor sigue hablando
        di(ot, c, ORD)
        txt, at = di(ot, c, "hola")                                                  # otro mensaje: cancela y sigue
        assert at is False and "Plan cancelado: no envié nada" in txt and ot.pend is None
        assert "No hay nada pendiente" in di(ot, c, "OK")[0] and p.ofertas == {}
        di(ot, c, ORD)
        txt, at = di(ot, c, "sí")                                                    # «sí», «dale», «okay»: no son OK
        assert "Plan cancelado" in txt and p.ofertas == {}
        di(ot, c, ORD)
        txt, at = di(ot, c, "ABC 5.00 stop 4.80 tp 6.00 riesgo 20")                   # otra orden: reemplaza al plan viejo
        assert at and "Plan cancelado" in txt and "ABC · comprar 100 acc" in txt, txt
        di(ot, c, "OK")
        assert [o["t"] for o in p.ofertas.values()] == ["ABC"]                        # solo se envió la última
        di(ot, c, "OK.")
        di(ot, c, "Ok!")
        assert len(p.ofertas) == 1


def test_posiciones_y_ordenes():
    with penny():
        p, ot, c = nuevo()
        assert "ninguna abierta" in di(ot, c, "posiciones")[0] and "ninguna" in di(ot, c, "ordenes")[0]
        di(ot, c, ORD)
        di(ot, c, "OK")
        txt = di(ot, c, "ordenes")[0]
        oid = next(iter(p.ofertas))
        assert oid in txt and "XYZ · 100 acc · compra 10.00 · stop 9.50 · objetivo 12.00 · en cola" in txt and "PAPER" in txt, txt
        p.sync(OK, [{"id": oid, "ev": "puesta", "t": "XYZ"}])
        vivas = [{"id": oid, "t": "XYZ", "qty": 100, "limite": 10.0, "stop": 9.5, "objetivo": 12.0, "estado": "puesta"}]
        p.sync({**OK, "ordenes_vivas": vivas}, [])
        assert "puesta en IBKR, sin llenar" in di(ot, c, "órdenes")[0]
        p.sync({**OK, "ordenes_vivas": [{**vivas[0], "estado": "llena"}], "posiciones": [{"t": "XYZ", "qty": 100, "px": 10.02}],
                "mercado": {"XYZ": 10.32}}, [{"id": oid, "ev": "llena", "px": 10.02, "qty": 100}])
        assert "comprada, protegida por su stop" in di(ot, c, "ordenes")[0]
        txt = di(ot, c, "/posiciones")[0]
        assert "XYZ · 100 acc · promedio 10.02 · precio 10.32 · P&L no realizado +30.00 US$" in txt and "PAPER" in txt, txt
        p.sync({**OK, "posiciones": [{"t": "XYZ", "qty": 100, "px": 10.02}, {"t": "ABC", "qty": 5, "px": 4.0}],
                "mercado": {"XYZ": 10.32}}, [])
        txt = di(ot, c, "posiciones")[0]
        assert "ABC · 5 acc · promedio 4.00 · P&L no realizado n/d" in txt and "Total no realizado" in txt, txt   # sin precio: no inventa
        c.t += 60                                                                                                   # ejecutor callado
        assert "no está conectado ahora" in di(ot, c, "posiciones")[0]
        # tras un reinicio del semáforo (no conoce la oferta) «ordenes» muestra lo que informa el ejecutor
        p2, ot2, c2 = nuevo()
        p2.sync({**OK, "ordenes_vivas": [{"id": "0a1b2c3d", "t": "QQQ", "qty": 3, "limite": 400.0, "stop": 395.0,
                                          "objetivo": 410.0, "estado": "puesta"}]}, [])
        assert "0a1b2c3d · QQQ · 3 acc · compra 400.00 · stop 395.00 · objetivo 410.00 · puesta" in di(ot2, c2, "ordenes")[0]


def test_cancelar():
    with penny():
        p, ot, c = nuevo()
        assert "No hay compras sin llenar" in di(ot, c, "cancelar")[0]
        di(ot, c, ORD)
        di(ot, c, "OK")
        oid = next(iter(p.ofertas))
        assert oid in di(ot, c, "cancelar")[0]                                       # sin argumento: lista cuáles
        assert "Uso: cancelar ID" in di(ot, c, "cancelar xyz!")[0]
        assert "No encontré" in di(ot, c, "cancelar ZZZ")[0]
        txt = di(ot, c, "cancelar xyz")[0]                                           # por ticker, pide OK
        assert "Cancelaría" in txt and oid in txt and "Responde OK" in txt and p.ofertas[oid]["estado"] == "cola", txt
        assert "descartada" in di(ot, c, "no")[0] and p.ofertas[oid]["estado"] == "cola"      # otro mensaje: no cambia nada
        di(ot, c, f"/cancelar {oid}")                                                # por id
        assert "Pedí cancelar 1 orden" in di(ot, c, "OK")[0] and p.ofertas[oid]["estado"] == "cancelada"   # en cola: no llegó a salir
        assert p.sync(OK, [])["ordenes"] == []
        # puesta en IBKR: se le pide al ejecutor
        di(ot, c, "ABC 5.00 stop 4.80 tp 6.00 riesgo 20")
        di(ot, c, "OK")
        o2 = [o for o in p.ofertas.values() if o["t"] == "ABC"][0]
        assert len(p.sync(OK, [])["ordenes"]) == 1
        p.sync(OK, [{"id": o2["id"], "ev": "puesta", "t": "ABC"}])
        di(ot, c, "cancelar ABC")
        di(ot, c, "ok")
        assert o2["id"] in p.sync(OK, [])["cancelar"]
        p.sync(OK, [{"id": o2["id"], "ev": "cancelada", "t": "ABC", "motivo": "cancelada"}])
        assert o2["estado"] == "cancelada"
        # ya comprada: no se cancela su protección
        di(ot, c, "DEF 8.00 stop 7.50 tp 9.00 riesgo 25")
        di(ot, c, "OK")
        o3 = [o for o in p.ofertas.values() if o["t"] == "DEF"][0]
        p.sync(OK, [{"id": o3["id"], "ev": "puesta", "t": "DEF"}])
        p.sync(OK, [{"id": o3["id"], "ev": "llena", "px": 8.0, "qty": 50}])
        txt = di(ot, c, "cancelar DEF")[0]
        assert "ya está comprada" in txt and "/cerrar" in txt and ot.pend is None, txt
        # orden que solo conoce el ejecutor (el semáforo se reinició): se le pide cancelar por id y deja de pedirse a los 5 min
        p2, ot2, c2 = nuevo()
        p2.sync({**OK, "ordenes_vivas": [{"id": "0a1b2c3d", "t": "QQQ", "qty": 3, "limite": 400.0, "stop": 395.0,
                                          "objetivo": 410.0, "estado": "puesta"}]}, [])
        di(ot2, c2, "cancelar QQQ")
        assert "Pedí cancelar 1" in di(ot2, c2, "OK")[0]
        assert p2.sync(OK, [])["cancelar"] == ["0a1b2c3d"]
        c2.t += 301
        assert p2.sync(OK, [])["cancelar"] == []


def test_telegram_solo_tu_chat():
    with penny():
        c = Clock(T0)
        p = P.Paper(modo="boton", clock=c)
        p.sync({**OK, "puerto": 4002}, [])
        msg = lambda i, text, user=555: {"update_id": i, "message": {"chat": {"id": 555}, "from": {"id": user},
                                                                     "date": T0, "text": text}}
        h = FakeHTTP([msg(100, ORD, user=777), msg(101, "OK", user=777), msg(102, ORD), msg(103, "Estado"), msg(104, "OK")])
        tg = TgIn(p, token="TKN", chat_id="555", http=h, clock=c, ordenes=OrdenesTg(p, clock=c, env={}))
        tg.poll_once(timeout=0)
        enviados = [x[1]["text"] for x in h.calls if x[0] == "sendMessage"]
        assert len(enviados) == 4, enviados                                           # el extraño no recibe ni una respuesta
        assert enviados[0].startswith("📝 Plan [PAPER]"), enviados
        # «Estado» es otro mensaje: cancela el plan pendiente y además responde el estado (con modo y puerto);
        # el OK de después ya no envía nada
        assert "Plan cancelado" in enviados[1] and "PAPER · IB Gateway puerto 4002" in enviados[2], enviados
        assert "No hay nada pendiente" in enviados[3] and p.ofertas == {}, enviados
        h.updates = [msg(105, ORD), msg(106, "ok")]
        tg.poll_once(timeout=0)
        assert len(p.ofertas) == 1
        sin = TgIn(p, token="TKN", chat_id="555", http=FakeHTTP([msg(107, ORD)]), clock=c)   # sin OrdenesTg: ignora texto como antes
        sin.poll_once(timeout=0)
        assert [x for x in sin.http.calls if x[0] == "sendMessage"] == []


if __name__ == "__main__":
    test_plan_ok_y_envio()
    test_validaciones()
    test_ok_vence_y_cualquier_otro_mensaje_cancela()
    test_posiciones_y_ordenes()
    test_cancelar()
    test_telegram_solo_tu_chat()
    print("OK · órdenes a mano por Telegram")
