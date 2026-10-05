"""Estrategia de Claude para el ejecutor paralelo (bridge/ejecutor_claude.py): «pullback con tendencia» en los líderes
del día. Solo compras. Lógica pura, sin red ni IBKR: la prueban tests/test_claude_estrategia.py y la repasa
tools/backtest_claude.py.

La idea, en una frase: comprar la pausa de una acción fuerte, sin perseguirla.
  1. Líder: sube bastante sobre el cierre de ayer (más cuanto más se mueve normalmente), con volumen por encima de lo
     habitual a esta hora, y cotiza sobre su VWAP. El mercado (SPY) no puede estar en pánico (−0.5 % o peor).
  2. Pullback sano: desde el máximo del día retrocedió entre el 25 % y el 60 % del impulso, sin romper el VWAP, con
     menos volumen que el impulso y ya con dos velas sin hacer mínimo nuevo (la caída se detuvo).
  3. Entrada: compra LÍMITE un poco por encima del mínimo del retroceso (no corre tras el precio: si la acción sigue
     subiendo sin volver, no compra).
  4. Salida: stop fijo (nativo en IBKR) bajo el mínimo del retroceso, con una distancia mínima del 0.6 %, y objetivo de 2R
     (R = distancia al stop). Lo que quede a las 15:55 ET se vende a mercado.
Es la antítesis operativa del semáforo: él compra rupturas (fuerza) con un stop que sube; esta compra debilidad dentro de
la fuerza, con orden límite y stop fijo, durante toda la sesión. Funciona con velas de Yahoo con 1–2 min de retraso
porque la entrada es una orden que ya espera en IBKR y las salidas son órdenes nativas (no dependen de ver el precio).

Velas: tuplas (m, o, h, l, c, v) con m = minuto del día en hora de Nueva York (570 = 9:30), solo velas COMPLETAS.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

OPEN_M = 9 * 60 + 30
CLOSE_M = 16 * 60


@dataclass(frozen=True)
class Cfg:
    ini_m: int = 9 * 60 + 50       # primera entrada: 20 min tras la apertura (ya hay estructura)
    fin_m: int = 15 * 60 + 15      # última entrada: deja tiempo al objetivo antes del cierre de las 15:55
    min_precio: float = 5.0
    max_precio: float = 800.0
    chg_min: float = 0.015         # sube al menos 1.5 % sobre el cierre de ayer...
    chg_atr: float = 0.45          # ...y al menos 0.45 × su ATR diario (una acción nerviosa necesita subir más)
    chg_max: float = 0.30          # más de +30 % es parabólica: no se persigue
    rvol_min: float = 1.3          # volumen acumulado / el habitual a esta hora
    leg_min: float = 0.012         # el impulso (mínimo previo → máximo del día) mide al menos 1.2 %...
    leg_atr: float = 0.35          # ...y 0.35 × ATR
    leg_ventana: int = 60          # velas antes del máximo en las que se busca el inicio del impulso
    retr_min: float = 0.25         # el cierre actual retrocedió entre el 25 %...
    retr_max: float = 0.60         # ...y el 60 % del impulso
    retr_prof: float = 0.70        # y el mínimo del retroceso nunca pasó del 70 %
    pb_min_velas: int = 4          # el retroceso lleva al menos 4 velas...
    pb_max_velas: int = 60         # ...y no más de 60 (si no, ya es otra cosa)
    pausa_velas: int = 2           # velas seguidas sin mínimo nuevo del retroceso
    vwap_tol: float = 0.003        # el retroceso no rompe el VWAP más de 0.3 %
    vol_pb: float = 0.9            # volumen medio del retroceso ≤ 90 % del del impulso
    entrada_f: float = 0.30        # la compra límite va al 30 % del camino entre el mínimo del retroceso y el último cierre
    stop_buf: float = 0.0015       # el stop va 0.15 % bajo el mínimo del retroceso...
    riesgo_min: float = 0.006      # ...pero nunca a menos de 0.6 % de la entrada (el ruido sacaría el stop)
    r_obj: float = 2.0             # objetivo = entrada + 2R
    hod_min_r: float = 1.5         # el máximo del día tiene que quedar al menos a 1.5R de la entrada
    spy_min: float = -0.005        # no se compra si SPY cae más de 0.5 % en el día
    ttl_s: int = 600               # la compra espera 10 min; si no se llena, se cancela
    orden_usd: float = 500.0
    riesgo_max_usd: float = 15.0   # pérdida máxima por operación (por si el precio es raro)
    vela_vieja_s: int = 240        # si la última vela completa tiene más de 4 min, no hay datos fiables


CFG = Cfg()

# fracción del volumen del día que suele estar hecha a cada hora (curva en U de la bolsa de EE. UU.)
_CURVA = [(570, 0.0), (575, 0.05), (585, 0.10), (600, 0.145), (630, 0.22), (660, 0.29), (720, 0.40), (780, 0.49),
          (840, 0.58), (900, 0.71), (930, 0.82), (960, 1.0)]


def frac_vol(m: float) -> float:
    if m <= _CURVA[0][0]:
        return 0.0
    for (m0, f0), (m1, f1) in zip(_CURVA, _CURVA[1:]):
        if m <= m1:
            return f0 + (f1 - f0) * (m - m0) / (m1 - m0)
    return 1.0


def num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def limpiar(bars) -> list[tuple]:
    """Velas válidas (todos los precios son números y positivos), ordenadas, sin repetidas y solo de la sesión."""
    out, vistos = [], set()
    for b in bars or []:
        try:
            m, o, h, l, c, v = b
        except (TypeError, ValueError):
            continue
        o, h, l, c = num(o), num(h), num(l), num(c)
        if None in (o, h, l, c) or min(o, h, l, c) <= 0 or h < l:
            continue
        m = int(m)
        if m in vistos or not OPEN_M <= m < CLOSE_M:
            continue
        vistos.add(m)
        out.append((m, o, h, l, c, max(0.0, num(v) or 0.0)))
    out.sort()
    return out


def vwap(bars) -> float | None:
    pv = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in bars)
    vol = sum(b[5] for b in bars)
    return pv / vol if vol > 0 else None


def regimen(spy_bars, spy_prev: float | None, cfg: Cfg = CFG) -> tuple[bool, str]:
    """No se compran retrocesos en un día de pánico: el mercado «no está en contra» si SPY no cae más de 0.5 % sobre el
    cierre de ayer. (Exigir además que SPY esté sobre su VWAP dejaba fuera la mitad de las jugadas en el repaso
    histórico, y justo las que mejor salían: se dejó el filtro suave.)"""
    bars = limpiar(spy_bars)
    if len(bars) < 5 or not spy_prev:
        return False, "sin datos de SPY"
    chg = bars[-1][4] / spy_prev - 1
    return chg >= cfg.spy_min, f"SPY {chg * 100:+.2f}% en el día"


def evaluar(t: str, bars, prev_close, adv, atr_pct, cfg: Cfg = CFG) -> tuple[dict | None, str]:
    """¿Hay jugada ahora en `t`? Devuelve (jugada, motivo): jugada es None si no la hay y el motivo dice por qué."""
    bars = limpiar(bars)
    n = len(bars)
    prev_close, adv, atr_pct = num(prev_close), num(adv), num(atr_pct)
    if n < 15:
        return None, "pocas velas"
    if not prev_close or prev_close <= 0:
        return None, "sin cierre previo"
    if not adv or adv <= 0:
        return None, "sin volumen habitual"
    atr_pct = atr_pct if atr_pct and atr_pct > 0 else 0.03
    last = bars[-1]
    m_ahora = last[0] + 1
    if not cfg.ini_m <= m_ahora < cfg.fin_m:
        return None, "fuera del horario de entradas"
    px = last[4]
    if not cfg.min_precio <= px <= cfg.max_precio:
        return None, "precio fuera de rango"
    chg = px / prev_close - 1
    if chg < max(cfg.chg_min, cfg.chg_atr * atr_pct):
        return None, f"no es líder ({chg * 100:+.1f}%)"
    if chg > cfg.chg_max:
        return None, "parabólica"
    vw = vwap(bars)
    if not vw:
        return None, "sin volumen"
    rvol = sum(b[5] for b in bars) / (adv * max(frac_vol(m_ahora), 0.02))
    if rvol < cfg.rvol_min:
        return None, f"volumen bajo (RVOL {rvol:.1f})"
    if px < vw:
        return None, "bajo el VWAP"
    # --- el impulso y el retroceso ---
    hod = max(b[2] for b in bars)
    hod_i = next(i for i, b in enumerate(bars) if b[2] == hod)
    pb = bars[hod_i + 1:]
    if not cfg.pb_min_velas <= len(pb) <= cfg.pb_max_velas:
        return None, "sin retroceso en curso"
    ini = max(0, hod_i - cfg.leg_ventana)
    seg = bars[ini:hod_i + 1]
    leg_lo = min(b[3] for b in seg)
    leg_i = next(i for i, b in enumerate(seg) if b[3] == leg_lo)
    leg = hod - leg_lo
    if leg / leg_lo < max(cfg.leg_min, cfg.leg_atr * atr_pct):
        return None, "impulso chico"
    pb_lo = min(b[3] for b in pb)
    pb_lo_i = next(i for i, b in enumerate(pb) if b[3] == pb_lo)
    retr = (hod - px) / leg
    retr_max = (hod - pb_lo) / leg
    if not cfg.retr_min <= retr <= cfg.retr_max:
        return None, f"retroceso {retr * 100:.0f}% fuera de rango"
    if retr_max > cfg.retr_prof:
        return None, "retroceso muy profundo"
    if pb_lo < vw * (1 - cfg.vwap_tol):
        return None, "rompió el VWAP"
    if len(pb) - 1 - pb_lo_i < cfg.pausa_velas:
        return None, "sigue haciendo mínimos"
    vol_leg = sum(b[5] for b in seg[leg_i:]) / max(1, len(seg) - leg_i)
    vol_pb = sum(b[5] for b in pb) / len(pb)
    if vol_leg > 0 and vol_pb > cfg.vol_pb * vol_leg:
        return None, "el retroceso trae mucho volumen"
    # --- la orden ---
    entrada = round(pb_lo + cfg.entrada_f * (px - pb_lo), 2)
    stop = round(min(pb_lo * (1 - cfg.stop_buf), entrada * (1 - cfg.riesgo_min)), 2)
    r = round(entrada - stop, 2)
    if r <= 0 or entrada <= stop:
        return None, "stop inválido"
    if hod < entrada + cfg.hod_min_r * r:
        return None, "el máximo del día queda muy cerca"
    objetivo = round(entrada + cfg.r_obj * r, 2)
    qty = int(cfg.orden_usd // entrada)
    if qty < 1:
        return None, "no alcanza para 1 acción"
    if qty * r > cfg.riesgo_max_usd:
        qty = int(cfg.riesgo_max_usd // r)
        if qty < 1:
            return None, "riesgo muy grande"
    score = rvol * min(chg / atr_pct, 3.0)
    texto = (f"pullback {retr * 100:.0f}% · {'+' if chg >= 0 else ''}{chg * 100:.1f}% en el día · RVOL {rvol:.1f} · "
             f"sobre VWAP {vw:.2f}")
    return {"t": t, "tipo": "lmt", "limite": entrada, "stop": stop, "objetivo": objetivo, "trail": r, "qty": qty,
            "riesgo": round(qty * r, 2), "rvol": round(rvol, 2), "chg": round(chg, 4), "retr": round(retr, 3),
            "score": round(score, 2), "texto": texto, "hod": hod, "pb_lo": pb_lo, "vwap": round(vw, 2),
            "m": last[0]}, "jugada"


def buscar(datos: dict, spy: tuple[bool, str], cfg: Cfg = CFG) -> tuple[list[dict], dict]:
    """datos: {t: {"bars": [...], "prev": cierre previo, "adv": volumen medio, "atr": ATR %}}. Devuelve las jugadas
    ordenadas de mejor a peor y un diccionario {t: motivo} de las que se descartaron (para el registro)."""
    ok, _ = spy
    out, motivos = [], {}
    for t, d in datos.items():
        j, motivo = evaluar(t, d.get("bars"), d.get("prev"), d.get("adv"), d.get("atr"), cfg)
        if j and not ok:
            motivos[t] = "el mercado no está a favor"
        elif j:
            out.append(j)
        else:
            motivos[t] = motivo
    out.sort(key=lambda j: -j["score"])
    return out, motivos


def con(cfg: Cfg = CFG, **kw) -> Cfg:
    return replace(cfg, **kw)
