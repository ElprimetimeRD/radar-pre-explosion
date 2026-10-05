"""Repaso histórico de la estrategia de Claude (bridge/estrategia_claude.py) con las velas de 1 min guardadas en
data/diag (rama diag-velas). Sirve para comprobar la MECÁNICA (que las órdenes salen donde deben, cuántas se llenan, cómo
salen), no como prueba de ganancias: esas velas son de las acciones que más corrieron cada día (sesgo a favor de quien
compra fuerza) y de acciones al azar de pequeña y mediana capitalización, no de la lista que opera el ejecutor.

Simula el día minuto a minuto con las reglas del ejecutor: US$500 por operación, US$1,000 comprometidos, pérdida máxima
del día US$100, 20 órdenes por día, una orden o posición por acción, cierre a las 15:55. Los datos llegan con retraso
(--demora velas), la compra es una orden límite que espera 10 min y, si en una misma vela toca el stop y el objetivo,
gana el stop (el caso malo).

Uso: PYTHONPATH=bridge python tools/backtest_claude.py DIR [--demora 2] [--roles control,mover] [--costo 0.0004]
DIR tiene ds_bars.csv.gz y ds_meta.json.gz.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import gzip
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bridge"))
import estrategia_claude as S  # noqa: E402

MAX_ABIERTO, PERDIDA_MAX, MAX_ORDENES = 1000.0, 100.0, 20
CIERRE_M = 15 * 60 + 55
ENFRIA_M = 20
SLIP = 0.0005   # deslizamiento de un stop (0.05 %)


def cargar(d: str):
    meta = {(m["day"], m["t"]): m for m in json.load(gzip.open(os.path.join(d, "ds_meta.json.gz"), "rt"))}
    series = defaultdict(lambda: defaultdict(dict))   # día -> t -> {m: vela}
    with gzip.open(os.path.join(d, "ds_bars.csv.gz"), "rt") as f:
        for r in csv.DictReader(f):
            series[r["day"]][r["t"]][int(r["m"])] = (int(r["m"]), float(r["o"]), float(r["h"]), float(r["l"]),
                                                     float(r["c"]), float(r["v"]))
    return meta, series


def dia(day, series, meta, roles, cfg, demora, costo, spy_filter=True):
    ser = series[day]
    syms = [t for t in ser if (day, t) in meta and meta[(day, t)]["role"] in roles]
    reg = [t for t in ("SPY",) if t in ser]
    spy_prev = (meta.get((day, "SPY")) or {}).get("prev")
    pend, pos, enfria, trades, sin_llenar = {}, {}, {}, [], 0
    ordenes = 0
    pnl_dia, parado = 0.0, False
    orden = {t: sorted(ser[t]) for t in list(syms) + reg}
    lista = {t: [ser[t][k] for k in orden[t]] for t in orden}
    adv = {t: (meta[(day, t)]["base"] or [0])[-1] for t in syms}
    atr = {t: (meta[(day, t)].get("atr") or 3.0) / 100 for t in syms}
    prev = {t: meta[(day, t)]["prev"] for t in syms}
    for m in range(S.OPEN_M, S.CLOSE_M):
        # 1) lo que pasa en la vela m con las órdenes y posiciones que ya existían
        for t in list(pend):
            o, b = pend[t], ser[t].get(m)
            if b is None:
                continue
            if m >= o["exp_m"]:
                del pend[t]
                sin_llenar += 1
                enfria[t] = m
                continue
            if b[3] <= o["limite"] - 0.01 or b[1] <= o["limite"]:
                px = min(o["limite"], b[1])
                p = {**o, "px_e": px, "m_in": m}
                del pend[t]
                pos[t] = p
                if b[3] <= p["stop"]:   # en la misma vela: pesimista, tocó el stop
                    salir(t, p, min(p["stop"], b[1]) * (1 - SLIP), m, "stop", pos, trades, costo)
                    pnl_dia = sum(x["pnl"] for x in trades)
                    enfria[t] = m
        for t in list(pos):
            p, b = pos[t], ser[t].get(m)
            if b is None or m == p["m_in"]:
                continue
            if m >= CIERRE_M:
                salir(t, p, b[1], m, "cierre", pos, trades, costo)
            elif b[1] <= p["stop"] or b[3] <= p["stop"]:
                salir(t, p, min(p["stop"], b[1]) * (1 - SLIP), m, "stop", pos, trades, costo)
            elif b[2] >= p["objetivo"] + 0.01:
                salir(t, p, max(p["objetivo"], b[1]), m, "objetivo", pos, trades, costo)
            if t not in pos:
                enfria[t] = m
        pnl_dia = sum(x["pnl"] for x in trades)
        if m >= CIERRE_M:
            sin_llenar += len(pend)
            pend.clear()
            break
        if pnl_dia <= -PERDIDA_MAX + 0.01:
            parado = True
        # 2) al final de la vela m: decide con lo que ya se ve (retrasado)
        if parado or ordenes >= MAX_ORDENES or not cfg.ini_m <= m + 1 < cfg.fin_m:
            continue
        spy_b = lista["SPY"][:bisect.bisect_right(orden["SPY"], m - demora)] if reg else []
        if not (reg and spy_filter):
            reg_ok = (True, "sin filtro")
        else:
            reg_ok = S.regimen(spy_b, spy_prev, cfg)
        datos = {}
        for t in syms:
            if t in pend or t in pos or m - enfria.get(t, -999) < ENFRIA_M:
                continue
            bs = lista[t][:bisect.bisect_right(orden[t], m - demora)]
            if len(bs) >= 15:
                datos[t] = {"bars": bs, "prev": prev[t], "adv": adv[t], "atr": atr[t]}
        js, _ = S.buscar(datos, reg_ok, cfg)
        for j in js:
            comp = sum(p["qty"] * p["px_e"] for p in pos.values()) + sum(o["qty"] * o["limite"] for o in pend.values())
            costo_o = j["qty"] * j["limite"]
            riesgo = sum(p["qty"] * p["trail"] for p in pos.values()) + sum(o["qty"] * o["trail"] for o in pend.values())
            perd = max(0.0, -pnl_dia)
            if (comp + costo_o > MAX_ABIERTO + 0.01 or perd + riesgo + j["qty"] * j["trail"] > PERDIDA_MAX + 0.01
                    or costo_o > cfg.orden_usd + 0.01 or ordenes >= MAX_ORDENES):
                continue
            pend[j["t"]] = {**j, "exp_m": m + 1 + cfg.ttl_s // 60}
            ordenes += 1
    return trades, sin_llenar, ordenes


def salir(t, p, px, m, por, pos, trades, costo):
    bruto = p["qty"] * (px - p["px_e"])
    com = costo * p["qty"] * (px + p["px_e"])
    trades.append({"t": t, "m_in": p["m_in"], "m_out": m, "px_e": p["px_e"], "px_s": px, "qty": p["qty"],
                   "pnl": bruto - com, "bruto": bruto, "R": bruto / (p["qty"] * p["trail"]), "por": por,
                   "retr": p["retr"], "rvol": p["rvol"], "chg": p["chg"]})
    pos.pop(t, None)


def resumen(nombre, trades, ordenes, sin_llenar):
    n = len(trades)
    if not n:
        print(f"{nombre}: {ordenes} órdenes, 0 llenadas")
        return
    gan = [x for x in trades if x["pnl"] > 0]
    perd = [x for x in trades if x["pnl"] <= 0]
    tot = sum(x["pnl"] for x in trades)
    pf = sum(x["pnl"] for x in gan) / abs(sum(x["pnl"] for x in perd)) if perd and sum(x["pnl"] for x in perd) else float("inf")
    por = defaultdict(int)
    for x in trades:
        por[x["por"]] += 1
    print(f"{nombre}: {ordenes} órdenes, {n} llenadas ({100 * n / max(1, ordenes):.0f}%) · aciertos {100 * len(gan) / n:.0f}% · "
          f"P/L {tot:+.0f} US$ ({tot / n:+.2f}/op) · R medio bruto {sum(x['R'] for x in trades) / n:+.2f} · "
          f"factor de ganancia {pf:.2f} · salidas {dict(por)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--demora", type=int, default=2)
    ap.add_argument("--roles", default="control,mover")
    ap.add_argument("--costo", type=float, default=0.0004)
    ap.add_argument("--sin-spy", action="store_true")
    ap.add_argument("--cfg", default="", help="cambios a la configuración: clave=valor,clave=valor")
    ap.add_argument("--dias", action="store_true", help="detalle por día")
    a = ap.parse_args()
    meta, series = cargar(a.dir)
    cfg = S.CFG
    for kv in filter(None, a.cfg.split(",")):
        k, v = kv.split("=")
        cfg = S.con(cfg, **{k: type(getattr(cfg, k))(float(v) if "." in v else int(v))})
    roles = set(a.roles.split(","))
    todos, ords, sl = [], 0, 0
    for d in sorted(series):
        if len(series[d]) < 20:
            continue
        tr, s, o = dia(d, series, meta, roles, cfg, a.demora, a.costo, not a.sin_spy)
        todos += [{**x, "dia": d} for x in tr]
        ords += o
        sl += s
        if a.dias:
            print(f"  {d}: {o} órdenes, {len(tr)} ops, P/L {sum(x['pnl'] for x in tr):+.1f}")
    resumen(f"roles={a.roles} demora={a.demora} {a.cfg}", todos, ords, sl)
    return todos


if __name__ == "__main__":
    main()
