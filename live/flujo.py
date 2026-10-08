"""Radar de flujo en penny stocks (oct-2026): acciones baratas que ya se dispararon, con entradas ≫ salidas y ballenas
comprando. Es un FILTRO que avisa; no arma órdenes ni toca los ejecutores.

El perfil que busca es el de SXTC del 7-oct-2026: cerró en US$1.25 y llegó a US$7.79 (+522 %), con 29.4 M de entradas contra
18.3 M de salidas y las órdenes grandes solo del lado comprador. Entra al radar una acción cuyo cierre previo fue
≤ FLUJO_PRIOR_MAX (US$5) y que hoy ya sube ≥ FLUJO_CHG_MIN (20 %). Se clasifica en tres colores:
  VERDE     se cumple todo: entradas ÷ salidas ≥ 1.5 en la sesión y sin enfriarse en los últimos 15 min; ballenas
            compradoras que pesan más que las vendedoras; precio sobre el VWAP y a ≤ 12 % del máximo; volumen ≥ 3× lo
            normal y ≥ US$1 M negociados; ninguna oferta de acciones en la SEC en los últimos 30 días ni titular de oferta.
  AMARILLO  hay flujo comprador pero falta una pieza (el aviso dice cuál).
  ROJO      evitar: dilución, halt regulatorio, spread caro, devolvió ≥ 30 % del máximo o dominan las salidas.

QUÉ ES Y QUÉ NO ES EL «FLUJO» AQUÍ. Yahoo no da el flujo por tamaño de orden (Large / Medium / Small) que muestra tu app. Se
ESTIMA con las velas de 1 min: el dinero de cada vela se reparte entre compra y venta según cuánto subió o bajó el precio ese
minuto comparado con lo normal del día (Bulk Volume Classification, Easley · López de Prado · O'Hara, 2012), y las
«ballenas» son velas con dinero anómalo (≥ 4× la mediana de los 30 min previos y ≥ US$150 k en el minuto), no órdenes
sueltas. Las cifras absolutas no van a coincidir con las de la app; lo comparable es la razón entradas ÷ salidas y de qué
lado están las ballenas. Los umbrales son una primera estimación, sin validar con histórico: cada señal VERDE queda
registrada con su máximo y mínimo posteriores (Book) para calibrarlos con resultados reales.

Todo es función pura (sin red) salvo Book, que guarda las señales en data/live/flujo.json.
"""
from __future__ import annotations

import math
import os
import re
from datetime import datetime

import numpy as np
import pandas as pd

from scanner.util import ET, fnum, read_json, write_json

from .metrics import to_et

