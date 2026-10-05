"""Pruebas sin red del ejecutor de Claude visto desde el semáforo: live/claude_paper.py (estado, eventos → Telegram,
lo que comparte con el ejecutor de Priamo, marcador y vigilancia de caídas), su ruta /api/claude/sync, los comandos
/claude y /marcador y la vigilancia que corre después de cada ciclo.
Uso: NO_LOOP=1 NO_NOTIFY=1 PYTHONPATH=. python tests/test_claude_paper.py"""
import os as _os
_os.environ.setdefault("RIESGO", "normal")
_os.environ.setdefault("NO_LOOP", "1")
_os.environ.setdefault("NO_NOTIFY", "1")

from datetime import datetime
from types import SimpleNamespace as NS
from unittest import mock

from scanner.util import ET
from live import claude_paper as C
from live import paper as P
from live.tgbot import TgIn

T0 = datetime(2026, 10, 5, 10, 0, tzinfo=ET).timestamp()  # lunes 10:00 ET
DIA = "2026-10-05"
OK = {"ib": True, "paper": True, "cuenta": "DUR233329", "comprometido": 0, "riesgo_abierto": 0, "pnl_dia": 0,
      "perdida_dia": 0, "pausa": False}
OKC = {**OK, "feed_ok": True, "feed_n": 40, "feed_de": 45, "feed_edad_s": 8, "estrategia": "pullback con tendencia",
       "ops": 0, "gan": 0}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def nuevo():
    c = Clock(T0)
    p = P.Paper(modo="boton", clock=c)
    return p, C.ClaudePaper(p, clock=c), c


def test_sync_y_mensajes():
    p, cp, c = nuevo()
    res = cp.sync(OKC, [], dia=DIA)
    assert res["ordenes"] == [] and res["cancelar"] == [] and res["modo"] == "auto" and res["pausa"] is False
    assert res["limites"] == {"orden_usd": 500.0, "max_abierto": 1000.0, "perdida_max": 100.0}, res
    assert cp.conectado() and not cp.tomar_msgs()
    cp.sync(OKC, [
        {"id": "ab12cd34", "ev": "puesta", "t": "NVDA", "detalle": "· 4 acc · compra límite 120.50 · stop 119.78 · objetivo 121.94"},
        {"id": "ab12cd34", "ev": "llena", "t": "NVDA", "px": 120.4, "qty": 4},
        {"id": "ab12cd34", "ev": "salida", "t": "NVDA", "px": 121.94, "px_e": 120.4, "qty": 4, "pnl": 6.16, "por": "objetivo"},
        {"id": "ee000001", "ev": "salida", "t": "AMD", "px": 99.0, "px_e": 100.0, "qty": 5, "pnl": -5.0, "por": "stop"},
        {"id": "ee000002", "ev": "rechazada", "t": "MU", "motivo": "fondos insuficientes"},
        {"id": "ee000003", "ev": "cancelada", "t": "TSM", "motivo": "la compra venció sin llenarse"},
        {"id": "ee000004", "ev": "cancelada", "t": "ARM", "motivo": "perdió su stop"},
        {"ev": "parada", "dia": DIA}, {"ev": "cierre", "dia": DIA, "motivo": "15:55 ET"},
        {"ev": "error", "motivo": "IB Gateway se desconectó"}, "basura", 7, None], dia=DIA)
    m = cp.tomar_msgs()
    claves = [k for k, _ in m]
    assert claves == ["claude:ab12cd34:puesta", "claude:ab12cd34:llena", "claude:ab12cd34:salida", "claude:ee000001:salida",
                      "claude:ee000002:rechazada", "claude:ee000004:cancelada", f"claude:parada:{DIA}",
                      f"claude:cierre:{DIA}:15:55 ET", "claude:error:IB Gateway se desconectó"], claves
    t = [x for _, x in m]
    assert all("Claude" in x for x in t) and "NVDA" in t[0] and "compra límite 120.50" in t[0]
    assert "compré 4 NVDA a 120.40" in t[1] and "stop fijo" in t[1]
    assert t[2].startswith("✅") and "el objetivo de 2R" in t[2] and "+6.16 US$" in t[2] and "+1.3%" in t[2], t[2]
    assert t[3].startswith("🛑") and "el stop" in t[3] and "-5.00 US$" in t[3], t[3]
    assert "fondos insuficientes" in t[4] and "perdió su stop" in t[5] and "pérdida máxima" in t[6] and "US$100" in t[6]
    assert cp.hoy["puestas"] == 1 and cp.hoy["llenas"] == 1 and cp.hoy["canceladas"] == 2 and len(cp.hoy["salidas"]) == 2
    assert cp.tomar_msgs() == []                                         # se entregan una sola vez
    cp.sync(OKC, [], dia="2026-10-06")                                   # día nuevo: contadores en cero
    assert cp.hoy["puestas"] == 0 and cp.hoy["salidas"] == []
    for i in range(300):                                                  # la lista de mensajes pendientes está acotada
        cp.sync(OKC, [{"id": f"{i:08x}", "ev": "rechazada", "t": "X", "motivo": "m"}])
    assert len(cp.tomar_msgs()) == 100


