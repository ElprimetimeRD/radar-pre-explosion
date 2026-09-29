"""Semáforo: COMPRA / ESPERA / NO COMPRES, con fuerza 0–100, razones y plan (entrada, stop, +2 %, +5 %).

Función pura: recibe métricas (live.metrics.session_metrics) y contexto; no toca la red.
Todos los umbrales están aquí arriba para ajustarlos con los resultados reales.
"""
from __future__ import annotations

from .metrics import OPEN_M, OR_MINUTES

# ---------- Umbrales ----------
MIN_PRICE = 1.0            # debajo: NO (spreads y dilución)
MIN_USD_VOL = 3_000_000    # dólares negociados hoy en sesión
MAX_SPREAD = 0.8           # % (1.5 % si el precio < 5)
RVOL_IN_PLAY = 2.0         # volumen relativo mínimo a la misma hora
EXT_MAX = 4.0              # % sobre VWAP: más que esto = esperar retroceso
EXT_PARABOLIC = 10.0       # % sobre VWAP: no perseguir
CHG15_PARABOLIC = 15.0     # % en 15 minutos: no perseguir
RISK_MAX = 2.5             # % máximo entre entrada y stop
RISK_MIN = 0.7             # stop nunca más cerca que esto (ruido)
MIN_ATR = 2.0              # % rango diario típico; debajo difícilmente da +2 %
BUY_MIN = 60               # fuerza mínima para COMPRA (70 con mercado amarillo)
LAST_ENTRY_M = 15 * 60 + 30  # 15:30 ET: sin entradas nuevas
T1, T2 = 2.0, 5.0          # objetivos %

CAT_PTS = {"fda": 20, "mna": 20, "contract": 18, "earnings": 18, "halt_news": 15, "index": 12,
           "analyst": 10, "theme": 7, "softpr": 4}
CAT_NAME = {"fda": "FDA/ensayo", "mna": "fusión/adquisición", "contract": "contrato", "earnings": "resultados",
            "halt_news": "halt por noticia", "index": "entrada a índice", "analyst": "analista",
            "theme": "tema caliente", "softpr": "PR blando"}


def _clip(x, a, b):
    return max(a, min(b, x))


def strength(m: dict, ctx: dict) -> tuple[int, list[str]]:
    """Fuerza 0–100 y lo que la explica."""
    pts, why = 0.0, []
    rv = m.get("rvol")
    if rv:
        p = 0 if rv < 1 else 10 if rv < 2 else 10 + 10 * min(rv - 2, 3) / 3 if rv < 5 else 20 + min(rv - 5, 5)
        pts += p
        if rv >= 2:
            why.append(f"RVOL {rv:.1f}×")
    cat = ctx.get("cat") or {}
    if cat.get("type") and cat.get("age") in ("fresh", "d1"):
        p = CAT_PTS.get(cat["type"], 0)
        pts += p
        if p:
            why.append(f"noticia: {CAT_NAME.get(cat['type'], cat['type'])}")
    w = m.get("whale") or {}
    net = w.get("buy", 0) - w.get("sell", 0)
    if net > 0:
        pts += min(15, 6 + 3 * net)
        why.append(f"ballena compradora ×{w['buy']}")
    px, vwap = m.get("px"), m.get("vwap")
    if px and vwap:
        if px > vwap:
            pts += 5
        if m.get("or_done") and px > m.get("orh", 1e18):
            pts += 5
        if m.get("higher_lows"):
            pts += 5
            why.append("mínimos crecientes")
        if m.get("dist_hod") is not None and m["dist_hod"] <= 1.0:
            pts += 5
    cvo = ctx.get("callVolOI")
    if cvo:
        pts += _clip((cvo - 0.3) * 10, 0, 10)
        if cvo >= 1:
            why.append(f"calls inusuales {cvo:.1f}× OI")
    rs = (m.get("chg") or 0) - (ctx.get("spy_chg") or 0)
    pts += _clip(rs / 2, 0, 10)
    if ctx.get("shelf"):
        pts -= 5
    sp = ctx.get("spread")
    if sp and sp > 0.4:
        pts -= 5
    return int(round(_clip(pts, 0, 100))), why


def _plan(entry: float, stop: float) -> dict:
    risk = 100 * (entry / stop - 1) if stop else None
    return {"entry": round(entry, 4), "stop": round(stop, 4), "t1": round(entry * (1 + T1 / 100), 4),
            "t2": round(entry * (1 + T2 / 100), 4), "risk": round(risk, 2) if risk else None,
            "rr1": round(T1 / risk, 2) if risk else None, "rr2": round(T2 / risk, 2) if risk else None}


