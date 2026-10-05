"""Pruebas sin red de la ejecución paper del lado del semáforo: live/paper.py (ofertas, límites, cola, eventos,
comandos), live/tgbot.py (botón y comandos de Telegram) y su conexión con los avisos y con /api/paper/sync.
Uso: NO_LOOP=1 NO_NOTIFY=1 PYTHONPATH=. python tests/test_paper.py"""
import os as _os
_os.environ.setdefault("RIESGO", "normal")
_os.environ.setdefault("NO_LOOP", "1")
_os.environ.setdefault("NO_NOTIFY", "1")

from datetime import datetime
from types import SimpleNamespace as NS

from scanner.util import ET
from live import paper as P
from live.tgbot import TgIn

T0 = datetime(2026, 10, 5, 10, 0, tzinfo=ET).timestamp()  # lunes 10:00 ET
OK = {"ib": True, "paper": True, "cuenta": "DUR233329", "comprometido": 0, "riesgo_abierto": 0, "pnl_dia": 0,
      "perdida_dia": 0}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def nuevo(modo="boton"):
    c = Clock(T0)
    return P.Paper(modo=modo, clock=c), c


def arma(p, t="ABC", px=10.0, risk=0.03, hasta=T0 + 3600):
    return p.ofrecer("arma", t, px, round(px * (1 - risk), 2), round(px * 1.05, 2), px * 1.003, gatillo=px, hasta=hasta)