def test_limpiar_estado():
    p, cp, c = nuevo()
    cp.sync({"ib": 1, "paper": "si", "pnl_dia": "no es número", "comprometido": float("nan"), "cuenta": "X" * 80,
             "bloqueado": "B" * 500, "ops": "3", "gan": 2.0, "feed_n": None, "posiciones": [{"t": "NVDAXXXXXXX", "qty": "4", "px": 1},
                                                                                           "mal", {"qty": 1}] + [{"t": "A"}] * 40,
             "vivas_t": ["TSMXXXXXXXX"] * 50, "candidatas": ["a" * 500] * 9, "version": "1.0.0.0.0.0.0.0"}, [])
    ex = cp.ex
    assert ex["ib"] is True and ex["paper"] is True and ex["pnl_dia"] is None and ex["comprometido"] is None
    assert len(ex["cuenta"]) == 20 and len(ex["bloqueado"]) == 200 and ex["ops"] == 3 and ex["gan"] == 2 and ex["feed_n"] is None
    assert ex["posiciones"][0] == {"t": "NVDAXX", "qty": 4.0, "px": 1.0} and len(ex["posiciones"]) == 18   # lee las 20 primeras y descarta las inválidas
    assert len(ex["vivas_t"]) == 20 and all(len(x) <= 6 for x in ex["vivas_t"])
    assert len(ex["candidatas"]) == 3 and all(len(x) <= 160 for x in ex["candidatas"]) and len(ex["version"]) <= 10
    cp.sync(None, None)                                                   # cuerpo vacío no revienta
    assert cp.ex["posiciones"] == [] and cp.conectado() is False           # sin ib/paper no cuenta como conectado
    st = cp.status()
    assert "posiciones" not in st and "pnl_dia" not in st and st["ejecutor"] is False


def test_comparte_pausa_y_cierre():
    p, cp, c = nuevo()
    assert cp.sync(OKC, [])["pausa"] is False
    p.comando("/pausa")
    assert cp.sync(OKC, [])["pausa"] is True                              # la pausa de /pausa vale para los dos
    p.comando("/reanuda")
    assert cp.sync(OKC, [])["pausa"] is False
    p.sync(OK, [])                                                        # el ejecutor de Priamo está conectado
    txt, kb = p.comando("/cerrar")
    assert "Claude" in txt
    codigo = kb["inline_keyboard"][0][0]["callback_data"][5:]
    p.confirmar_cierre(codigo)
    c.t += 7
    r = cp.sync(OKC, [])
    assert r["cerrar_id"] and r["cerrar_hace_s"] == 7.0 and r["cerrar_id"] == p.sync(OK, [])["cerrar_id"], r
    # tras un reinicio del semáforo habla primero el ejecutor de Claude: toma la pausa, pero el modo lo sigue
    # adoptando el ejecutor de Priamo (si no, un redeploy lo devolvería al modo botón)
    p2, cp2, c2 = nuevo()
    assert cp2.sync({**OKC, "pausa": True}, [])["pausa"] is True and p2.pausa is True
    p2.sync({**OK, "pausa": True, "modo": "auto"}, [])
    assert p2.modo == "auto" and p2.pausa is True
    p3, cp3, c3 = nuevo()
    p3.sync({**OK, "pausa": False, "modo": "auto"}, [])                    # ya adoptó: Claude no cambia la pausa
    assert cp3.sync({**OKC, "pausa": True}, [])["pausa"] is False