ON = os.environ.get("FLUJO", "1") != "0"                       # FLUJO=0 apaga el radar entero
N_MAX = int(os.environ.get("FLUJO_N", "25"))                   # acciones que se evalúan por ciclo (las demás ni se descargan)
PRIOR_MAX = float(os.environ.get("FLUJO_PRIOR_MAX", "5"))      # cierre previo máximo (US$) para ser «penny»
PX_MIN = float(os.environ.get("FLUJO_PX_MIN", "0.2"))          # debajo: polvo (spreads imposibles)
PX_MAX = float(os.environ.get("FLUJO_PX_MAX", "60"))           # tope de la pantalla de Yahoo (un penny que ya se multiplicó ×12)
CHG_MIN = float(os.environ.get("FLUJO_CHG_MIN", "20"))         # % mínimo sobre el cierre previo para entrar al radar
USD_MIN = float(os.environ.get("FLUJO_USD_MIN", "1000000"))    # dólares negociados hoy para VERDE (30 % en pre-market)
RVOL_MIN = float(os.environ.get("FLUJO_RVOL_MIN", "3"))        # volumen de hoy ÷ volumen diario normal, para VERDE
RATIO_GO = float(os.environ.get("FLUJO_RATIO", "1.5"))         # entradas ÷ salidas de la sesión, para VERDE
RATIO_MIN = 1.15                                               # debajo: ROJO (la subida no la sostienen compras)
RATIO15_MIN = float(os.environ.get("FLUJO_RATIO15", "1.2"))    # entradas ÷ salidas de los últimos RECENT min, para VERDE
RECENT = 15                                                    # minutos que cuentan como «ahora»
LARGE_K = float(os.environ.get("FLUJO_LARGE_K", "4"))          # ballena: vela con dinero ≥ K × mediana de los 30 min previos...
LARGE_USD = float(os.environ.get("FLUJO_LARGE_USD", "150000")) # ...y al menos este monto en el minuto
LARGE_RATIO = 1.5                                              # dinero de ballenas compradoras ÷ vendedoras, para VERDE
HOD_NEAR = 12.0                                                # % máximo debajo del máximo del día, para VERDE
DD_ROJO = 30.0                                                 # % devuelto desde el máximo: ROJO
MAX_SPREAD = 3.0                                               # % (ROJO si pasa)
STALE_MIN = 6                                                  # minutos sin velas nuevas: no es VERDE
MIN_BARS = 8                                                   # velas completas mínimas para medir
MAX_ALERTS = int(os.environ.get("FLUJO_ALERTS", "8"))          # avisos 🔥 por día (no saturar Telegram)
ENRICH_N = int(os.environ.get("FLUJO_ENRICH", "8"))            # a cuántas se les revisa SEC y noticias por ciclo
REG_HALTS = ("T12", "H10", "H4", "H11", "T2", "T5", "T6")      # halts regulatorios: no hay nada que operar
PEND = "falta revisar dilución (SEC y noticias)"
SYMBOL_RX = re.compile(r"[A-Z]{1,5}(\.[A-Z])?")

_ERF = np.vectorize(math.erf, otypes=[float])


def _phi(z) -> np.ndarray:
    """Distribución normal acumulada."""
    return 0.5 * (1.0 + _ERF(np.asarray(z, dtype=float) / math.sqrt(2.0)))


def _ratio(a: float, b: float) -> float | None:
    """a ÷ b con tope 99 (sin salidas ni entradas queda None)."""
    if b > 0:
        return min(round(a / b, 2), 99.0)
    return 99.0 if a > 0 else None


def _clip(x: float, a: float = 0.0, b: float = 1.0) -> float:
    return max(a, min(b, x))


def _x(v: float | None) -> str:
    return "—" if v is None else (">50×" if v >= 50 else f"{v:.1f}×")


def usd_txt(x: float) -> str:
    return f"US${x / 1e6:.1f} M" if x >= 1e6 else f"US${x / 1e3:.0f} k"


def cfg() -> dict:
    """Umbrales vigentes (la página los muestra junto a lo que falta)."""
    return {"prior_max": PRIOR_MAX, "chg_min": CHG_MIN, "ratio": RATIO_GO, "ratio15": RATIO15_MIN, "rvol": RVOL_MIN,
            "usd": USD_MIN, "hod": HOD_NEAR, "large_k": LARGE_K, "large_usd": LARGE_USD, "alerts": MAX_ALERTS}


# ---------------- candidatas ----------------
def screen_query(Q, exch):
    """Pantalla de Yahoo: acciones baratas con una subida fuerte hoy. El cierre previo se confirma después con la
    cotización; aquí solo se acota por precio actual, que ya incluye la subida."""
    return Q("and", [Q("gt", ["percentchange", 0.6 * CHG_MIN]), exch, Q("gt", ["dayvolume", 500000]),
                     Q("gte", ["intradayprice", PX_MIN]), Q("lte", ["intradayprice", PX_MAX])])