def test_sin_ejecutor_no_hay_boton():
    p, c = nuevo()
    assert arma(p) is None                                              # el ejecutor nunca habló
    p.sync(OK, [])
    o = arma(p)
    assert P.ORDEN_USD == 1000 and int(P.ORDEN_USD // 10.03) == 99
    assert o["boton"] and o["qty"] == 99 and o["tipo"] == "stp" and o["gatillo"] == 10.0 and o["limite"] == 10.03, o
    assert o["trail"] == 0.3 and o["objetivo"] == 10.5, o
    kb = P.boton(o)
    assert kb["inline_keyboard"][0][0]["callback_data"] == f"x:{o['id']}" and "99 acc" in kb["inline_keyboard"][0][0]["text"]
    c.t += 30                                                            # 30 s sin noticias del ejecutor
    assert arma(p, "XYZ") is None
    c.t = T0
    p.sync({**OK, "paper": False}, [])                                   # cuenta que no es paper
    assert arma(p, "XYZ") is None
    p.sync({**OK, "ib": False}, [])                                      # ejecutor sin IB Gateway
    assert arma(p, "XYZ") is None
    p.sync(OK, [])
    assert p.ofrecer("arma", "BIG", 1200, 1180, 1260, 1203.6, gatillo=1200) is None   # US$1,000 no alcanza para 1 acción
    assert p.ofrecer("arma", "BAD", 10, 10, 10.5, 10.03, gatillo=10) is None      # stop inválido


def test_flujo_boton():
    p, c = nuevo()
    p.sync(OK, [])
    o = arma(p)
    ok, txt = p.pedir(o["id"])
    assert ok and "ABC 99 acc" in txt, txt
    ok, txt = p.pedir(o["id"])
    assert not ok and "camino" in txt                                    # doble toque: una sola orden
    r = p.sync(OK, [])
    assert [x["id"] for x in r["ordenes"]] == [o["id"]] and r["ordenes"][0]["gatillo"] == 10.0, r
    assert r["limites"]["orden_usd"] == 1000 and r["limites"]["max_abierto"] == 4000 and r["limites"]["perdida_max"] == 250
    assert r["cerrar_id"] is None
    assert len(p.sync(OK, [])["ordenes"]) == 1                           # se repite hasta que el ejecutor avise
    r = p.sync(OK, [{"id": o["id"], "ev": "puesta", "t": "ABC"}])
    assert r["ordenes"] == [] and p.ofertas[o["id"]]["estado"] == "puesta"
    (k, m), = p.tomar_msgs()
    assert k == f"paper:{o['id']}:puesta" and m.startswith("🟦") and "compra stop 10.00 (límite 10.03)" in m, m
    p.sync({**OK, "comprometido": 491.5}, [{"id": o["id"], "ev": "llena", "px": 10.02, "qty": 49}])
    p.sync(OK, [{"id": o["id"], "ev": "salida", "px": 10.45, "qty": 49, "por": "trailing", "pnl": 21.07,
                 "px_e": 10.02}])
    m = [x[1] for x in p.tomar_msgs()]
    assert "compré 49 ABC a 10.02" in m[0], m
    assert m[1].startswith("✅") and "stop que sube" in m[1] and "+4.3%" in m[1] and "+21.07 US$" in m[1], m
    assert p.ofertas[o["id"]]["estado"] == "cerrada"
    p.sync(OK, [{"id": o["id"], "ev": "ack", "estado": "puesta"}])       # un ack viejo no retrocede el estado
    assert p.ofertas[o["id"]]["estado"] == "cerrada"


def test_limites():
    p, c = nuevo()
    p.sync(OK, [])
    assert P.MAX_ABIERTO == 4000 and P.PERDIDA_MAX == 250
    cuatro = [arma(p, t, 10.0) for t in ("AAA", "BBB", "CCC", "DDD")]    # 99 acciones: ~US$993 cada una
    quinta = arma(p, "EEE", 10.0)
    assert all(p.pedir(o["id"])[0] for o in cuatro)                      # caben 4 en US$4,000
    ok, txt = p.pedir(quinta["id"])
    assert not ok and "4,000 comprometidos" in txt, txt                  # la quinta pasaría de US$4,000
    a2 = p.ofrecer("compra", "AAA", 10.1, 9.8, 10.6, 10.13)
    assert a2["estado"] == "omitida" and "ya hay una orden" in a2["nota"] and not a2.get("boton")  # una por acción
    p.sync({**OK, "comprometido": 900}, [])                              # lo que el ejecutor ya tiene puesto cuenta
    p2, c2 = nuevo()
    p2.sync({**OK, "perdida_dia": 240}, [])
    o = arma(p2, "EEE")
    ok, txt = p2.pedir(o["id"])
    assert not ok and "pérdida máxima" in txt, txt                       # 240 perdidos + 29.7 de riesgo > 250
    p2.sync({**OK, "parado": True}, [])
    ok, txt = p2.pedir(o["id"])
    assert not ok and "máximo del día" in txt, txt
    p3, c3 = nuevo()
    c3.t = datetime(2026, 10, 5, 12, 5, tzinfo=ET).timestamp()
    p3.sync(OK, [])
    o = arma(p3, "FFF", hasta=c3.t + 600)
    ok, txt = p3.pedir(o["id"])
    assert not ok and "horario" in txt, txt                              # sin compras después de las 12:00 ET
    p4, c4 = nuevo()
    p4.sync(OK, [])
    o = p4.ofrecer("ruptura", "GGG", 10.0, 9.7, 10.5, 10.03)
    c4.t += 100
    p4.sync(OK, [])
    ok, txt = p4.pedir(o["id"])
    assert not ok and "venció" in txt, txt                               # el botón de una ruptura vale 90 s
    p5, c5 = nuevo()
    p5.sync(OK, [])
    o = arma(p5, "HHH")
    p5.comando("/pausa")
    ok, txt = p5.pedir(o["id"])
    assert not ok and "pausa" in txt
    p5.comando("/reanuda")
    assert p5.pedir(o["id"])[0]


def test_modo_automatico():
    p, c = nuevo("auto")
    p.sync(OK, [])
    o = arma(p, "AAA")
    assert o["estado"] == "cola" and "automático" in o["nota"] and P.boton(o) is None, o
    assert len(p.sync(OK, [])["ordenes"]) == 1
    p.comando("/pausa")
    o2 = arma(p, "BBB")
    assert o2["estado"] == "omitida" and "pausa" in o2["nota"], o2
    txt, _ = p.comando("/boton")
    assert p.modo == "boton" and "Modo botón" in txt
    txt, _ = p.comando("/auto")
    assert p.modo == "auto" and "automático" in txt


def test_cancelar_ticker():
    p, c = nuevo()
    p.sync(OK, [])
    a, b, d, e = arma(p, "AAA"), arma(p, "BBB"), arma(p, "DDD", 15.0), arma(p, "EEE", 5.0)
    p.pedir(d["id"])
    p.sync(OK, [{"id": e["id"], "ev": "llena", "px": 5.01, "qty": 99}])  # DDD ya salió al ejecutor; EEE ya compró
    p.pedir(b["id"])                                                     # BBB en cola, aún no salió
    for t in ("AAA", "BBB", "DDD", "EEE"):
        p.cancelar_ticker(t, "perdió el stop sin romper")
    ok, txt = p.pedir(a["id"])
    assert not ok and "Ya no vale" in txt                                # la oferta murió con el aviso
    r = p.sync(OK, [])
    assert p.ofertas[b["id"]]["estado"] == "cancelada" and b["id"] not in [x["id"] for x in r["ordenes"]]
    assert r["cancelar"] == [d["id"]], r                                 # la enviada se manda a cancelar...
    assert d["id"] not in [x["id"] for x in r["ordenes"]]                # ...y ya no se reenvía para ponerla
    assert e["id"] not in r["cancelar"] and p.ofertas[e["id"]]["estado"] == "llena"   # lo comprado sigue con su stop


def test_cerrar():
    p, c = nuevo()
    p.sync(OK, [])
    enviada = arma(p, "EEE")
    p.pedir(enviada["id"])
    p.sync(OK, [])                                                       # ya salió al ejecutor
    txt, kb = p.comando("/cerrar")
    data = kb["inline_keyboard"][0][0]["callback_data"]
    assert "¿Cierro todo" in txt and data.startswith("c:si:"), data
    assert p.sync(OK, [])["cerrar_id"] is None                           # pedir no basta: hay que confirmar
    assert "venció" in p.confirmar_cierre("otro")                        # un botón de otro /cerrar no cierra
    assert "venció" in p.confirmar_cierre(None)                          # ni uno viejo sin código
    o = arma(p, "AAA")
    assert "Cerrando" in p.confirmar_cierre(data[5:])
    c.t += 2
    r = p.sync(OK, [])
    assert r["cerrar_id"] and r["cerrar_hace_s"] == 2.0, r
    assert not p.pedir(o["id"])[0]                                       # las ofertas abiertas mueren con el cierre
    assert enviada["id"] in r["cancelar"] and r["ordenes"] == []         # la que iba en camino se cancela
    assert "venció" in p.confirmar_cierre(data[5:])                      # el mismo botón no sirve dos veces
    txt, kb = p.comando("/cerrar")
    c.t += 121
    assert "venció" in p.confirmar_cierre(kb["inline_keyboard"][0][0]["callback_data"][5:])   # vale 2 min


def test_reinicio_adopta_pausa():
    """Si el semáforo se reinicia (redeploy), la pausa y el modo los recupera del ejecutor..."""
    p, c = nuevo()
    p.sync({**OK, "pausa": True, "modo": "auto"}, [])
    assert p.pausa and p.modo == "auto"
    p.comando("/reanuda")
    p.sync({**OK, "pausa": True, "modo": "auto"}, [])                    # solo en el primer contacto
    assert not p.pausa
    p2, c2 = nuevo()                                                     # ...salvo que ya le hayas dado una orden
    p2.comando("/pausa")
    p2.comando("/boton")
    p2.sync({**OK, "pausa": False, "modo": "auto"}, [])
    assert p2.pausa and p2.modo == "boton"


def test_cancelada_sin_llegar():
    """Una orden que se canceló antes de que el ejecutor la recibiera no queda «en camino» para siempre."""
    p, c = nuevo()
    p.sync(OK, [])
    o = arma(p, "ABC")
    p.pedir(o["id"])
    p.sync(OK, [])                                                       # salió (pero el ejecutor la perdió)
    p.cancelar_ticker("ABC", "perdió el stop sin romper")
    r = p.sync(OK, [])
    assert r["cancelar"] == [o["id"]] and r["ordenes"] == []
    r = p.sync(OK, [{"id": o["id"], "ev": "cancelada", "motivo": "se canceló antes de llegar a IBKR"}])
    assert p.ofertas[o["id"]]["estado"] == "cancelada" and r["cancelar"] == [] and not p.viva("ABC")
    p2, c2 = nuevo()                                                     # y si el ejecutor no dice nada, a los 2 min
    p2.sync(OK, [])
    o2 = arma(p2, "ABC")
    p2.pedir(o2["id"])
    p2.sync(OK, [])
    p2.cancelar_ticker("ABC", "x")
    c2.t += 121
    p2.sync(OK, [])
    assert p2.ofertas[o2["id"]]["estado"] == "cancelada" and not p2.viva("ABC")
    p3, c3 = nuevo()                                                     # un ack con el final le llega aunque se perdiera el evento
    p3.sync(OK, [])
    o3 = arma(p3, "ABC")
    p3.pedir(o3["id"])
    p3.sync(OK, [{"id": o3["id"], "ev": "ack", "estado": "puesta"}])
    p3.cancelar_ticker("ABC", "x")
    p3.sync(OK, [{"id": o3["id"], "ev": "ack", "estado": "cancelada"}])
    assert p3.ofertas[o3["id"]]["estado"] == "cancelada"


def test_eventos_y_estado():
    p, c = nuevo()
    p.sync({**OK, "vivas_t": ["QQQ"], "posiciones": [{"t": "QQQ", "qty": 10, "px": 50.1}], "pnl_dia": -3,
            "comprometido": 501}, [{"id": "deadbeef", "ev": "salida", "t": "ZZZ", "px": 9.5, "qty": 10,
                                    "por": "trailing", "pnl": -3.0, "px_e": 9.8},
                                   {"id": "cafe0001", "ev": "rechazada", "t": "YYY", "motivo": "fondos insuficientes"},
                                   {"id": "cafe0002", "ev": "parada", "dia": "2026-10-05"}])
    m = [x[1] for x in p.tomar_msgs()]
    assert m[0].startswith("🛑") and "ZZZ" in m[0] and "-3.00 US$" in m[0] and "-3.1%" in m[0], m
    assert "fondos insuficientes" in m[1] and m[2].startswith("⛔"), m
    o = arma(p, "QQQ")
    assert o["estado"] == "omitida"                                      # la posición que informa el ejecutor cuenta
    txt = p.estado_txt()
    assert "Ejecutor conectado (cuenta DUR233329)" in txt and "QQQ 10 @50.10" in txt and "-3.00 US$" in txt, txt
    assert "/cerrar" in p.ayuda() and "US$1,000 por operación" in p.ayuda() and "US$4,000 comprometidos" in p.ayuda()
    st = p.status()
    assert st["ejecutor"] and st["modo"] == "boton" and "posiciones" not in st
    c.t += 120
    assert "sin conexión" in p.estado_txt()


class FakeHTTP:
    """Telegram falso: registra cada llamada y responde a getUpdates con la lista que se le carga."""
    def __init__(self, updates=None):
        self.calls, self.updates = [], list(updates or [])

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json))
        if method == "getUpdates":
            if json.get("offset") == -1:
                res = [{"update_id": 99}]                                   # lo acumulado antes de arrancar
            else:
                res, self.updates = self.updates, []
            body = {"ok": True, "result": res}
        else:
            body = {"ok": True, "result": {}}
        return NS(content=b"x", json=lambda: body, status_code=200)


