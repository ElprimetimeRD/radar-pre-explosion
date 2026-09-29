"""Motor del Radar Pre-Explosión: port exacto de engine.js (mismos pesos, escalas y reglas).

Puntos de evidencia sobre 100. Lo no medido vale 0; "no aplica" se excluye y el resto se reescala.
"""
from __future__ import annotations

import math


def clamp(x, lo=0.0, hi=1.0):
    return min(hi, max(lo, x))


def lin(x, a, b):
    return clamp((x - a) / (b - a))


def logmap(x, a, b):
    return 0.0 if x <= 0 else clamp(math.log(x / a) / math.log(b / a))


def has(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


FAM = {
    "catalizador": "Catalizador", "opciones": "Flujo de opciones", "squeeze": "Presión de cortos",
    "insiders": "Compras de insiders", "estructura": "Precio y momentum", "compresion": "Compresión y volumen",
    "volumen": "Gap y volumen relativo", "atencion": "Atención retail",
}
WEIGHTS = {
    "A": {"S": {"catalizador": 25, "opciones": 20, "squeeze": 25, "volumen": 20, "atencion": 10},
          "L": {"catalizador": 30, "opciones": 35, "squeeze": 10, "volumen": 15, "atencion": 10}},
    "B": {"S": {"catalizador": 25, "insiders": 20, "estructura": 20, "opciones": 15, "squeeze": 12, "compresion": 8},
          "L": {"catalizador": 25, "insiders": 12, "estructura": 25, "opciones": 25, "squeeze": 5, "compresion": 8}},
}
ORDER = {"A": ["catalizador", "opciones", "squeeze", "volumen", "atencion"],
         "B": ["catalizador", "insiders", "estructura", "opciones", "squeeze", "compresion"]}
CAT_S = {"none": 0, "softpr": 0.25, "theme": 0.4, "binary": 0.45, "analyst": 0.5, "index": 0.6,
         "contract": 0.7, "mna": 0.75, "earnings": 0.8, "fda": 0.85}
AGE_M = {"fresh": {"A": 1, "B": 1}, "d3": {"A": 0.6, "B": 1}, "d20": {"A": 0.3, "B": 0.75},
         "old": {"A": 0.1, "B": 0.3}, "ahead": {"A": 1, "B": 0.9}}
INS_M = {"": 0.7, "opp": 1, "routine": 0.15, "plan": 0.1}
DEFAULT_TH = {"watch": 50, "alert": 65, "trigger": 80}
TIER_RANK = {"veto": -1, "none": 0, "watch": 1, "alert": 2, "trigger": 3}


def gap_map(g):
    if g <= 3:
        return 0.0
    if g <= 12:
        return lin(g, 3, 12)
    if g <= 30:
        return 1.0
    if g <= 45:
        return 1 - 0.4 * lin(g, 30, 45)
    return 0.5


# (id, familia, horizontes, subpesos, escala) — solo datos numéricos/tri con subpeso genérico
def _m_ctb(v, st):
    return clamp(logmap(v, 3, 50) + (0.25 if st.get("ctbUp") else 0))


GENERIC = [
    ("callVolOI", "opciones", {"A": 0.4, "B": 0.25}, lambda v, st: lin(v, 1, 4)),
    ("putCall", "opciones", {"A": 0.3, "B": 0.25}, lambda v, st: lin(v, 1, 0.35)),
    ("ivSpread", "opciones", {"A": 0.2, "B": 0.3}, lambda v, st: lin(v, -1, 3)),
    ("smirk", "opciones", {"A": 0.1, "B": 0.2}, lambda v, st: lin(v, 12, 3)),
    ("util", "squeeze", {"A": 0.35, "B": 0.35}, lambda v, st: lin(v, 70, 97)),
    ("ctb", "squeeze", {"A": 0.25, "B": 0.25}, _m_ctb),
    ("siFloat", "squeeze", {"A": 0.25, "B": 0.25}, lambda v, st: lin(v, 10, 35)),
    ("dtc", "squeeze", {"A": 0.15, "B": 0.15}, lambda v, st: lin(v, 2, 8)),
    ("pct52", "estructura", {"B": 0.5}, lambda v, st: lin(v, 75, 98)),
    ("rel6m", "estructura", {"B": 0.5}, lambda v, st: lin(v, 0, 40)),
    ("gap", "volumen", {"A": 0.5}, lambda v, st: gap_map(v)),
    ("rvol", "volumen", {"A": 0.5}, lambda v, st: lin(v, 1.5, 6)),
]
FEAT_HORIZONS = {
    "catType": "AB", "catAge": "AB", "eps": "AB", "guide": "AB", "callVolOI": "AB", "putCall": "AB", "ivSpread": "AB",
    "smirk": "AB", "util": "AB", "ctb": "AB", "ctbUp": "AB", "siFloat": "AB", "dtc": "AB", "insN": "B", "insKind": "B",
    "insCeo": "B", "pct52": "B", "rel6m": "B", "maxRet": "B", "bbw": "B", "vcp": "B", "dryUp": "B", "gap": "A",
    "rvol": "A", "pmVol": "A", "ment": "A", "trends": "A",
}
FEAT_FAM = {"catType": "catalizador", "eps": "catalizador", "guide": "catalizador", "insN": "insiders",
            "bbw": "compresion", "vcp": "compresion", "dryUp": "compresion", "ment": "atencion", "trends": "atencion"}
for _id, _fam, _sub, _m in GENERIC:
    FEAT_FAM[_id] = _fam

VETOES = [("atm", None), ("pump", None), ("shelf", None), ("rs", None), ("warrants", None),
          ("runway", None), ("ipo", None), ("sub1", None), ("halts", "A")]
VETO_LABEL = {
    "atm": "Oferta o ATM activa: 424B5, S-1 o \"at-the-market\" en los últimos 30 días",
    "pump": "Promoción pagada, alertas en chats o subida sin noticia verificable",
    "shelf": "S-3 shelf efectivo: puede emitir acciones en cualquier momento",
    "rs": "Reverse split en los últimos 90 días", "warrants": "Warrants o convertibles con ejercicio cerca del precio",
    "runway": "Caja para menos de 6 meses de operación", "ipo": "IPO hace menos de 12 meses con float < 10 M",
    "sub1": "Precio < $1 o aviso de incumplimiento de listado", "halts": "3 o más halts LULD hoy",
}


def _veto_kind(vid, seg):
    if vid == "atm":
        return "hard" if seg == "S" else "soft"
    return "hard" if vid == "pump" else "soft"


def _generic(st, H, fam):
    s = cov = 0.0
    for fid, f, sub, m in GENERIC:
        if f != fam or H not in sub:
            continue
        v = st.get(fid)
        if not has(v):
            continue
        s += sub[H] * m(v, st)
        cov += sub[H]
    return {"s": clamp(s), "cov": clamp(cov), "notes": []}


def _catalizador(st, H):
    t = st.get("catType") or ""
    if not t:
        return {"s": 0.0, "cov": 0.0, "notes": []}
    if t == "none":
        return {"s": 0.0, "cov": 1.0, "notes": []}
    notes = []
    s = CAT_S.get(t, 0)
    if t == "earnings":
        e = lin(st["eps"], 0, 20) if has(st.get("eps")) else 0
        s = clamp(0.5 + 0.3 * e + 0.2 * (1 if st.get("guide") == "yes" else 0))
    age = st.get("catAge") or ""
    if t == "binary":
        mult = 1 if (not age or age == "ahead") else AGE_M[age][H]
        notes.append("Evento binario: anticipa el tamaño del movimiento, no la dirección.")
    elif age:
        mult = AGE_M[age][H]
    else:
        mult = 0.8
    return {"s": clamp(s * mult), "cov": 1.0, "notes": notes}


def _insiders(st):
    n = st.get("insN")
    if not has(n):
        return {"s": 0.0, "cov": 0.0, "notes": []}
    base = 0 if n <= 0 else 0.4 if n < 2 else 0.75 if n < 3 else 1
    k = INS_M.get(st.get("insKind") or "", 0.7)
    ceo = 0.15 if st.get("insCeo") and n >= 1 else 0
    return {"s": clamp((base + ceo) * k), "cov": 1.0, "notes": []}


def _compresion(st, fams):
    b = lin(st["bbw"], 50, 5) if has(st.get("bbw")) else None
    v = 0.8 if st.get("vcp") == "yes" else (0 if st.get("vcp") == "no" else None)
    d = 1 if st.get("dryUp") == "yes" else (0 if st.get("dryUp") == "no" else None)
    raw = cov = 0.0
    if b is not None or v is not None:
        raw += 0.6 * max(b or 0, v or 0)
        cov += 0.6
    if d is not None:
        raw += 0.4 * d
        cov += 0.4
    dir_src = max(fams.get("estructura", {}).get("s", 0), fams.get("opciones", {}).get("s", 0))
    return {"s": clamp(raw * (0.3 + 0.7 * dir_src)), "cov": cov, "notes": []}


def _volumen(st, seg):
    s = cov = 0.0
    notes = []
    if has(st.get("gap")):
        g = gap_map(st["gap"])
        if st["gap"] >= 45 and seg == "S":
            notes.append("Gap ≥ 45 % en small cap: ~66 % se desvanecen en la sesión.")
            if has(st.get("pmVol")) and st["pmVol"] >= 5:
                g *= 0.8
            if has(st.get("price")) and st["price"] < 1:
                g *= 0.7
        s += 0.5 * g
        cov += 0.5
    if has(st.get("rvol")):
        s += 0.5 * lin(st["rvol"], 1.5, 6)
        cov += 0.5
    return {"s": clamp(s), "cov": cov, "notes": notes}


def _atencion(st):
    a = lin(st["ment"], 1.5, 6) if has(st.get("ment")) else None
    b = lin(st["trends"], 1.5, 5) if has(st.get("trends")) else None
    if a is None and b is None:
        return {"s": 0.0, "cov": 0.0, "notes": []}
    return {"s": max(a or 0, b or 0), "cov": 1.0, "notes": []}


def segment_of(st):
    if st.get("seg") in ("S", "L"):
        return st["seg"]
    if has(st.get("mcap")) and st["mcap"] < 2000:
        return "S"
    if has(st.get("float")) and st["float"] < 20:
        return "S"
    if has(st.get("mcap")):
        return "L"
    return "S"


def tier_of(H, score, coverage, firing, soft_n, hard_n, th=None):
    th = th or DEFAULT_TH
    if hard_n > 0:
        return "veto"
    nF = len(firing)
    t = "none"
    if score >= th["watch"]:
        t = "watch"
    if score >= th["alert"] and nF >= 2 and coverage >= 0.5:
        t = "alert"
    if t == "alert" and score >= th["trigger"]:
        if H == "A":
            k = sum(1 for x in ("opciones", "squeeze", "volumen") if x in firing)
            ok = "catalizador" in firing and k >= 2
        else:
            ok = nF >= 3 and any(x in firing for x in ("insiders", "opciones", "catalizador"))
        if ok and coverage >= 0.65 and soft_n <= 1:
            t = "trigger"
    if soft_n >= 3 and TIER_RANK[t] > TIER_RANK["watch"]:
        t = "watch"
    elif soft_n >= 2 and TIER_RANK[t] > TIER_RANK["alert"]:
        t = "alert"
    return t


def evaluate(st, th=None):
    H = "B" if st.get("horizon") == "B" else "A"
    seg = segment_of(st)
    W = WEIGHTS[H][seg]
    fams = {}
    for fid in ("opciones", "catalizador", "insiders", "estructura", "volumen", "atencion", "squeeze", "compresion"):
        if fid not in W:
            continue
        if fid == "opciones":
            fams[fid] = {"s": 0.0, "cov": 0.0, "na": True, "notes": []} if st.get("optNA") else _generic(st, H, fid)
        elif fid == "catalizador":
            fams[fid] = _catalizador(st, H)
        elif fid == "insiders":
            fams[fid] = _insiders(st)
        elif fid == "volumen":
            fams[fid] = _volumen(st, seg)
        elif fid == "atencion":
            fams[fid] = _atencion(st)
        elif fid == "compresion":
            fams[fid] = _compresion(st, fams)
        else:
            fams[fid] = _generic(st, H, fid)

    def fires(fid):
        f = fams.get(fid)
        return bool(f) and not f.get("na") and f["cov"] > 0 and f["s"] >= 0.5

    detonator = fires("catalizador") or fires("opciones")
    sq = fams.get("squeeze")
    if sq and sq["cov"] > 0 and not detonator:
        sq["s"] *= 0.5
        sq["notes"].append("Sin catalizador ni flujo de opciones que lo detonen, cuenta a la mitad.")

    total = earned = cov_w = 0.0
    rows = []
    for fid in ORDER[H]:
        if fid not in W:
            continue
        r = fams[fid]
        na = bool(r.get("na"))
        if not na:
            total += W[fid]
            earned += W[fid] * r["s"]
            cov_w += W[fid] * r["cov"]
        rows.append({"id": fid, "w": W[fid], "s": r["s"], "cov": r["cov"], "na": na, "notes": r["notes"],
                     "firing": (not na) and r["cov"] > 0 and r["s"] >= 0.5})
    scale = 100 / total if total > 0 else 0
    for r in rows:
        r["max"] = 0 if r["na"] else r["w"] * scale
        r["pts"] = 0 if r["na"] else r["w"] * r["s"] * scale
    base = earned * scale
    coverage = cov_w / total if total > 0 else 0

    hard, soft = [], []
    for vid, only in VETOES:
        if only and only != H:
            continue
        if st.get("v_" + vid):
            (hard if _veto_kind(vid, seg) == "hard" else soft).append(vid)
    if has(st.get("spread")) and st["spread"] > 2:
        soft.append("spread")
    if has(st.get("price")) and st["price"] < 1 and not st.get("v_sub1"):
        soft.append("sub1a")
    at = fams.get("atencion")
    if H == "A" and seg == "S" and at and at["cov"] > 0 and at["s"] >= 0.33 and (st.get("catType") or "") in ("", "none", "softpr", "theme"):
        soft.append("hype")

    adj = []
    if (has(st.get("siFloat")) and st["siFloat"] >= 20
            and ((has(st.get("util")) and st["util"] < 60) or (has(st.get("ctb")) and st["ctb"] < 5)) and not detonator):
        adj.append(("si", -5.0))
    if H == "B" and has(st.get("maxRet")) and st["maxRet"] > 15:
        adj.append(("max", -8 * lin(st["maxRet"], 15, 40)))
    if st.get("regime") == "off":
        adj.append(("regime", -7.0))
    if st.get("regime") == "on":
        adj.append(("regime", 3.0))
    soft_pts = 0.0
    for f in soft:
        p = max(-5.0, -20 - soft_pts)
        if p < 0:
            adj.append(("soft-" + f, p))
            soft_pts += p
    score = clamp(base + sum(p for _, p in adj), 0, 100)
    firing = [r["id"] for r in rows if r["firing"]]
    tier = tier_of(H, score, coverage, firing, len(soft), len(hard), th)
    return {"H": H, "seg": seg, "rows": rows, "base": base, "coverage": coverage, "adj": adj, "hard": hard,
            "soft": soft, "score": score, "firing": firing, "tier": tier, "detonator": detonator}