def pick(quotes: dict[str, dict], n: int | None = None, pre: bool = False) -> list[str]:
    """Candidatas de las cotizaciones de las pantallas de Yahoo: penny (cierre previo ≤ PRIOR_MAX) que ya suben fuerte.
    El umbral de subida se afloja al 60 % porque la cotización de una pantalla puede ir atrasada; el filtro de verdad lo
    hace decide() con las velas. Orden: más movimiento × más dinero negociado."""
    rows = []
    for s, q in quotes.items():
        s = str(s).upper()
        if pre:  # pre-market: Yahoo deja en regularMarket* lo de ayer; el cierre previo es el último precio regular
            px, prev = fnum(q.get("preMarketPrice")), fnum(q.get("regularMarketPrice"))
            chg, vol = (100 * (px / prev - 1) if px and prev else None), fnum(q.get("preMarketVolume"))
        else:
            px, chg = fnum(q.get("regularMarketPrice")), fnum(q.get("regularMarketChangePercent"))
            prev, vol = fnum(q.get("regularMarketPreviousClose")), fnum(q.get("regularMarketVolume"))
        if not px or px < PX_MIN or not SYMBOL_RX.fullmatch(s):
            continue
        if prev is None and chg is not None and chg > -99:
            prev = px / (1 + chg / 100)
        if not prev or prev > PRIOR_MAX:
            continue
        if chg is None:
            chg = 100 * (px / prev - 1)
        if chg < 0.6 * CHG_MIN:
            continue
        usd = px * (vol or 0)
        rows.append((math.log1p(chg) * math.log1p(usd / 1e6), s))
    rows.sort(reverse=True)
    return [s for _, s in rows[: (n or N_MAX)]]


# ---------------- métricas de las velas ----------------
def metrics(bars: pd.DataFrame | None, prev: float | None, live_px: float | None, now: datetime) -> dict | None:
    """Precio, estructura y flujo estimado de hoy (pre-market incluido) con velas de 1 min. None si no hay con qué medir.

    Flujo: a cada vela se le reparte su dinero (volumen × precio típico) entre entradas y salidas con la fracción de compra
    Φ(r / σ), con r el retorno de la vela y σ la desviación de los retornos de hoy. Ballenas: velas con dinero ≥ LARGE_K ×
    la mediana de las 30 previas y ≥ LARGE_USD; compradora si su fracción de compra es ≥ 0.6, vendedora si ≤ 0.4.
    Volatilidad a favor: varianza realizada de las velas que suben ÷ la de las que bajan."""
    d = to_et(bars)
    now = now.astimezone(ET)
    if d.empty or not prev or prev <= 0:
        return None
    d = d[d["day"] == now.date()].dropna(subset=["Open", "High", "Low", "Close"])
    if len(d) < MIN_BARS + 1:
        return None
    now_m = now.hour * 60 + now.minute
    px = float(live_px or d["Close"].iloc[-1])
    # Estructura con todas las velas; el flujo con las completas: la última de Yahoo llega a medio llenar
    vol_all = d["Volume"].to_numpy(float)
    tp_all = ((d["High"] + d["Low"] + d["Close"]) / 3.0).to_numpy(float)
    vol, usd_vol = float(vol_all.sum()), float((tp_all * vol_all).sum())
    if vol <= 0 or usd_vol <= 0:
        return None
    vwap = usd_vol / vol
    hod = max(float(d["High"].max()), px)
    closes = d["Close"]
    last15 = d.tail(15)
    m = {"px": px, "prev": float(prev), "chg": round(100 * (px / prev - 1), 2), "vwap": vwap,
         "ext": round(100 * (px / vwap - 1), 2), "hod": hod, "dd": round(100 * (1 - px / hod), 2),
         "chg5": round(100 * (px / float(closes.iloc[-6]) - 1), 2) if len(closes) > 5 else None,
         "chg15": round(100 * (px / float(closes.iloc[-16]) - 1), 2) if len(closes) > 15 else None,
         "vol": vol, "usd_vol": usd_vol, "now_m": now_m, "bars": int(len(d)),
         "age_min": max(0, now_m - int(d["m"].iloc[-1])),
         "rng1m": round(float(((last15["High"] - last15["Low"]) / last15["Close"]).median() * 100), 3) if len(last15) >= 5 else None}

    c = d.iloc[:-1]
    o, h, lo, cl, v = (c[k].to_numpy(float) for k in ("Open", "High", "Low", "Close", "Volume"))
    mm = c["m"].to_numpy(int)
    ref = np.r_[o[0], cl[:-1]]
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.log(cl / ref)
    r = np.where(np.isfinite(r), r, 0.0)
    act = v > 0
    sig = max(float(np.std(r[act])) if int(act.sum()) >= 5 else 0.0, 1e-4)
    frac = _phi(r / sig)
    usd = v * (h + lo + cl) / 3.0
    buy, sell = usd * frac, usd - usd * frac
    total = float(usd.sum())
    if total <= 0:
        return None
    med = pd.Series(usd).shift(1).rolling(30, min_periods=5).median().to_numpy()
    with np.errstate(invalid="ignore"):
        big = (med > 0) & (usd >= LARGE_K * med) & (usd >= LARGE_USD)
    lb, ls = big & (frac >= 0.6), big & (frac <= 0.4)
    rec = mm >= int(mm[-1]) - (RECENT - 1)
    tin, tout = float(buy.sum()), float(sell.sum())
    rin, rout = float(buy[rec].sum()), float(sell[rec].sum())
    lin, lout = float(buy[big].sum()), float(sell[big].sum())
    last_big = None
    if big.any():
        i = int(np.flatnonzero(big)[-1])
        last_big = {"t": f"{mm[i] // 60:02d}:{mm[i] % 60:02d}",
                    "side": "buy" if frac[i] >= 0.6 else "sell" if frac[i] <= 0.4 else "absorb",
                    "usd": round(float(usd[i])), "x": round(float(usd[i] / med[i]), 1)}
    m["flow"] = {
        "in": round(tin), "out": round(tout), "ratio": _ratio(tin, tout), "net": round(100 * (tin - tout) / total, 1),
        "in15": round(rin), "out15": round(rout), "ratio15": _ratio(rin, rout) if int(rec.sum()) >= 3 else None,
        "vfav": _ratio(float(np.sum(r[r > 0] ** 2)), float(np.sum(r[r < 0] ** 2))), "sigma": round(sig * 100, 3),
        "bars": int(len(c)),
        "large": {"n": int(big.sum()), "buy": int(lb.sum()), "sell": int(ls.sum()), "in": round(lin), "out": round(lout),
                  "ratio": _ratio(lin, lout), "share": round(100 * (lin + lout) / total, 1),
                  "rec_buy": int(lb[rec].sum()), "last": last_big}}
    return m


