"""Marcador acumulado: el ejecutor del semáforo (Priamo) contra el de Claude, desde sus diarios CSV.

Cada ejecutor apunta una línea por operación cerrada, cancelada o rechazada en su diario (diario_sem.csv y diario_cla.csv,
en esta misma carpeta). Aquí se juntan: operaciones, % de aciertos, ganancia y pérdida media, expectativa por operación,
factor de beneficio, caída máxima, resultado por día y por motivo de salida, y cuántas compras llegaron a llenarse.

Uso:  python comparar.py                 (todos los días)
      python comparar.py --dias 5        (solo los últimos 5 días con operaciones)
      python comparar.py a.csv b.csv     (otros diarios: el primero es «Tú», el segundo «Claude»)
Deja el resultado también en comparacion.txt. Solo lee los diarios: no toca ninguna orden ni conexión.

Ojo con las conclusiones: unas pocas decenas de operaciones no distinguen una estrategia buena de una con suerte. El
programa lo dice con números (error estándar de la diferencia) y no declara ganador si la diferencia cabe en el azar.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import statistics
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
NOMBRES = ("Tú (semáforo)", "Claude (pullback)")
POR = {"stop": "stop", "trailing": "stop que sube", "objetivo": "objetivo", "cierre": "cierre 15:55"}


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def leer(path: str) -> list[dict]:
    """Filas del diario (lista vacía si no existe o no se puede leer)."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return [r for r in csv.DictReader(f) if r.get("fecha") and r.get("evento")]
    except OSError:
        return []


def operaciones(filas: list[dict]) -> list[dict]:
    """Las ventas (cada una cierra una operación) con su resultado neto de comisiones."""
    out = []
    for r in filas:
        if r["evento"] != "salida":
            continue
        pnl = _f(r.get("pnl_usd"))
        if pnl is None:
            continue
        com = _f(r.get("comision_usd")) or 0.0
        out.append({"fecha": r["fecha"], "hora": r.get("hora") or "", "t": r.get("simbolo") or "?", "bruto": pnl,
                    "com": com, "neto": pnl - com, "R": _f(r.get("R")), "por": r.get("por") or "", "riesgo": _f(r.get("riesgo_usd"))})
    out.sort(key=lambda o: (o["fecha"], o["hora"]))
    return out


def caida_maxima(ops: list[dict]) -> float:
    acum = pico = peor = 0.0
    for o in ops:
        acum += o["neto"]
        pico = max(pico, acum)
        peor = min(peor, acum - pico)
    return peor


def resumen(filas: list[dict], dias: set[str] | None = None) -> dict:
    ops = [o for o in operaciones(filas) if dias is None or o["fecha"] in dias]
    fil = [r for r in filas if dias is None or r["fecha"] in dias]
    ganan = [o["neto"] for o in ops if o["neto"] > 0]
    pierden = [o["neto"] for o in ops if o["neto"] <= 0]
    n = len(ops)
    netos = [o["neto"] for o in ops]
    rs = [o["R"] for o in ops if o["R"] is not None]
    por_dia: dict[str, float] = defaultdict(float)
    por_motivo: dict[str, list[float]] = defaultdict(list)
    for o in ops:
        por_dia[o["fecha"]] += o["neto"]
        por_motivo[o["por"] or "otro"].append(o["neto"])
    sin_llenar = sum(1 for r in fil if r["evento"] == "cancelada")
    rechazadas = sum(1 for r in fil if r["evento"] == "rechazada")
    return {
        "n": n, "ganadas": len(ganan), "bruto": sum(o["bruto"] for o in ops), "com": sum(o["com"] for o in ops),
        "neto": sum(netos), "media": statistics.fmean(netos) if n else None,
        "sd": statistics.stdev(netos) if n > 1 else None,
        "gan_media": statistics.fmean(ganan) if ganan else None, "per_media": statistics.fmean(pierden) if pierden else None,
        "R_media": statistics.fmean(rs) if rs else None,
        "pf": (sum(ganan) / -sum(pierden)) if pierden and sum(pierden) < 0 else None,
        "caida": caida_maxima(ops), "mejor": max(netos) if n else None, "peor": min(netos) if n else None,
        "por_dia": dict(sorted(por_dia.items())), "por_motivo": {k: (len(v), sum(v)) for k, v in por_motivo.items()},
        "sin_llenar": sin_llenar, "rechazadas": rechazadas,
        "llenado": n / (n + sin_llenar) if (n + sin_llenar) else None,
    }


def _usd(v, signo=True) -> str:
    return "—" if v is None else (f"{v:+,.2f}" if signo else f"{v:,.2f}")


def _pct(v) -> str:
    return "—" if v is None else f"{100 * v:.0f}%"


def _num(v, d=2) -> str:
    return "—" if v is None else f"{v:.{d}f}"


def veredicto(a: dict, b: dict) -> str:
    """Quién va mejor y si la diferencia se puede distinguir del azar (error estándar de la diferencia de medias)."""
    if a["n"] < 2 or b["n"] < 2:
        return "Todavía no hay operaciones suficientes (al menos 2 de cada uno) para comparar nada."
    dif = (b["media"] or 0) - (a["media"] or 0)
    se = math.sqrt((a["sd"] or 0) ** 2 / a["n"] + (b["sd"] or 0) ** 2 / b["n"])
    quien = "Claude" if dif > 0 else "Tú"
    base = (f"Expectativa por operación: Tú {_usd(a['media'])} · Claude {_usd(b['media'])} · diferencia {_usd(dif)} ± {se:.2f} "
            f"(error estándar, {a['n']} y {b['n']} operaciones).")
    if se == 0 or abs(dif) < 2 * se:
        return base + (f"\nNo se puede declarar ganador: la diferencia cabe dentro del azar (haría falta que superara ±{2 * se:.2f}). "
                       f"Sigue acumulando días; con pocas operaciones un resultado así es anecdótico.")
    return base + (f"\n{quien} va por delante y la diferencia supera 2 errores estándar. Aun así son pocos días: "
                   f"un régimen de mercado distinto puede cambiarlo.")


