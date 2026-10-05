"""Pruebas de bridge/comparar.py (marcador acumulado semáforo vs Claude) con diarios CSV hechos como los del ejecutor.
Uso: PYTHONPATH=. python tests/test_comparar.py"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bridge"))
import comparar as C  # noqa: E402
import ejecutor_paper as E  # noqa: E402


def diario(path, filas):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(E.DIARIO_COLS)
        for r in filas:
            w.writerow(r)


def fila(fecha, hora, quien, ev, t, qty=10, pe=100.0, ps=101.0, pnl=10.0, com=1.0, riesgo=5.0, por="objetivo", setup="", motivo=""):
    r = round(pnl / riesgo, 2) if (pnl != "" and riesgo) else ""
    return [fecha, hora, quien, ev, t, qty, pe, ps, pnl, com, riesgo, r, por, setup, motivo]


def test_columnas_y_lectura():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "x.csv")
    assert C.leer(p) == []                                               # no existe: vacío, sin error
    diario(p, [fila("2026-10-05", "10:05:00", "semáforo", "salida", "MU")])
    f = C.leer(p)
    assert len(f) == 1 and f[0]["simbolo"] == "MU" and C.operaciones(f)[0]["neto"] == 9.0
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("basura sin comas\n")
    assert len(C.leer(p)) == 1                                           # una línea rara no revienta la lectura
    assert C.operaciones([{"fecha": "2026-10-05", "evento": "salida", "pnl_usd": "no"}]) == []


def test_resumen_y_tabla():
    sem = [fila("2026-10-05", "10:05:00", "semáforo", "salida", "MU", pnl=12.0, com=1.0, riesgo=6.0, por="trailing"),
           fila("2026-10-05", "11:00:00", "semáforo", "salida", "AMD", pnl=-6.0, com=1.0, riesgo=6.0, por="trailing"),
           fila("2026-10-05", "11:10:00", "semáforo", "cancelada", "NVDA", pnl="", com="", riesgo="", por="", motivo="venció"),
           fila("2026-10-06", "10:30:00", "semáforo", "salida", "MU", pnl=-9.0, com=1.0, riesgo=6.0, por="cierre")]
    cla = [fila("2026-10-05", "10:40:00", "Claude", "salida", "TSM", pnl=15.0, com=1.0, riesgo=7.5, por="objetivo"),
           fila("2026-10-05", "13:00:00", "Claude", "salida", "ARM", pnl=-7.5, com=1.0, riesgo=7.5, por="stop"),
           fila("2026-10-06", "10:20:00", "Claude", "salida", "AMD", pnl=14.0, com=1.0, riesgo=7.0, por="objetivo"),
           fila("2026-10-06", "10:30:00", "Claude", "rechazada", "X", pnl="", com="", riesgo="", por="", motivo="m")]
    d = tempfile.mkdtemp()
    ps, pc = os.path.join(d, "s.csv"), os.path.join(d, "c.csv")
    diario(ps, sem)
    diario(pc, cla)
    a, b = C.resumen(C.leer(ps)), C.resumen(C.leer(pc))
    # Tú: netos +11, -7, -10 (bruto 12, -6, -9 menos 1 de comisión cada una)
    assert a["n"] == 3 and a["ganadas"] == 1 and round(a["bruto"], 2) == -3.0 and a["com"] == 3.0
    assert round(a["neto"], 2) == -6.0 and round(a["media"], 2) == -2.0 and round(a["pf"], 3) == round(11 / 17, 3)
    assert a["por_dia"] == {"2026-10-05": 4.0, "2026-10-06": -10.0}
    assert round(a["caida"], 2) == -17.0                                  # pico +11, valle -6
    assert a["sin_llenar"] == 1 and round(a["llenado"], 2) == 0.75 and a["rechazadas"] == 0
    assert a["por_motivo"] == {"trailing": (2, 4.0), "cierre": (1, -10.0)}
    # Claude: netos +14, -8.5, +13
    assert b["n"] == 3 and b["ganadas"] == 2 and round(b["bruto"], 2) == 21.5 and round(b["neto"], 2) == 18.5
    assert round(b["gan_media"], 2) == 13.5 and round(b["per_media"], 2) == -8.5 and round(b["pf"], 3) == round(27 / 8.5, 3)
    assert round(b["media"], 4) == round(18.5 / 3, 4) and round(b["caida"], 2) == -8.5 and b["rechazadas"] == 1
    assert b["por_motivo"] == {"objetivo": (2, 27.0), "stop": (1, -8.5)} and b["llenado"] == 1.0
    solo = C.resumen(C.leer(ps), {"2026-10-06"})
    assert solo["n"] == 1 and solo["neto"] == -10.0
    txt = C.informe(C.leer(ps), C.leer(pc))
    assert "MARCADOR" in txt and "Operaciones cerradas" in txt and "Por día" in txt and "2026-10-06" in txt
    assert "No se puede declarar ganador" in txt and "anecdótico" in txt, txt        # 3 operaciones cada uno: es azar
    assert "Días ganados" in txt and "stop que sube" in txt and "objetivo" in txt
    ult = C.informe(C.leer(ps), C.leer(pc), dias_max=1)
    assert "1 día(s)" in ult and "2026-10-05" not in ult.split("Por día")[1]


def test_veredicto_con_muestra_grande():
    import random
    r = random.Random(7)
    sem = [fila("2026-10-%02d" % (1 + i // 10), "10:%02d:00" % (i % 60), "s", "salida", "A", pnl=round(r.gauss(0, 5), 2), com=0.5)
           for i in range(200)]
    cla = [fila("2026-10-%02d" % (1 + i // 10), "11:%02d:00" % (i % 60), "c", "salida", "B", pnl=round(r.gauss(4, 5), 2), com=0.5)
           for i in range(200)]
    d = tempfile.mkdtemp()
    ps, pc = os.path.join(d, "s.csv"), os.path.join(d, "c.csv")
    diario(ps, sem)
    diario(pc, cla)
    a, b = C.resumen(C.leer(ps)), C.resumen(C.leer(pc))
    v = C.veredicto(a, b)
    assert "Claude va por delante" in v and "2 errores estándar" in v, v
    assert "pocos" in C.veredicto(C.resumen([]), b) or "Todavía no hay" in C.veredicto(C.resumen([]), b)
    vacio = C.informe([], [])
    assert "No hay diario del semáforo" in vacio and "No hay diario de Claude" in vacio and "Todavía no hay" in vacio


def test_main():
    d = tempfile.mkdtemp()
    ps, pc = os.path.join(d, "s.csv"), os.path.join(d, "c.csv")
    diario(ps, [fila("2026-10-05", "10:05:00", "semáforo", "salida", "MU")])
    diario(pc, [fila("2026-10-05", "10:06:00", "Claude", "salida", "TSM", pnl=-4.0)])
    out = os.path.join(d, "informe.txt")
    assert C.main([ps, pc, "--salida", out]) == 0 and C.main([ps, pc, "--dias", "3", "--salida", out]) == 0
    assert "MARCADOR" in open(out, encoding="utf-8").read()
    try:
        C.main([ps])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("debe exigir los dos diarios")


if __name__ == "__main__":
    test_columnas_y_lectura()
    test_resumen_y_tabla()
    test_veredicto_con_muestra_grande()
    test_main()
    print("OK · comparar")