# ---------------- decisión ----------------
def _score(m: dict, rv: float | None, ctx: dict) -> int:
    fl, lg = m["flow"], m["flow"]["large"]
    s = 25 * _clip(((fl["ratio"] or 1.0) - 1.0) / 2.0)                       # entradas ÷ salidas: 1× → 0, 3× → 25
    s += 15 * _clip((((fl["ratio15"] if fl["ratio15"] is not None else 1.0)) - 1.0) / 1.5)  # y lo reciente: 1× → 0, 2.5× → 15
    lr = lg["ratio"]
    if lg["buy"] and lr and lr > 1:
        s += 5 + 15 * _clip((lr - 1.0) / 3.0)                                # ballenas compradoras: de 5 a 20
    if fl["vfav"]:
        s += 10 * _clip((fl["vfav"] - 1.0) / 2.0)                            # sube más de lo que baja: 1× → 0, 3× → 10
    s += 10 * _clip(math.log(max(m["chg"], CHG_MIN) / CHG_MIN) / math.log(300 / CHG_MIN))  # +20 % → 0, +300 % → 10
    if rv:
        s += 10 * _clip(math.log(max(rv, RVOL_MIN) / RVOL_MIN) / math.log(30 / RVOL_MIN))  # 3× → 0, 30× → 10
    if m["px"] >= m["vwap"]:
        s += 5
    if m["dd"] <= HOD_NEAR:
        s += 5
    if ctx.get("shelf"):
        s -= 5                                                               # estante de ofertas abierto: puede diluir cuando quiera
    sp = ctx.get("spread")
    if sp and sp > 1.5:
        s -= 5
    if (m["chg5"] or 0) < -3:
        s -= 5
    return int(round(_clip(s, 0, 100)))


