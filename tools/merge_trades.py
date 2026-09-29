"""Fusiona las señales del día que expone /api/trades con el registro en data/live/ y rehace summary.json.
Uso: python tools/merge_trades.py today.json data/live"""
import glob
import json
import os
import sys

ORDER = ["pendiente", "abierta", "no ejecutada", "t1", "cierre", "stop", "t1-cerrada", "t2"]  # más avanzado a la derecha


def rank(st):
    return ORDER.index(st) if st in ORDER else 0


def main(src, out_dir):
    with open(src, encoding="utf-8") as f:
        cur = json.load(f)
    os.makedirs(out_dir, exist_ok=True)
    # días anteriores que el servidor aún recuerda (por si GitHub se saltó la corrida del cierre)
    for h in cur.get("history") or []:
        if h.get("day") and h.get("trades"):
            merge_day(h["day"], h["trades"], out_dir)
    # acepta /api/trades o /api/signals (el día sale de las propias señales si falta)
    day = cur.get("day") or next((t.get("day") for t in cur.get("trades") or [] if t.get("day")), None)
    if day and cur.get("trades"):
        merge_day(day, cur["trades"], out_dir)
    summary(out_dir)


def merge_day(day, cur_trades, out_dir):
    path = os.path.join(out_dir, f"{day}.json")
    old = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = {t["t"]: t for t in json.load(f).get("trades", [])}
    for t in cur_trades:
        o = old.get(t["t"])
        # el servidor puede haberse reiniciado: nunca retroceder un estado ya registrado
        if o is None or rank(t.get("status")) >= rank(o.get("status")) or t.get("hit1") and not o.get("hit1"):
            old[t["t"]] = t
    trades = sorted(old.values(), key=lambda x: x.get("time", ""))
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"day": day, "trades": trades}, f, ensure_ascii=False, indent=1)
    print(f"{day}: {len(trades)} señales")


def summary(out_dir):
    days, total = [], {"n": 0, "filled": 0, "t1": 0, "t2": 0, "stop": 0}
    for p in sorted(glob.glob(os.path.join(out_dir, "20*.json"))):
        with open(p, encoding="utf-8") as f:
            tr = json.load(f).get("trades", [])
        d = {"day": os.path.basename(p)[:10], "n": len(tr),
             "filled": sum(1 for x in tr if x.get("status") not in ("pendiente", "no ejecutada")),
             "t1": sum(1 for x in tr if x.get("hit1")), "t2": sum(1 for x in tr if x.get("status") == "t2"),
             "stop": sum(1 for x in tr if x.get("status") == "stop")}
        days.append(d)
        for k in total:
            total[k] += d[k]
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"days": len(days), "total": total, "byDay": days}, f, ensure_ascii=False, indent=1)
    print(f"acumulado {total}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