def test_telegram_entrada():
    p, c = nuevo()
    p.sync(OK, [])
    o = arma(p)
    cb = lambda i, data, chat, user=None: {"update_id": i, "callback_query": {
        "id": f"cb{i}", "data": data, "from": {"id": user or chat}, "message": {"message_id": 7, "chat": {"id": chat}}}}
    msg = lambda i, text, age=0, user=555: {"update_id": i, "message": {"chat": {"id": 555}, "from": {"id": user},
                                                                        "date": T0 - age, "text": text}}
    h = FakeHTTP([cb(100, f"x:{o['id']}", 555), cb(101, f"x:{o['id']}", 666), msg(102, "/estado"),
                  msg(103, "/cerrar", age=500), msg(104, "/cerrar@semaforo_bot"), msg(105, "hola"),
                  msg(106, "/auto", user=777)])
    tg = TgIn(p, token="TKN", chat_id="555", http=h, clock=c)
    tg.poll_once(timeout=0)
    calls = [x for x in h.calls if x[0] != "getUpdates"]
    assert calls[0][0] == "answerCallbackQuery" and calls[0][1]["text"].startswith("Enviando"), calls[0]
    assert calls[1][0] == "editMessageReplyMarkup" and "Enviada" in str(calls[1][1]["reply_markup"]), calls[1]
    assert p.ofertas[o["id"]]["estado"] == "cola"
    assert calls[2] == ("answerCallbackQuery", {"callback_query_id": "cb101", "text": "No autorizado."}), calls[2]
    assert calls[3][0] == "sendMessage" and calls[3][1]["text"].startswith("📊 Paper"), calls[3]
    assert calls[4][0] == "sendMessage" and calls[4][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("c:si:")
    assert len(calls) == 5 and tg.offset == 107                         # el /cerrar viejo, el "hola" y otro usuario: nada
    assert p.modo == "boton"
    assert h.calls[0] == ("getUpdates", {"offset": -1, "timeout": 0})   # al arrancar salta lo acumulado
    data = calls[4][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    h.updates = [cb(107, "c:si", 555), cb(108, data, 555, user=777)]    # botón viejo / de otro usuario: no
    tg.poll_once(timeout=0)
    assert p.cerrar_id is None
    h.updates = [cb(109, data, 555)]
    tg.poll_once(timeout=0)
    assert p.cerrar_id and any(x[0] == "sendMessage" and "Cerrando" in x[1]["text"] for x in h.calls)
    grupo = TgIn(p, token="TKN", chat_id="-100123", http=FakeHTTP(), clock=c)   # grupo sin TELEGRAM_USER_ID: nadie manda
    assert not grupo.mine("-100123", 555) and TgIn(p, token="TKN", chat_id="-100123", user_id="555").mine("-100123", 555)
    o2 = arma(p, "BBB")
    p.comando("/pausa")
    h.updates = [cb(110, f"x:{o2['id']}", 555)]
    tg.poll_once(timeout=0)
    assert h.calls[-1][0] == "sendMessage" and "pausa" in h.calls[-1][1]["text"]   # si no se puede, dice por qué
    bad = NS(content=b"x", json=lambda: {"ok": False, "error_code": 409, "description": "Conflict"}, status_code=409)
    h.post = lambda url, json=None, timeout=None: bad
    assert tg.poll_once(timeout=0) == 0 and tg.state["code"] == 409 and "TKN" not in str(tg.state)


def test_avisos_con_boton():
    """ARMA, ⚡ y COMPRA llevan el botón cuando el ejecutor está conectado; sin ejecutor, salen como siempre."""
    from live import runner
    r = runner.Radar(notify=False)
    r.trades, r.arms, r.sent = {}, {}, set()
    plain, marked = [], []
    r._send = lambda text: (plain.append(text), True)[1]
    r._send_markup = lambda text, markup: (marked.append((text, markup)), True)[1]

    def row(t, lvl, px, score=70, risk=1.0):
        entry = lvl * 1.001
        return {"t": t, "decision": "ESPERA", "level": lvl, "px": px, "chg": 6.0, "score": score,
                "reason": "debajo del máximo de apertura",
                "plan": {"entry": entry, "stop": entry * (1 - risk / 100), "t1": entry * 1.02, "t2": entry * 1.05,
                         "risk": risk}}
    r._arm([row("AAA", 50.0, 49.8)], "verde", "open", 10 * 60)
    assert len(plain) == 1 and not marked and "🟡 ARMA AAA" in plain[0]  # sin ejecutor: sin botón
    r.paper.sync(OK, [])
    r._arm([row("AAA", 50.0, 49.8), row("BBB", 20.0, 19.9)], "verde", "open", 10 * 60)
    assert len(plain) == 1 and set(r.armed) == {"AAA", "BBB"}            # AAA ya avisó: no se repite
    (text, kb), = marked
    assert "🟡 ARMA BBB" in text and kb["inline_keyboard"][0][0]["callback_data"].startswith("x:"), (text, kb)
    oid = kb["inline_keyboard"][0][0]["callback_data"][2:]
    of = r.paper.ofertas[oid]
    assert of["tipo"] == "stp" and of["gatillo"] == 20.02 and of["qty"] == int(P.ORDEN_USD // 20.08) == 49, of
    # ⚡ de AAA (sin orden paper): trae botón de compra límite; ⚡ de BBB con su orden ya pedida: no ofrece otra
    r.paper.ofertas[oid]["estado"] = "puesta"
    now = datetime(2026, 9, 29, 10, 1, tzinfo=ET)
    r._check_breaks({"AAA": 50.06, "BBB": 20.03}, "yahoo", now)
    brk = [x for x in marked if "⚡ AAA" in x[0]]
    assert brk and r.paper.ofertas[brk[0][1]["inline_keyboard"][0][0]["callback_data"][2:]]["tipo"] == "lmt"
    assert any("⚡ BBB" in x for x in plain), plain
    # la armada de BBB se cancela (pierde el stop) → la orden paper puesta se manda a cancelar
    r.paper.cancelar_ticker("BBB", "perdió el stop sin romper")
    assert r.paper.sync(OK, [])["cancelar"] == [oid]
    # el ejecutor manda eventos → llegan por Telegram una sola vez
    sent_keys = []
    r.tg = lambda key, text, wait=True, private=False: sent_keys.append(key)
    res = r.on_paper(OK, [{"id": oid, "ev": "cancelada", "motivo": "el semáforo la canceló"}])
    assert sent_keys == [f"paper:{oid}:cancelada"] and res["cancelar"] == [], (sent_keys, res)


def test_endpoint_sync():
    from fastapi.testclient import TestClient
    from live import app as A
    _os.environ["BRIDGE_TOKEN"] = "clave-prueba"
    try:
        cl = TestClient(A.app)
        assert cl.post("/api/paper/sync", json={}).status_code == 401
        assert cl.post("/api/paper/sync", json={}, headers={"X-Token": "mala"}).status_code == 401
        r = cl.post("/api/paper/sync", json={"estado": OK, "eventos": []}, headers={"X-Token": "clave-prueba"})
        assert r.status_code == 200 and r.json()["ordenes"] == [] and r.json()["limites"]["perdida_max"] == 250, r.text
        big = {"estado": OK, "eventos": [{"id": "x" * 100, "ev": "error", "motivo": "y" * 1000}] * 300}
        assert cl.post("/api/paper/sync", json=big, headers={"X-Token": "clave-prueba"}).status_code == 413
        h = cl.get("/health").json()
        assert h["paper"]["ejecutor"] is True and "entrada" in h["telegram"], h["paper"]
    finally:
        del _os.environ["BRIDGE_TOKEN"]


if __name__ == "__main__":
    test_sin_ejecutor_no_hay_boton()
    test_flujo_boton()
    test_limites()
    test_modo_automatico()
    test_cancelar_ticker()
    test_cerrar()
    test_reinicio_adopta_pausa()
    test_cancelada_sin_llegar()
    test_eventos_y_estado()
    test_telegram_entrada()
    test_avisos_con_boton()
    test_endpoint_sync()
    print("OK · paper (semáforo)")