def decide(t: str, m: dict, ctx: dict) -> dict | None:
    """VERDE / AMARILLO / ROJO para una acción, o None si no entra al radar (no es penny, no subió lo suficiente o casi no
    se negoció). ctx: phase ('pre'|'open'|'late'), name, spread, halted (dict|None), avg_vol, mcap, cat, offer, offer30,
    shelf, enrich_ok (False hasta revisar SEC y noticias: sin eso no hay VERDE). `pend` en la respuesta: solo falta esa
    revisión (el ciclo la pide en el acto)."""
    px, prev, chg = m["px"], m["prev"], m["chg"]
    pre = ctx.get("phase") == "pre"
    usd_min = USD_MIN * (0.3 if pre else 1.0)
    if prev > PRIOR_MAX or px < PX_MIN or chg < CHG_MIN or m["usd_vol"] < 0.2 * usd_min:
        return None
    fl, lg = m["flow"], m["flow"]["large"]
    ratio, r15 = fl["ratio"], fl["ratio15"]
    avg = ctx.get("avg_vol")
    rv = round(m["vol"] / avg, 1) if avg and avg > 0 else None
    h, sp = ctx.get("halted"), ctx.get("spread")
    why: list[str] = []
    falta: list[str] = []

    out = {"t": t, "name": ctx.get("name"), "px": px, "prev": prev, "chg": chg, "chg5": m["chg5"], "chg15": m["chg15"],
           "vwap": round(m["vwap"], 4), "ext": m["ext"], "hod": m["hod"], "dd": m["dd"], "usd": round(m["usd_vol"]),
           "vol": round(m["vol"]), "rvol": rv, "ratio": ratio, "ratio15": r15, "net": fl["net"], "in": fl["in"],
           "out": fl["out"], "in15": fl["in15"], "out15": fl["out15"], "vfav": fl["vfav"], "large": lg, "spread": sp,
           "age_min": m["age_min"], "halted": h.get("code") if h else None, "cat": ctx.get("cat"), "mcap": ctx.get("mcap"),
           "shelf": bool(ctx.get("shelf")), "pre": pre, "score": _score(m, rv, ctx), "pend": False}

    def res(decision: str, reason: str):
        out.update(decision=decision, reason=reason, falta=falta, why=why)
        return out

    # ---- ROJO: evitar ----
    if ctx.get("offer") or ctx.get("offer30"):
        return res("ROJO", f"dilución reciente ({(ctx.get('offer30') or [''])[0] or 'titular de oferta'})")
    if h and h.get("code") in REG_HALTS:
        return res("ROJO", f"halt regulatorio {h['code']}")
    if sp is not None and sp > MAX_SPREAD:
        return res("ROJO", f"spread {sp:.1f}%: te come el movimiento")
    if m["dd"] >= DD_ROJO:
        return res("ROJO", f"ya devolvió {m['dd']:.0f}% desde el máximo")
    if ratio is not None and ratio < RATIO_MIN:
        return res("ROJO", f"sin flujo comprador (entradas ÷ salidas {ratio:.2f}×): la subida no la sostienen compras")
    if r15 is not None and r15 < 0.8 and (m["ext"] < 0 or m["dd"] > 20):
        return res("ROJO", f"el flujo se dio vuelta: dominan las salidas ({_x(r15)} en 15 min)")

    # ---- lo que falta para VERDE ----
    if ratio is None or ratio < RATIO_GO:
        falta.append(f"entradas ÷ salidas {_x(ratio)} (pide ≥ {RATIO_GO:g}×)")
    if r15 is not None and r15 < RATIO15_MIN:
        falta.append(f"flujo de los últimos {RECENT} min flojo ({_x(r15)})")
    if not lg["buy"]:
        falta.append("sin ballenas compradoras")
    elif lg["out"] > 0 and lg["in"] < LARGE_RATIO * lg["out"]:
        falta.append(f"ballenas parejas (compran {usd_txt(lg['in'])} · venden {usd_txt(lg['out'])})")
    if px < m["vwap"]:
        falta.append("debajo del VWAP")
    if m["dd"] > HOD_NEAR:
        falta.append(f"a {m['dd']:.0f}% del máximo (pide ≤ {HOD_NEAR:g}%)")
    if rv is not None and rv < RVOL_MIN:
        falta.append(f"volumen {rv:.1f}× lo normal (pide ≥ {RVOL_MIN:g}×)")
    if m["usd_vol"] < usd_min:
        falta.append(f"solo {usd_txt(m['usd_vol'])} negociados (pide ≥ {usd_txt(usd_min)})")
    if h:
        falta.append(f"halt {h.get('code')} en curso: espera la reanudación")
    elif m["age_min"] > STALE_MIN:
        falta.append(f"sin operaciones hace {m['age_min']} min")
    if not ctx.get("enrich_ok", True):
        falta.append(PEND)
        out["pend"] = falta == [PEND]

    if ratio is not None and ratio >= RATIO_GO:
        why.append(f"entradas {_x(ratio)} las salidas")
    if lg["buy"]:
        why.append(f"ballenas ↑{lg['buy']} ↓{lg['sell']}")
    if fl["vfav"] and fl["vfav"] >= 1.5:
        why.append(f"sube {_x(fl['vfav'])} más de lo que baja")
    if rv is not None and rv >= RVOL_MIN:
        why.append(f"volumen {rv:.0f}× lo normal")
    if (ctx.get("cat") or {}).get("type"):
        why.append("noticia")

    if not falta:
        return res("VERDE", "flujo comprador fuerte, ballenas a favor y sin dilución a la vista")
    return res("AMARILLO", "; ".join(falta[:2]))