def test_textos_y_marcador():
    p, cp, c = nuevo()
    assert "sin conexión" in cp.estado_txt() and "ARRANCAR_PAPER.bat" in cp.estado_txt()
    assert "sin datos" in cp.marcador()
    p.sync({**OK, "pnl_dia": 12.5, "ops": 3, "gan": 2, "posiciones": [{"t": "MU", "qty": 3, "px": 100.0}]}, [])
    cp.sync({**OKC, "pnl_dia": -4.25, "ops": 2, "gan": 0, "comprometido": 480, "posiciones": [{"t": "AMD", "qty": 5, "px": 99.5}],
             "vivas_t": ["AMD", "TSM"], "candidatas": ["TSM compra 50.20 stop 49.85"]},
            [{"id": "a1", "ev": "puesta", "t": "AMD"}, {"id": "a1", "ev": "llena", "t": "AMD", "px": 99.5, "qty": 5}], dia=DIA)
    e = cp.estado_txt()
    assert "pullback con tendencia" in e and "Ejecutor conectado (cuenta DUR233329)" in e and "Yahoo 40/45" in e, e
    assert "AMD 5 @99.50" in e and "Compras puestas esperando: TSM" in e and "-4.25 US$" in e and "2 ops (0 ganadas)" in e, e
    assert "1 órdenes puestas · 1 llenadas" in e and "Jugadas que ve ahora: TSM" in e, e
    m = cp.marcador()
    assert "Tú (semáforo): +12.50 US$ · 3 ops cerradas (2 ganadas) · 1 posiciones abiertas" in m, m
    assert "Claude (pullback): -4.25 US$ · 2 ops cerradas (0 ganadas)" in m and "Va ganando: Priamo" in m, m
    assert "no demuestra nada" in m
    c.t += 700                                                            # los dos dejan de hablar
    m = cp.marcador()
    assert m.count("sin conexión") == 2 and "hace 11 min" in m, m
    assert "sin conexión" in cp.estado_txt()
    sin_feed = {**OKC, "feed_ok": False, "feed_error": "AMD: HTTP 429"}
    c.t = T0
    cp.sync(sin_feed, [])
    assert "SIN datos de Yahoo (AMD: HTTP 429)" in cp.estado_txt()
    cp.sync({**OKC, "bloqueado": "no es cuenta paper", "ib": False}, [])
    assert "Ejecutor bloqueado: no es cuenta paper" in cp.estado_txt()
    p.comando("/pausa")
    assert "EN PAUSA" in cp.estado_txt()
    assert "/claude" in p.ayuda() and "/marcador" in p.ayuda()
    assert cp.actividad() is True