def tabla(a: dict, b: dict) -> list[str]:
    filas = [
        ("Operaciones cerradas", str(a["n"]), str(b["n"])),
        ("Ganadas", f"{a['ganadas']} ({_pct(a['ganadas'] / a['n'] if a['n'] else None)})",
         f"{b['ganadas']} ({_pct(b['ganadas'] / b['n'] if b['n'] else None)})"),
        ("Resultado neto (US$)", _usd(a["neto"]), _usd(b["neto"])),
        ("  bruto / comisiones", f"{_usd(a['bruto'])} / {_usd(-a['com'])}", f"{_usd(b['bruto'])} / {_usd(-b['com'])}"),
        ("Por operación (US$)", _usd(a["media"]), _usd(b["media"])),
        ("Ganancia media / pérdida media", f"{_usd(a['gan_media'])} / {_usd(a['per_media'])}",
         f"{_usd(b['gan_media'])} / {_usd(b['per_media'])}"),
        ("R medio (resultado ÷ riesgo)", _num(a["R_media"]), _num(b["R_media"])),
        ("Factor de beneficio", _num(a["pf"]), _num(b["pf"])),
        ("Mejor / peor operación", f"{_usd(a['mejor'])} / {_usd(a['peor'])}", f"{_usd(b['mejor'])} / {_usd(b['peor'])}"),
        ("Caída máxima acumulada (US$)", _usd(a["caida"]), _usd(b["caida"])),
        ("Compras que se llenaron", _pct(a["llenado"]), _pct(b["llenado"])),
        ("Canceladas sin llenar / rechazadas", f"{a['sin_llenar']} / {a['rechazadas']}", f"{b['sin_llenar']} / {b['rechazadas']}"),
    ]
    w = max(len(f[0]) for f in filas) + 2
    out = [f"{'':<{w}}{NOMBRES[0]:>20}{NOMBRES[1]:>22}", "-" * (w + 42)]
    out += [f"{k:<{w}}{x:>20}{y:>22}" for k, x, y in filas]
    return out


def informe(sem: list[dict], cla: list[dict], dias_max: int | None = None) -> str:
    todos = sorted({o["fecha"] for o in operaciones(sem)} | {o["fecha"] for o in operaciones(cla)})
    dias = set(todos[-dias_max:]) if dias_max else None
    a, b = resumen(sem, dias), resumen(cla, dias)
    out = [f"MARCADOR · Tú (semáforo) contra Claude (pullback con tendencia) · cuenta paper · "
           f"{len(dias) if dias else len(todos)} día(s) con operaciones", ""]
    if not sem:
        out.append("(No hay diario del semáforo todavía: diario_sem.csv)")
    if not cla:
        out.append("(No hay diario de Claude todavía: diario_cla.csv)")
    out += tabla(a, b) + [""]
    dd = sorted(set(a["por_dia"]) | set(b["por_dia"]))
    if dd:
        out += ["Por día (US$ netos):", f"{'fecha':<12}{'Tú':>12}{'Claude':>12}   quién"]
        gana = {"Tú": 0, "Claude": 0, "empate": 0}
        for d in dd:
            x, y = a["por_dia"].get(d), b["por_dia"].get(d)
            q = "—" if x is None or y is None else ("Tú" if x > y else "Claude" if y > x else "empate")
            if q in gana:
                gana[q] += 1
            out.append(f"{d:<12}{_usd(x):>12}{_usd(y):>12}   {q}")
        out.append(f"Días ganados (cuando operaron los dos): Tú {gana['Tú']} · Claude {gana['Claude']} · empate {gana['empate']}")
        out.append("")
    for nombre, r in zip(NOMBRES, (a, b)):
        if r["por_motivo"]:
            out.append(f"{nombre}: " + " · ".join(f"{POR.get(k, k)} {n} ({_usd(s)})" for k, (n, s) in sorted(r["por_motivo"].items())))
    out += ["", veredicto(a, b), "",
            "Lo que no dice este marcador: ambos operan la misma cuenta y los mismos precios, pero con reglas distintas "
            "(tuyo: señales del semáforo y entradas 9:30–12:00 ET con stop que sube; Claude: pullbacks de líderes, "
            "9:50–15:15 ET, stop fijo y objetivo de 2R). Cada uno cuenta solo sus órdenes (cada orden lleva su prefijo en IBKR)."]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Marcador acumulado: semáforo (Priamo) contra Claude")
    ap.add_argument("diarios", nargs="*", help="diario del semáforo y diario de Claude (por defecto, los de esta carpeta)")
    ap.add_argument("--dias", type=int, default=None, help="solo los últimos N días con operaciones")
    ap.add_argument("--salida", default=os.path.join(HERE, "comparacion.txt"), help="dónde dejar el informe en texto")
    a = ap.parse_args(argv)
    if len(a.diarios) not in (0, 2):
        ap.error("pasa los dos diarios (semáforo y Claude) o ninguno")
    sem_p, cla_p = a.diarios or (os.path.join(HERE, "diario_sem.csv"), os.path.join(HERE, "diario_cla.csv"))
    txt = informe(leer(sem_p), leer(cla_p), a.dias)
    print(txt)
    try:
        with open(a.salida, "w", encoding="utf-8") as f:
            f.write(txt + "\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # la consola de Windows no siempre acepta acentos y símbolos
    except (AttributeError, OSError):
        pass
    sys.exit(main())