# ---------------- avisos y registro ----------------
def alert_text(r: dict, phase: str = "open") -> str:
    lg = r["large"]
    cat = (r.get("cat") or {}).get("title")
    pre = " · pre-market" if phase == "pre" else ""
    vol = "{:.0f}× lo normal".format(r["rvol"]) if r.get("rvol") else "alto"
    lines = [f"🔥 FLUJO {r['t']} · {r['px']:.2f} ({r['chg']:+.0f}% sobre {r['prev']:.2f}) · fuerza {r['score']}{pre}",
             f"Entradas {_x(r['ratio'])} las salidas (15 min: {_x(r['ratio15'])}) · ballenas ↑{lg['buy']} ↓{lg['sell']} "
             f"({usd_txt(lg['in'])} contra {usd_txt(lg['out'])})",
             f"Volumen {vol} · {usd_txt(r['usd'])} negociados · VWAP {r['ext']:+.0f}% · a {r['dd']:.0f}% del máximo"]
    if cat:
        lines.append(f"📰 {cat}")
    lines.append("Sin oferta de acciones en la SEC (30 días) ni titular de oferta." + (" Hay estante S-3/F-3: puede diluir." if r.get("shelf") else ""))
    lines.append("⚠ Flujo ESTIMADO con velas de 1 min (Yahoo, 1–2 min tarde): confirma Large/Medium/Small en tu app. Penny de alto "
                 "riesgo: tamaño chico y stop puesto; puede devolver todo en minutos. Solo aviso, no hay orden.")
    return "\n".join(lines)