def test_vigilar():
    p, cp, c = nuevo()
    abre = 9 * 60 + 30
    lab = True
    # fuera de horario o fin de semana: nada
    assert cp.vigilar(T0, DIA, 8 * 60, lab, 9999) == [] and cp.vigilar(T0, DIA, abre, False, 9999) == []
    assert cp.vigilar(T0, DIA, 16 * 60, lab, 9999) == []
    # recién reiniciado el semáforo: todavía no es una caída
    assert cp.vigilar(T0, DIA, abre, lab, 100) == []
    # nadie se conectó y el mercado ya abre: un aviso por ejecutor, una sola vez
    a = cp.vigilar(T0, DIA, abre, lab, 600)
    assert len(a) == 2 and {k for k, _ in a} == {f"ej:paper:nunca:{DIA}", f"ej:claude:nunca:{DIA}"}, a
    assert "ARRANCAR_PAPER.bat" in a[0][1] and cp.vigilar(T0 + 60, DIA, abre + 1, lab, 660) == []
    # los dos se conectan: avisa que volvieron
    p.sync(OK, [], now=T0 + 120)
    cp.sync(OKC, [], now=T0 + 120)
    v = cp.vigilar(T0 + 125, DIA, abre + 2, lab, 720)
    assert len(v) == 2 and all("conectado otra vez" in t for _, t in v), v
    assert cp.vigilar(T0 + 126, DIA, abre + 2, lab, 721) == []
    # el de Claude deja de hablar 100 s: un aviso de caída (no antes de 90 s), una sola vez
    p.sync(OK, [], now=T0 + 220)
    assert cp.vigilar(T0 + 200, DIA, abre + 3, lab, 800) == []
    v = cp.vigilar(T0 + 221, DIA, abre + 4, lab, 821)
    assert len(v) == 1 and v[0][0].startswith(f"ej:claude:caido:{DIA}") and "Claude" in v[0][1] and "protegido" in v[0][1], v
    assert cp.vigilar(T0 + 260, DIA, abre + 5, lab, 860) == []
    cp.sync(OKC, [], now=T0 + 300)
    p.sync(OK, [], now=T0 + 300)
    v = cp.vigilar(T0 + 301, DIA, abre + 6, lab, 900)
    assert len(v) == 1 and "conectado otra vez" in v[0][1]
    # el ejecutor de Claude sigue vivo pero sin IB Gateway: avisa pasados 2 min, una vez
    cp.sync({**OKC, "ib": False, "bloqueado": "IB Gateway no responde"}, [], now=T0 + 400)
    p.sync(OK, [], now=T0 + 400)
    assert cp.vigilar(T0 + 401, DIA, abre + 8, lab, 1000) == []
    cp.sync({**OKC, "ib": False, "bloqueado": "IB Gateway no responde"}, [], now=T0 + 530)
    p.sync(OK, [], now=T0 + 530)
    v = cp.vigilar(T0 + 531, DIA, abre + 17, lab, 1100)
    assert len(v) == 1 and "sin conexión con IB Gateway" in v[0][1] and "IB Gateway no responde" in v[0][1], v
    cp.sync({**OKC, "ib": False}, [], now=T0 + 600)
    p.sync(OK, [], now=T0 + 600)
    assert cp.vigilar(T0 + 601, DIA, abre + 18, lab, 1200) == []
    cp.sync(OKC, [], now=T0 + 700)
    p.sync(OK, [], now=T0 + 700)
    assert "conectado otra vez" in cp.vigilar(T0 + 701, DIA, abre + 19, lab, 1300)[0][1]
    # sin datos de Yahoo en plena sesión: un aviso por hora
    cp.sync({**OKC, "feed_ok": False, "feed_error": "AMD: HTTP 429"}, [], now=T0 + 800)
    p.sync(OK, [], now=T0 + 800)
    assert cp.vigilar(T0 + 801, DIA, 9 * 60 + 40, lab, 1400) == []        # antes de las 9:55 el flujo aún se estabiliza
    v = cp.vigilar(T0 + 802, DIA, 10 * 60, lab, 1400)
    assert len(v) == 1 and "datos de Yahoo" in v[0][1] and v[0][0].startswith(f"claude:feed:{DIA}"), v
    cp.sync({**OKC, "feed_ok": False}, [], now=T0 + 900)
    p.sync(OK, [], now=T0 + 900)
    assert cp.vigilar(T0 + 901, DIA, 10 * 60 + 5, lab, 1500) == []        # ya avisó en esta hora
    # el reloj de otro día cambia los contadores aunque el ejecutor no hable
    cp.hoy["puestas"] = 3
    cp.vigilar(T0 + 90000, "2026-10-06", 8 * 60, lab, 9999)
    assert cp.hoy["puestas"] == 0 and cp.hoy["dia"] == "2026-10-06"


def test_comandos_telegram():
    p, cp, c = nuevo()
    enviados = []

    class H:
        def post(self, url, json=None, timeout=None):
            if url.endswith("/sendMessage"):
                enviados.append(json["text"])
            return NS(content=b"{}", json=lambda: {"ok": True, "result": []}, status_code=200)
    tg = TgIn(p, token="TKN", chat_id="555", http=H(), clock=c, claude=cp)
    cp.sync(OKC, [])
    for cmd in ("/claude", "/marcador", "/claude@semaforo_bot", "/ayuda"):
        tg.on_message({"chat": {"id": 555}, "from": {"id": 555}, "date": c.t, "text": cmd})
    assert "🤖 Claude (paper)" in enviados[0] and "Ejecutor conectado" in enviados[0]
    assert "Marcador de hoy" in enviados[1] and "🤖 Claude (paper)" in enviados[2]
    assert "/claude" in enviados[3] and "/marcador" in enviados[3] and "US$500" in enviados[3]
    sin = TgIn(p, token="TKN", chat_id="555", http=H(), clock=c)           # sin ejecutor de Claude: cae en la ayuda
    sin.on_message({"chat": {"id": 555}, "from": {"id": 555}, "date": c.t, "text": "/claude"})
    assert "/pausa" in enviados[-1]
    tg.on_message({"chat": {"id": 555}, "from": {"id": 999}, "date": c.t, "text": "/claude"})   # otro usuario: nada
    assert len(enviados) == 5


