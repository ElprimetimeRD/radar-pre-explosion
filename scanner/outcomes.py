"""Mide qué pasó después de cada escaneo y resume los aciertos para la página."""
from __future__ import annotations

import glob
import os
from datetime import date, datetime, timedelta

from .util import DATA, log, read_json, rnd, write_json

FAM_BITS = ["catalizador", "opciones", "squeeze", "volumen", "atencion", "insiders", "estructura", "compresion"]
WINDOW_B = 20
KEEP_DAYS = 180


def _records():
    for path in sorted(glob.glob(os.path.join(DATA, "scans", "*.json"))):
        rec = read_json(path)
        if rec:
            yield path, rec


def update_outcomes(today: date, phase: str, history_fn=None):
    """Completa [subida máx., caída máx.] en %: intradía = la sesión del escaneo; swing = las 20 sesiones siguientes."""
    if history_fn is None:
        from .sources.yahoo import history as history_fn  # noqa: N806
    pending, need = [], set()
    for path, rec in _records():
        d = date.fromisoformat(rec["date"])
        if (today - d).days > KEEP_DAYS:
            os.remove(path)
            continue
        if rec.get("resolved"):
            continue
        if rec["H"] == "A" and (d < today or phase in ("after-hours", "cerrado")) and (today - d).days <= 20:
            pending.append((path, rec))
        elif rec["H"] == "B" and (today - d).days >= 28 and (today - d).days <= 60:
            pending.append((path, rec))
        else:
            continue
        need.update(r["t"] for r in rec["rows"])
    if not pending:
        return 0
    hist = history_fn(sorted(need), period="3mo", interval="1d")
    done = 0
    for path, rec in pending:
        d = date.fromisoformat(rec["date"])
        for r in rec["rows"]:
            df = hist.get(r["t"])
            px = r.get("px")
            if df is None or df.empty or not px:
                continue
            idx = [x.date() if hasattr(x, "date") else x for x in df.index]
            if rec["H"] == "A":
                sel = [i for i, x in enumerate(idx) if x == d]
                if not sel:
                    continue
                bar = df.iloc[sel[0]]
                hi, lo = float(bar["High"]), float(bar["Low"])
            else:
                after = [i for i, x in enumerate(idx) if x > d][:WINDOW_B]
                if len(after) < WINDOW_B:
                    continue
                win = df.iloc[after]
                hi, lo = float(win["High"].max()), float(win["Low"].min())
            r["o"] = [rnd(100 * (hi / px - 1), 1), rnd(100 * (lo / px - 1), 1)]
        rec["resolved"] = True
        write_json(path, rec)
        done += 1
    log.info("resultados completados: %d escaneos", done)
    return done


def build_scanlog(H: str, max_scans=30, max_rows=3000):
    """Filas resueltas compactas: [score, cobertura×100, máscara de familias, suaves, duras, subida, caída, ticker, fecha]."""
    recs = [rec for _, rec in _records() if rec["H"] == H and rec.get("resolved")]
    recs = recs[-max_scans:]
    rows = []
    for rec in reversed(recs):
        for r in rec["rows"]:
            if "o" not in r:
                continue
            mask = sum(1 << i for i, f in enumerate(FAM_BITS) if f in r.get("firing", []))
            rows.append([r["score"], round(100 * (r.get("cov") or 0)), mask, r.get("soft", 0), r.get("hard", 0),
                         r["o"][0], r["o"][1], r["t"], rec["date"]])
            if len(rows) >= max_rows:
                break
        if len(rows) >= max_rows:
            break
    doc = {"H": H, "updatedAt": datetime.utcnow().isoformat() + "Z", "scans": len(recs), "famBits": FAM_BITS,
           "window": "la sesión del escaneo" if H == "A" else f"{WINDOW_B} sesiones", "rows": rows}
    write_json(os.path.join(DATA, "artifact", f"scanlog-{H}.json"), doc)
    return doc