def decide(t: str, m: dict, ctx: dict) -> dict:
    """ctx: phase ('pre','open','late','closed'), regime ('verde','amarillo','rojo'), spy_chg, cat{type,age,title},
    offer (bool), offer30 (list), shelf (bool), callVolOI, spread, halted (dict|None), atr, name, watch (bool)."""
    score, why = strength(m, ctx)
    px = m.get("px")
    out = {"t": t, "name": ctx.get("name"), "px": px, "chg": m.get("chg"), "score": score, "why": why,
           "rvol": m.get("rvol"), "ext": m.get("ext"), "vwap": m.get("vwap"), "orh": m.get("orh"),
           "hod": m.get("hod"), "atr": ctx.get("atr"), "spread": ctx.get("spread"), "whale": m.get("whale"),
           "cat": ctx.get("cat"), "watch": bool(ctx.get("watch")), "plan": None, "trigger": None}

    def res(decision, reason, trigger=None, plan=None):
        out.update(decision=decision, reason=reason, trigger=trigger, plan=plan)
        return out

    # ---- 1. Vetos duros ----
    if not px:
        return res("NO", "sin datos de precio")
    if px < MIN_PRICE:
        return res("NO", f"precio < US${MIN_PRICE:g}: spreads y dilución")
    if ctx.get("offer") or ctx.get("offer30"):
        src = (ctx.get("offer30") or [""])[0] or "titular de oferta"
        return res("NO", f"dilución reciente ({src})")
    h = ctx.get("halted")
    if h and h.get("code") in ("T12", "H10", "H4", "H11", "T2", "T5", "T6"):
        return res("NO", f"halt regulatorio {h['code']}")
    sp = ctx.get("spread")
    if sp is not None and sp > (1.5 if px < 5 else MAX_SPREAD):
        return res("NO", f"spread {sp:.2f}%: te come el objetivo")

    phase = ctx.get("phase")
    if phase == "closed":
        return res("NO", "mercado cerrado")
    if phase == "pre":
        gap = m.get("chg")
        pmv = m.get("pm_vol") or 0  # Yahoo a veces reporta 0 en pre-market: no lo exijo
        if gap is not None and gap >= 4 and (pmv == 0 or pmv * px >= 500_000):
            return res("ESPERA", f"gap pre-market {gap:+.1f}%", "se evalúa desde las 9:45 con el rango de apertura")
        return res("NO", "pre-market sin gap relevante")
    if h:
        return res("ESPERA", f"halt {h.get('code')} en curso", "espera la reanudación y el primer retroceso que aguante")

    now_m = m.get("now_m", 0)
    if now_m >= LAST_ENTRY_M or phase == "late":
        return res("NO", "después de 15:30: sin tiempo para +2 %")

    # ---- 2. ¿Está en juego? ----
    rv = m.get("rvol")
    cat_fresh = (ctx.get("cat") or {}).get("age") in ("fresh", "d1")
    if not rv or rv < (1.5 if cat_fresh else RVOL_IN_PLAY):
        return res("NO", f"sin volumen relativo (RVOL {rv:.1f}×)" if rv else "sin volumen relativo")
    if (m.get("usd_vol") or 0) < MIN_USD_VOL and now_m >= OPEN_M + OR_MINUTES:
        return res("NO", "poca liquidez en dólares")
    atr = ctx.get("atr")
    if atr is not None and atr < MIN_ATR and rv < 4:
        return res("NO", f"se mueve poco (rango diario {atr:.1f}%)")
    vwap, orh = m.get("vwap"), m.get("orh")
    if vwap and px < vwap:
        return res("NO", "debajo del VWAP: mandan los vendedores")
    ext = m.get("ext") or 0
    if ext > EXT_PARABOLIC or (m.get("chg15") or 0) > CHG15_PARABOLIC:
        return res("NO", f"parabólico (+{ext:.1f}% sobre VWAP): no persigas")

    # ---- 3. En juego pero falta algo: ESPERA ----
    if not m.get("or_done"):
        return res("ESPERA", "formando el rango de apertura", f"compra si rompe {orh:.2f} después de las 9:45")
    if ext > EXT_MAX:
        pull = vwap * 1.01
        return res("ESPERA", f"extendido +{ext:.1f}% sobre VWAP", f"compra si retrocede hacia {pull:.2f} y rebota")
    if orh and px <= orh:
        return res("ESPERA", "debajo del máximo de apertura", f"compra si rompe {orh:.2f} con volumen",
                   _plan(orh * 1.001, max(vwap, m.get("swing_low") or 0) * 0.999))
    w = m.get("whale") or {}
    if w.get("sell", 0) > w.get("buy", 0):
        return res("ESPERA", "ballenas vendiendo en los últimos 15 min", "espera que el precio aguante sobre el VWAP")
    reg = ctx.get("regime")
    if reg == "rojo":
        return res("ESPERA", "mercado en rojo (SPY/QQQ bajo VWAP)", "espera que el mercado recupere")
    impulse = m.get("higher_lows") or w.get("buy", 0) > 0 or (m.get("accel") or 0) >= 2
    if not impulse:
        return res("ESPERA", "sin impulso ahora", f"compra si hace nuevo máximo sobre {m.get('hod', px):.2f} con volumen")
    stop = max(vwap, m.get("swing_low") or 0) * 0.999
    stop = min(stop, px * (1 - RISK_MIN / 100))
    risk = 100 * (px / stop - 1)
    if risk > RISK_MAX:
        return res("ESPERA", f"stop lejos ({risk:.1f}%)", f"espera retroceso cerca de {px * (1 - (risk - RISK_MAX) / 100):.2f}")
    need = BUY_MIN + (10 if reg == "amarillo" else 0)
    if score < need:
        return res("ESPERA", f"fuerza {score} < {need}", "falta confirmación: volumen, noticia o ballena")
    return res("COMPRA", "en juego, sobre VWAP y rompiendo con impulso", None, _plan(px, stop))


def regime(spy: dict, qqq: dict) -> tuple[str, str]:
    """Semáforo del mercado a partir de las métricas de SPY y QQQ."""
    def weak(x):
        return x.get("px") and x.get("vwap") and x["px"] < x["vwap"]
    s_chg, q_chg = spy.get("chg") or 0, qqq.get("chg") or 0
    if (weak(spy) and weak(qqq)) and min(s_chg, q_chg) <= -0.8:
        return "rojo", f"SPY {s_chg:+.1f}% y QQQ {q_chg:+.1f}% bajo VWAP"
    if weak(spy) or weak(qqq) or min(s_chg, q_chg) <= -0.5:
        return "amarillo", f"SPY {s_chg:+.1f}% · QQQ {q_chg:+.1f}%"
    return "verde", f"SPY {s_chg:+.1f}% · QQQ {q_chg:+.1f}%"
