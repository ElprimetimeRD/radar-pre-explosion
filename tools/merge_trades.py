"""Fusiona las señales del día que expone /api/trades con el registro en data/live/ y rehace summary.json.
También guarda las compras stop de los avisos ARMA ("armadas") para comparar la vía rápida con la COMPRA confirmada.
Uso: python tools/merge_trades.py today.json data/live"""
import glob
import json
import os
import sys

# más avanzado a la derecha
ORDER = ["pendiente", "abierta", "no ejecutada", "cancelada", "t1", "cierre", "stop", "t1-cerrada", "t2"]
FILLED = ("abierta", "t1", "t2", "stop", "t1-cerrada", "cierre")


def rank(st):
    return ORDER.index(st) if st in ORDER else 0


def main(src, out_dir):
    with open(src, encoding="utf-8") as f:
        cur = json.load(f)
    os.makedirs(out_dir, exist_ok=True)
    # días anteriores que el servidor aún recuerda (por si GitHub se saltó la corrida del cierre)
    for h in cur.get("history") or []:
        if h.get("day") and (h.get("trades") or h.get("armadas")):
            merge_day(h["day"], h.get("trades") or [], out_dir, h.get("armadas") or [])
    # acepta /api/trades o /api/signals (el día sale de las propias señales si falta)
    sig = (cur.get("trades") or []) + (cur.get("armadas") or [])
    day = cur.get("day") or next((t.get("day") for t in sig if t.get("day")), None)
    if day and sig:
        merge_day(day, cur.get("trades") or [], out_dir, cur.get("armadas") or [])
    summary(out_dir)


def merge_list(old_list, cur_list):
    old = {t["t"]: t for t in old_list}
    for t in cur_list:
        o = old.get(t["t"])
        # el servidor puede haberse reiniciado: nunca retroceder un estado ya registrado
        if o is None or rank(t.get("status")) >= rank(o.get("status")) or t.get("hit1") and not o.get("hit1"):
            old[t["t"]] = t
    return sorted(old.values(), key=lambda x: x.get("time", ""))


def merge_day(day, cur_trades, out_dir, cur_arms=()):
    path = os.path.join(out_dir, f"{day}.json")
    prev = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
    out = {"day": day, "trades": merge_list(prev.get("trades", []), cur_trades)}
    arms = merge_list(prev.get("armadas", []), cur_arms)
    if arms:
        out["armadas"] = arms
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"{day}: {len(out['trades'])} señales · {len(arms)} armadas")


def count(tr, filled):
    return {"n": len(tr), "filled": sum(1 for x in tr if filled(x)), "t1": sum(1 for x in tr if x.get("hit1")),
            "t2": sum(1 for x in tr if x.get("status") == "t2"), "stop": sum(1 for x in tr if x.get("status") == "stop")}


def summary(out_dir):
    days, keys = [], ("n", "filled", "t1", "t2", "stop")
    total, arm_total = dict.fromkeys(keys, 0), dict.fromkeys(keys + ("cancelled",), 0)
    lags, slips = [], []
    for p in sorted(glob.glob(os.path.join(out_dir, "20*.json"))):
        with open(p, encoding="utf-8") as f:
            j = json.load(f)
        tr, arms = j.get("trades", []), j.get("armadas", [])
        d = {"day": os.path.basename(p)[:10], **count(tr, lambda x: x.get("status") not in ("pendiente", "no ejecutada"))}
        if arms:
            a = count(arms, lambda x: x.get("status") in FILLED)
            a["cancelled"] = sum(1 for x in arms if x.get("status") in ("cancelada", "no ejecutada"))
            d["armadas"] = a
            for k in arm_total:
                arm_total[k] += a[k]
        days.append(d)
        for k in total:
            total[k] += d[k]
        lags += [x["lag_min"] for x in tr if x.get("lag_min") is not None]
        slips += [x["slip"] for x in tr if x.get("slip") is not None]
    out = {"days": len(days), "total": total, "armadas": arm_total, "byDay": days,
           "lag": round(sum(lags) / len(lags), 1) if lags else None,
           "slip": round(sum(slips) / len(slips), 2) if slips else None}
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"acumulado {total} · armadas {arm_total}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