def test_vigilancia_del_semaforo():
    """Radar.on_claude y Radar.vigilar_ejecutores: los mensajes salen por Telegram una sola vez y la vigilancia corre sola."""
    from live import runner
    r = runner.Radar(notify=False)
    claves = []
    r.tg = lambda key, text, wait=True, private=False: claves.append((key, private))
    r.t_arranque = T0 - 3600
    with mock.patch("live.runner.time.time", return_value=T0):
        res = r.on_claude(OKC, [{"id": "ab12cd34", "ev": "llena", "t": "NVDA", "px": 120.4, "qty": 4}])
        assert res["modo"] == "auto" and claves == [("claude:ab12cd34:llena", True)], claves
        r.paper.sync(OK, [], now=T0)
        r.claude.sync(OKC, [], now=T0)
        claves.clear()
        r.vigilar_ejecutores()
        assert claves == []                                                # los dos conectados: en silencio
    with mock.patch("live.runner.time.time", return_value=T0 + 200):
        r.vigilar_ejecutores()
        assert {k.split(":")[1] for k, _ in claves} == {"paper", "claude"} and all(k.startswith("ej:") for k, _ in claves), claves
    # fin de semana y feriados: no se vigila
    claves.clear()
    sabado = datetime(2026, 10, 10, 10, 0, tzinfo=ET).timestamp()
    with mock.patch("live.runner.time.time", return_value=sabado):
        r.vigilar_ejecutores()
    accion = datetime(2026, 11, 26, 10, 0, tzinfo=ET).timestamp()
    with mock.patch("live.runner.time.time", return_value=accion):
        r.vigilar_ejecutores()
    assert claves == []
    # a las 16:10 ET llega el marcador, solo si hubo actividad
    cierre = datetime(2026, 10, 5, 16, 11, tzinfo=ET).timestamp()
    r.paper.sync({**OK, "pnl_dia": 8.0, "ops": 2, "gan": 1}, [], now=cierre)
    r.claude.sync({**OKC, "pnl_dia": -3.0, "ops": 1, "gan": 0}, [], now=cierre)
    with mock.patch("live.runner.time.time", return_value=cierre):
        r.vigilar_ejecutores()
        r.vigilar_ejecutores()
    assert [k for k, _ in claves] == [f"marcador:{DIA}", f"marcador:{DIA}"]   # (r.tg es falso: la clave única la pone el real)
    r2 = runner.Radar(notify=False)
    textos = []
    r2.notify = True
    r2._send = lambda text: (textos.append(text), True)[1]
    r2._emit = lambda key, text, wait=True, private=False, markup=None: textos.append(text)
    r2.t_arranque = T0 - 3600
    r2.paper.sync({**OK, "pnl_dia": 8.0, "ops": 2, "gan": 1}, [], now=cierre)
    r2.claude.sync({**OKC, "pnl_dia": -3.0, "ops": 1, "gan": 0}, [], now=cierre)
    with mock.patch("live.runner.time.time", return_value=cierre):
        r2.vigilar_ejecutores()
        r2.vigilar_ejecutores()
    assert len(textos) == 1 and "Marcador de hoy" in textos[0] and "+8.00" in textos[0] and "-3.00" in textos[0], textos


def test_endpoint_claude():
    from fastapi.testclient import TestClient
    from live import app as A
    _os.environ["BRIDGE_TOKEN"] = "clave-prueba"
    vistos = []
    tg_real = A.radar.tg
    A.radar.tg = lambda key, text, wait=True, private=False: vistos.append(key)
    try:
        cl = TestClient(A.app)
        assert cl.post("/api/claude/sync", json={}).status_code == 401
        assert cl.post("/api/claude/sync", json={}, headers={"X-Token": "mala"}).status_code == 401
        body = {"estado": OKC, "eventos": [{"id": "ab12cd34", "ev": "llena", "t": "NVDA", "px": 120.4, "qty": 4}]}
        r = cl.post("/api/claude/sync", json=body, headers={"X-Token": "clave-prueba"})
        assert r.status_code == 200 and r.json()["ordenes"] == [] and r.json()["limites"]["perdida_max"] == 100, r.text
        assert vistos == ["claude:ab12cd34:llena"], vistos
        big = {"estado": OKC, "eventos": [{"id": "x" * 100, "ev": "error", "motivo": "y" * 1000}] * 300}
        assert cl.post("/api/claude/sync", json=big, headers={"X-Token": "clave-prueba"}).status_code == 413
        h = cl.get("/health").json()
        assert h["claude"]["ejecutor"] is True and "paper" in h and "posiciones" not in h["claude"], h["claude"]
    finally:
        A.radar.tg = tg_real
        del _os.environ["BRIDGE_TOKEN"]


if __name__ == "__main__":
    test_sync_y_mensajes()
    test_limpiar_estado()
    test_comparte_pausa_y_cierre()
    test_textos_y_marcador()
    test_vigilar()
    test_comandos_telegram()
    test_vigilancia_del_semaforo()
    test_endpoint_claude()
    print("OK · ejecutor de Claude (semáforo)")