class Book:
    """Señales de hoy (la primera vez que una acción se pone VERDE) y su seguimiento: máximo y mínimo posteriores y cierre,
    para calibrar los umbrales con resultados reales. Se guarda en data/live/flujo.json: sobrevive reinicios; un redeploy
    de Render lo borra."""

    def __init__(self, path: str | None = None, hist_days: int = 10):
        self.path = path
        self.hist_days = hist_days
        self.day: str | None = None
        self.sigs: dict[str, dict] = {}
        self.history: dict[str, list] = {}
        self.snap: dict = {"status": "sin correr", "rows": [], "counts": {}, "sigs": [], "hist": []}
        j = read_json(path, {}) if path else {}
        if isinstance(j, dict):
            self.day, self.sigs, self.history = j.get("day"), j.get("sigs") or {}, j.get("history") or {}

    def save(self):
        if self.path:
            write_json(self.path, {"day": self.day, "sigs": self.sigs, "history": self.history})

    def roll(self, day: str):
        """Cambio de día: las señales de ayer pasan al historial."""
        if self.day == day:
            return
        if self.day and self.sigs:
            self.history[self.day] = list(self.sigs.values())
            for old in sorted(self.history)[: -self.hist_days]:
                del self.history[old]
        self.day, self.sigs = day, {}
        self.save()

    def sign(self, rows: list[dict], now_m: int) -> list[dict]:
        """Registra las acciones que acaban de ponerse VERDE (una vez por ticker y día, máximo MAX_ALERTS). Devuelve las nuevas."""
        new = []
        for r in rows:
            if r["decision"] != "VERDE" or r["t"] in self.sigs or len(self.sigs) >= MAX_ALERTS:
                continue
            lg = r["large"]
            sg = {"t": r["t"], "day": self.day, "time": f"{now_m // 60:02d}:{now_m % 60:02d}", "m": now_m, "px": r["px"],
                  "prev": r["prev"], "chg": r["chg"], "score": r["score"], "ratio": r["ratio"], "ratio15": r["ratio15"],
                  "rvol": r["rvol"], "lbuy": lg["buy"], "lsell": lg["sell"], "mfe": 0.0, "mae": 0.0, "last": r["px"],
                  "close_pct": None, "status": "abierta"}
            self.sigs[r["t"]] = sg
            new.append(sg)
        if new:
            self.save()
        return new

    def follow(self, bars: dict, today) -> None:
        """Máximo y mínimo desde el aviso (sobre su precio) con las velas de hoy; nunca retroceden."""
        changed = False
        for s, sg in self.sigs.items():
            if sg.get("status") != "abierta":
                continue
            d = to_et(bars.get(s))
            if d.empty:
                continue
            after = d[(d["day"] == today) & (d["m"] > sg["m"]) & (d["m"] < 16 * 60)]
            if after.empty:
                continue
            px0 = sg["px"]
            mfe = max(sg["mfe"], round(100 * (float(after["High"].max()) / px0 - 1), 2))
            mae = min(sg["mae"], round(100 * (float(after["Low"].min()) / px0 - 1), 2))
            last = float(after["Close"].iloc[-1])
            if (mfe, mae, last) != (sg["mfe"], sg["mae"], sg["last"]):
                sg["mfe"], sg["mae"], sg["last"], changed = mfe, mae, last, True
        if changed:
            self.save()

    def finish(self, day: str) -> str | None:
        """Cierre de la sesión: lo abierto se cierra al último precio y devuelve el resumen para Telegram (None si no hubo señales)."""
        if self.day != day or not self.sigs:
            return None
        for sg in self.sigs.values():
            if sg.get("status") == "abierta":
                sg["close_pct"] = round(100 * (float(sg["last"]) / sg["px"] - 1), 2)
                sg["status"] = "cierre"
        self.save()
        lines = [f"🔥 Flujo {day}: {len(self.sigs)} aviso(s)"]
        for sg in self.sigs.values():
            lines.append(f"{sg['t']} {sg['time']} · entrada {sg['px']:.2f} · máx {sg['mfe']:+.1f}% · mín {sg['mae']:+.1f}% · "
                         f"cierre {sg['close_pct']:+.1f}%")
        lines.append("Máx y mín son contra el precio del aviso (velas de Yahoo). Sirve para calibrar los umbrales.")
        return "\n".join(lines)

    def listing(self) -> list[dict]:
        return sorted(self.sigs.values(), key=lambda x: x["m"])

    def hist_listing(self) -> list[dict]:
        return [{"day": d, "sigs": self.history[d]} for d in sorted(self.history)]
