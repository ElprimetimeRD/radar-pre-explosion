"""Pruebas del lector de Yahoo del ejecutor de Claude (bridge/datos_yahoo.py) con respuestas falsas: lectura de velas de 1 min
y de velas diarias (cierre previo, volumen medio, ATR), barrido de toda la lista y, sobre todo, qué hace cuando Yahoo limita
las peticiones (429): deja de insistir, lee más despacio y vuelve al ritmo normal cuando se normaliza.
Uso: PYTHONPATH=. python tests/test_datos_yahoo.py"""
import os
import sys
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bridge"))
import datos_yahoo as Y  # noqa: E402

NY = ZoneInfo("America/New_York")
HOY = "2026-10-05"


def et(h, m, s=0, dia=5):
    return datetime(2026, 10, dia, h, m, s, tzinfo=NY).timestamp()


def chart(ts, o, h, l, c, v, gmtoffset=-14400):
    return {"chart": {"result": [{"meta": {"gmtoffset": gmtoffset}, "timestamp": ts,
                                  "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c, "volume": v}]}}]}}


def velas_1m(hasta_minuto=45):
    """Velas de 1 min de la sesión de hoy (9:30 ... 9:{hasta_minuto-1}), más una de pre-mercado y una con huecos."""
    ts = [et(9, 29)] + [et(9, m) for m in range(30, hasta_minuto)]
    n = len(ts)
    o = [10.0] + [10.0 + 0.01 * i for i in range(n - 1)]
    c = [10.0] + [10.01 + 0.01 * i for i in range(n - 1)]
    h = [x + 0.02 for x in c]
    l = [x - 0.02 for x in o]
    v = [1000.0] * n
    c[3] = None                                  # una vela sin precio: se salta
    return chart(ts, o, h, l, c, v)


def diarias(dias=25, con_hoy=True):
    """`dias` velas diarias de cierre 100 (alto 101, bajo 99, volumen 1e6) y, si se pide, la de hoy a 150 (no debe contar)."""
    ts = [et(9, 30, dia=d) for d in range(1, 5)] + [et(9, 30, dia=1) - 86400 * k for k in range(1, dias)]
    ts = sorted(ts)[-(dias - (1 if con_hoy else 0)):]
    n = len(ts)
    o, h, l, c, v = [100.0] * n, [101.0] * n, [99.0] * n, [100.0] * n, [1e6] * n
    if con_hoy:
        ts.append(et(9, 30))
        o.append(150.0), h.append(151.0), l.append(149.0), c.append(150.0), v.append(5e6)
    return chart(ts, o, h, l, c, v)


class Reloj:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def fetch_normal(url, timeout=8.0):
    return diarias() if "range=3mo" in url else velas_1m()


def test_velas_de_1_minuto():
    ahora = et(9, 45, 30)
    dia, barras = Y.parse_1m(velas_1m(), ahora)
    assert dia == HOY
    ms = [b[0] for b in barras]
    assert ms[0] == 9 * 60 + 30 and ms[-1] == 9 * 60 + 44, ms                  # sin la de pre-mercado
    assert 9 * 60 + 32 not in ms                                              # la vela sin precio (9:32) se salta
    assert all(len(b) == 6 for b in barras) and barras[0][4] == 10.01
    # la vela del minuto en curso (se está formando) no cuenta
    _, b2 = Y.parse_1m(velas_1m(hasta_minuto=46), et(9, 45, 30))
    assert b2[-1][0] == 9 * 60 + 44
    # respuesta rara: no revienta, avisa
    for malo in ({}, {"chart": {"result": None}}, {"chart": {"result": []}}):
        try:
            Y.parse_1m(malo, ahora)
        except Y.SinDatos:
            pass
        else:
            raise AssertionError("debía fallar con SinDatos")


def test_velas_diarias():
    d = Y.parse_diario(diarias(), HOY)
    assert d["prev"] == 100.0 and d["adv"] == 1e6, d                          # la vela de hoy (150) no cuenta
    assert abs(d["atr"] - 0.02) < 1e-9, d                                     # rango diario 2 con cierre 100
    try:
        Y.parse_diario(diarias(dias=10, con_hoy=False), HOY)
    except Y.SinDatos:
        pass
    else:
        raise AssertionError("con menos de 15 días no hay ATR fiable")


def nuevo_feed(fetch=fetch_normal, ahora=None, n=10):
    reloj = Reloj(ahora or et(9, 45, 30))
    f = Feed(["T%d" % i for i in range(n)], fetch, reloj)
    return f, reloj


def Feed(simbolos, fetch, reloj):
    return Y.Feed(simbolos, fetch=fetch, reloj=reloj, workers=3)


def test_barrido_normal():
    f, reloj = nuevo_feed()
    assert f.barrido(HOY) == 11 and f.n_ok == 11 and f.n_total == 11 and f.error is None
    assert f.ok() and f.intervalo == 60 and f.limpios == 1
    datos, spy = f.snapshot(HOY)
    assert len(datos) == 10 and spy["bars"] and spy["prev"] == 100.0
    d = datos["T3"]
    assert d["prev"] == 100.0 and d["adv"] == 1e6 and abs(d["atr"] - 0.02) < 1e-9 and d["dia"] == HOY
    # los datos diarios de otro día no valen (daría un cambio del día falso)
    datos2, _ = f.snapshot("2026-10-06")
    assert datos2 == {}
    # envejece: a los 151 s sin lecturas ya no es fiable
    reloj.t += 151
    assert not f.ok()
    est = f.estado()
    assert est["feed_intervalo_s"] == 60 and est["feed_de"] == 11


def test_yahoo_limita_y_se_normaliza():
    """429: no se sigue insistiendo en el mismo barrido, se lee más despacio (60 → 120 → 240 s) y, tras 8 barridos limpios
    seguidos, se vuelve a acercar al ritmo normal (240 → 120 → 60 s)."""
    estado = {"limitar": True, "llamadas": 0}
    lock = threading.Lock()

    def fetch(url, timeout=8.0):
        with lock:
            estado["llamadas"] += 1
        if estado["limitar"]:
            raise Y.SinDatos("HTTP 429")
        return fetch_normal(url)
    f, reloj = nuevo_feed(fetch, n=46)
    assert f.barrido(HOY) == 0
    # 47 acciones y todas dirían 429: solo salen unas pocas peticiones (las que ya iban en vuelo), no las 94 posibles
    assert estado["llamadas"] <= 12, estado
    assert f.intervalo == 120 and f.limpios == 0 and f.pausa_hasta >= reloj.t + 120
    assert not f.ok()
    # durante la pausa no se pide nada
    antes = estado["llamadas"]
    reloj.t += 60
    assert f.barrido(HOY) == 0 and estado["llamadas"] == antes
    # pasada la pausa, sigue limitando: 240 s (el máximo), y ahí se queda
    reloj.t += 61
    f.barrido(HOY)
    assert f.intervalo == 240
    reloj.t += 241
    f.barrido(HOY)
    assert f.intervalo == 240
    # el estado dice cada cuánto lee y lo exigido a la frescura crece con ello: a 240 s de la última lectura aún vale
    estado["limitar"] = False
    reloj.t += 241
    assert f.barrido(HOY) == 47 and f.ok() and f.n_ok == 47
    reloj.t += 240
    assert f.ok(), "con el intervalo largo, la lectura de hace 240 s todavía es reciente"
    assert f.estado()["feed_intervalo_s"] == 240
    # 8 barridos limpios seguidos: 240 → 120; otros 8: 120 → 60 (el barrido de arriba ya fue el primero limpio)
    for i in range(6):
        reloj.t += 240
        f.barrido(HOY)
    assert f.intervalo == 240 and f.limpios == 7, (f.intervalo, f.limpios)
    reloj.t += 240
    f.barrido(HOY)
    assert f.intervalo == 120 and f.limpios == 0, (f.intervalo, f.limpios)
    for i in range(7):
        reloj.t += 120
        f.barrido(HOY)
    assert f.intervalo == 120 and f.limpios == 7, (f.intervalo, f.limpios)
    reloj.t += 120
    f.barrido(HOY)
    assert f.intervalo == 60 and f.limpios == 0, (f.intervalo, f.limpios)
    # ritmo normal: a los 6 s de cada minuto
    reloj.t = et(10, 0, 20)
    assert abs(f.espera_barrido() - 46) < 1e-6
    f.intervalo = 120
    assert abs(f.espera_barrido() - 106) < 1e-6
    f.intervalo = 60
    reloj.t = et(10, 0, 59)
    assert abs(f.espera_barrido() - 7) < 1e-6


def test_un_429_suelto_no_tira_todo():
    """Un solo 429 en medio de un barrido sano: el resto de acciones sí se leen y el lector pasa a ir más despacio."""
    estado = {"n": 0}
    lock = threading.Lock()

    def fetch(url, timeout=8.0):
        with lock:
            estado["n"] += 1
            k = estado["n"]
        if k == 1:
            raise Y.SinDatos("HTTP 429")
        return fetch_normal(url)
    f, _ = nuevo_feed(fetch, n=10)
    ok = f.barrido(HOY)
    assert f.intervalo == 120 and f.limitado
    assert ok < 11                                                             # algunas no pudieron leerse (pausa)
    assert f.ult_barrido > 0 and f.error and "429" in f.error


def test_otros_errores_no_cambian_el_ritmo():
    """Un error de red o un 500 de una acción no es un límite: no se frena el lector."""
    def fetch(url, timeout=8.0):
        if "T2" in url:
            raise Y.SinDatos("HTTP 500")
        return fetch_normal(url)
    f, _ = nuevo_feed(fetch, n=10)
    assert f.barrido(HOY) == 10 and f.intervalo == 60 and f.error and "T2" in f.error
    assert f.ok()                                                              # 10 de 11 bastan (≥ 60 %)


if __name__ == "__main__":
    test_velas_de_1_minuto()
    test_velas_diarias()
    test_barrido_normal()
    test_yahoo_limita_y_se_normaliza()
    test_un_429_suelto_no_tira_todo()
    test_otros_errores_no_cambian_el_ritmo()
    print("OK · datos de Yahoo")
