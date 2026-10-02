"""Bucle en vivo: universo dinámico → velas de 1 min → métricas → semáforo → alertas y seguimiento.

Corre en un hilo dentro del servicio web (live/app.py). Cada ciclo deja una foto completa en self.snapshot.
"""
from __future__ import annotations

import math
import os
import re
import threading
import time
import traceback
from datetime import datetime, timezone

import requests

from scanner.features import RULES, options_features
from scanner.sources import other, yahoo
from scanner.util import DATA, ET, fnum, log, read_json, write_json

from . import halts as halts_src
from . import memory
from .bridge import Bridge
from .decide import CAT_NAME, ENTRY_END_M, LAST_ENTRY_M, LIMIT_VALID_MIN as LIMIT_MIN, RISK_MAX, T2, decide, regime
from .decide import RVOL_IN_PLAY as D_RVOL_IN_PLAY
from .metrics import OPEN_M, OR_MINUTES, atr_pct, baseline_curve, rows_frame, session_metrics, to_et, vol_scale
from .positions import Positions

UNIVERSE_N = int(os.environ.get("UNIVERSE_N", "50"))
CYCLE_S = int(os.environ.get("CYCLE_S", "60"))
UNIVERSE_TTL = int(os.environ.get("UNIVERSE_TTL", "180"))  # s entre refrescos del universo en sesión (pre-market: 600)
FAST_S = int(os.environ.get("FAST_S", "15"))         # s entre vistazos del vigía rápido a las acciones armadas
ARM_MIN = int(os.environ.get("ARM_MIN", "55"))        # fuerza mínima para armar (60 con mercado amarillo): la ruptura suma 5
ARM_NEAR = float(os.environ.get("ARM_NEAR", "1.0"))   # % máximo debajo del gatillo para armarla
ARM_MAX = int(os.environ.get("ARM_MAX", "8"))         # avisos de "arma" por día (no saturar Telegram)
IBKR_TOP = int(os.environ.get("IBKR_TOP", "12"))     # tickers de los escáneres de IBKR (puente) que entran al universo
# Velas de 1 min de IBKR (al día cada ~5 s) para las N candidatas principales: con ellas el semáforo avisa la COMPRA
# por Telegram antes que con las de Yahoo. 0 = apagado. Requiere los datos de IBKR por la API funcionando (sin 10089).
IBKR_BARS = int(os.environ.get("IBKR_BARS", "0"))
ENRICH_N = 20
CTX_RETRY_S = 300     # s antes de volver a pedir la curva de volumen o el cierre previo de una acción que falló
PARTIAL_BARS = 2      # las últimas velas de Yahoo pueden estar a medio llenar: se vuelven a revisar en el ciclo siguiente
HALTS_S = 20          # s entre lecturas de los halts de NASDAQ (en segundo plano)
NEWS_TTL, NEWS_TTL_HOT = 600, 180  # s de caché de titulares; las armadas y las que están cerca del gatillo, más seguido
REPLAY_MIN_S = 600    # s mínimos entre dos replays recalculados (compite con el ciclo por CPU y memoria)
Q_BACKOFF = (60, 120, 300, 1200)  # s sin pedir cotizaciones v7 tras 1, 2, 3 y 4+ fallos seguidos (antes 20 min al primero)
STATE_DIR = os.path.join(DATA, "live")
HIST_DAYS = 5
DEFAULT_WATCH = ["MU", "SNDK", "MRVL", "ARM", "BE", "AXTI", "NVDA", "AMD", "SNXX", "MUU"]
EARN_RX = r"(earnings|quarterly results|Q[1-4] (results|revenue)|beats?|tops? (estimates|expectations)|raises?\b.{0,40}\b(guidance|outlook|forecast)|record revenue)"
# Palabras de nombre que no identifican a una empresa (prefijos: también cubren nombres truncados como "Corporati")
GENERIC = ("united", "american", "first", "general", "national", "internation", "global", "holding", "group",
           "corp", "company", "incorp", "limited", "trust", "fund", "technolog", "system", "solution", "industr",
           "partners", "capital", "financial", "bancorp", "resource", "pharmaceut", "therapeut", "acquisition",
           "daily", "shares", "leverage", "etf", "long", "short", "bull", "bear", "inverse", "ultra", "tradr",
           "direxion", "proshares", "graniteshares", "defiance")


def phase_of(now: datetime) -> str:
    t = now.astimezone(ET)
    if t.weekday() >= 5:
        return "closed"
    m = t.hour * 60 + t.minute
    if 4 * 60 <= m < OPEN_M:
        return "pre"
    if OPEN_M <= m < LAST_ENTRY_M:
        return "open"
    if LAST_ENTRY_M <= m < 16 * 60:
        return "late"
    return "closed"


def load_watchlist() -> list[str]:
    path = os.path.join(os.path.dirname(DATA), "watchlist.txt")
    try:
        with open(path, encoding="utf-8") as f:
            w = [ln.strip().upper() for ln in f if ln.strip() and not ln.startswith("#")]
    except OSError:
        w = []
    extra = [x.strip().upper() for x in os.environ.get("WATCHLIST", "").split(",") if x.strip()]
    return list(dict.fromkeys(w + extra)) or DEFAULT_WATCH


def name_keys(name: str | None) -> list[str]:
    """Palabras distintivas del nombre de la empresa ('Accenture plc' → ['Accenture']). Si todas son genéricas
    ('United Therapeutics Corporati…'), usa las dos primeras juntas como frase."""
    words = [w.strip("'-&") for w in re.split(r"[^A-Za-z0-9&'-]+", name or "")]
    words = [w for w in words if w]
    keys = [w for w in words if len(w) >= 4 and not w.lower().startswith(GENERIC)]
    if keys:
        return keys[:2]
    return [" ".join(words[:2])] if len(words) >= 2 else words[:1]


def relevant(title: str, symbol: str | None, name: str | None) -> bool:
    """¿El titular habla de esta empresa? Evita que un resumen genérico ('top analyst calls') cuente como su noticia."""
    if not symbol or not name:
        return True  # sin nombre no hay con qué comparar: no filtrar
    if re.search(rf"(?<![A-Za-z]){re.escape(symbol)}(?![A-Za-z])", title):
        return True
    return any(k and re.search(rf"(?<![A-Za-z]){re.escape(k)}(?![A-Za-z])", title, re.I) for k in name_keys(name))


def sane_spread(bid, ask, px, rng1m=None) -> float | None:
    """Spread % solo si es creíble. Yahoo a veces deja bid/ask viejos (MRVL o AMD con 10 % en plena sesión):
    si el spread no cuadra con el último precio o es mucho más ancho que lo que de verdad opera en 1 min, se ignora."""
    if not (bid and ask and ask > bid):
        return None
    sp = 100 * (ask - bid) / ((ask + bid) / 2)
    if px and (bid > px * 1.02 or ask < px * 0.98):
        return None
    if rng1m is not None and sp > max(1.0, 4 * rng1m):
        return None
    return round(sp, 2)


def classify(items: list[dict], now_ts: float, symbol: str | None = None, name: str | None = None) -> dict:
    """Mejor catalizador de las últimas ~2 semanas + bandera de oferta. Con symbol/name, solo cuentan los titulares
    que mencionan el ticker o el nombre de la empresa."""
    prio = ["fda", "mna", "contract", "earnings", "index", "analyst", "theme", "softpr"]
    rules = dict(RULES)
    rules["earnings"] = EARN_RX
    out, best = {"offer": False, "n": len(items)}, None
    for it in items[:10]:
        ts = it.get("ts")
        if not ts:
            continue
        age_h = (now_ts - ts) / 3600
        if age_h > 24 * 14:
            continue
        title = it["title"]
        if not relevant(title, symbol, name):
            continue
        if re.search(rules["offer"], title, re.I) and age_h <= 24 * 5:
            out["offer"] = True
            out["offerTitle"] = title
        for k in prio:
            if re.search(rules[k], title, re.I):
                r = prio.index(k)
                if best is None or r < best[0] or (r == best[0] and age_h < best[1]):
                    best = (r, age_h, k, title, it.get("url"))
                break
    if best:
        _, age_h, k, title, url = best
        out.update(type=k, age="fresh" if age_h < 18 else "d1" if age_h < 36 else "old",
                   hours=round(age_h, 1), title=title, url=url)
    return out


def _rss(url: str, params: dict, src: str) -> list[dict]:
    import xml.etree.ElementTree as ETX
    from email.utils import parsedate_to_datetime
    try:
        r = requests.get(url, params=params, headers={"User-Agent": halts_src.UA}, timeout=10)
        root = ETX.fromstring(r.content)
    except (requests.RequestException, ETX.ParseError):
        return []
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate") or "").timestamp()
        except (TypeError, ValueError):
            ts = None
        if title:
            out.append({"title": title, "ts": ts, "url": it.findtext("link"), "src": src})
    out.sort(key=lambda x: x["ts"] or 0, reverse=True)
    return out[:10]


def rss_news(symbol: str, name: str | None = None) -> list[dict]:
    """Titulares sin crumb: RSS de Yahoo y, si Yahoo bloquea la IP (429), Google News filtrado por ticker o nombre."""
    items = _rss("https://feeds.finance.yahoo.com/rss/2.0/headline",
                 {"s": symbol, "region": "US", "lang": "en-US"}, "Yahoo RSS")
    if items:
        return items
    items = _rss("https://news.google.com/rss/search",
                 {"q": f"{symbol} stock when:2d", "hl": "en-US", "gl": "US", "ceid": "US:en"}, "Google News")
    word = next((w for w in re.split(r"\W+", name or "") if len(w) >= 4), None)
    keep = []
    for x in items:
        t = x["title"]
        if re.search(rf"\b{re.escape(symbol)}\b", t) or (word and word.lower() in t.lower()):
            keep.append(x)
    return keep


def advance(tr: dict, bars) -> list[str]:
    """Avanza una señal con velas nuevas (DataFrame con m, Open, High, Low). Devuelve eventos:
    fill, expired, stop, t1, t2, be (y en compras stop: invalid, gap). Estados: pendiente → abierta → t1 → t2 |
    t1-cerrada | stop | no ejecutada | cancelada.
    order='stop' (ruptura armada): se activa cuando el precio SUBE hasta la entrada; si antes pierde el stop, la jugada
    se cancela; si abre por encima del límite (cap) y no vuelve a él, no se llena (no se persigue).
    Las últimas velas pueden llegar de nuevo, ya completas, en el ciclo siguiente: las anteriores a la entrada no
    cuentan para la posición y la vuelta a la entrada solo cuenta en velas posteriores a la del +2 %."""
    ev = []
    if tr.get("hit1") and tr.get("t1_m") is None:
        tr["t1_m"] = tr.get("m", -1)  # señal guardada antes de existir t1_m: todo hasta su cursor ya se revisó
    for _, b in bars.iterrows():
        hi, lo, m = float(b["High"]), float(b["Low"]), int(b["m"])
        if tr["status"] == "pendiente":
            if m > (tr.get("valid_m") or 10**9):
                tr["status"] = "no ejecutada"
                ev.append("expired")
                break
            if tr.get("order") == "stop":
                if hi < tr["entry"]:
                    if lo <= tr["stop"]:
                        tr["status"] = "cancelada"
                        ev.append("invalid")
                        break
                    continue
                op, cap = float(b["Open"]), tr.get("cap") or float("inf")
                if op > cap and lo > cap:
                    tr["status"] = "no ejecutada"
                    ev.append("gap")
                    break
                tr["fill"] = round(min(max(tr["entry"], op), cap), 4)
            elif lo > tr["entry"]:
                continue
            tr["status"], tr["fill_m"] = "abierta", m
            ev.append("fill")
        if tr["status"] not in ("abierta", "t1"):
            break
        if tr.get("fill_m") is not None and m < tr["fill_m"]:
            continue  # vela repasada de antes de llenarse la orden: no es de la posición
        # En la vela en que se llena una límite (retesteo desde arriba) su máximo suele ser de ANTES de llenarse: no
        # cuenta para el +2 % ni para el máximo (antes podía avisar "✅ tocó +2 %" en el mismo minuto de la entrada)
        lim_fill = bool(tr.get("limit")) and m == tr.get("fill_m")
        if not lim_fill:
            tr["mfe"] = max(tr["mfe"], round(100 * (hi / tr["entry"] - 1), 2))
        tr["mae"] = min(tr["mae"], round(100 * (lo / tr["entry"] - 1), 2))
        if not tr["hit1"] and lo <= tr["stop"]:
            tr["status"] = "stop"
            ev.append("stop")
            break
        if lim_fill:
            continue
        if not tr["hit1"] and hi >= tr["t1"]:
            tr["hit1"], tr["status"], tr["t1_m"] = True, "t1", m
            ev.append("t1")
        if tr["hit1"] and hi >= tr["t2"]:
            tr["status"] = "t2"
            ev.append("t2")
            break
        if tr["hit1"] and lo <= tr["entry"] and m > tr.get("t1_m", -1):
            tr["status"] = "t1-cerrada"
            ev.append("be")
            break
    return ev


def new_trade(r: dict, day: str, t_str: str, m: int) -> dict:
    """m: minuto del aviso. El seguimiento empieza en la vela siguiente: las anteriores (y la del propio minuto, que
    mezcla segundos de antes del aviso) son de antes de la orden y darían stops, llenados o cancelaciones falsos."""
    p = r["plan"]
    lim = bool(p.get("limit"))
    return {"t": r["t"], "day": day, "time": t_str, "m": m, "entry": p["entry"], "stop": p["stop"], "t1": p["t1"],
            "t2": p["t2"], "risk": p.get("risk"), "score": r["score"], "limit": lim,
            "valid_m": m + (p.get("valid_min") or 10) if lim else None,
            "status": "pendiente" if lim else "abierta", "hit1": False, "mfe": 0.0, "mae": 0.0, "last": p["entry"]}


def buy_text(r: dict, tr: dict, reg_txt: str, head: str = "🟢 COMPRA", tail: str = "") -> str:
    """Texto del aviso de COMPRA (el del ciclo y el temprano con velas de IBKR): orden, fuerza, stop y objetivos."""
    why = ", ".join(r["why"][:3])
    if tr["limit"]:
        vm = tr["valid_m"]
        how = f"orden LÍMITE {tr['entry']:.2f} válida hasta {vm // 60}:{vm % 60:02d} ET (no persigas {r['px']:.2f})"
    else:
        how = f"a {tr['entry']:.2f}, no pagues más de {tr['entry'] * 1.003:.2f}"
    return (f"{head} {r['t']} {how}\nFuerza {r['score']}/100 · {why}\n"
            f"Stop {tr['stop']:.2f} (−{tr['risk']:.1f}%) · +2%: {tr['t1']:.2f} · +5%: {tr['t2']:.2f}\nMercado: {reg_txt}{tail}")


def new_arm(t: str, a: dict, day: str | None, t_str: str, m: int, valid_m: int) -> dict:
    """La compra stop que pide el aviso 🟡 ARMA, como orden virtual: así se mide si entrar en la ruptura (rápido)
    rinde mejor que esperar la COMPRA confirmada. Se sigue desde la vela posterior al aviso (ver new_trade)."""
    e = a["entry"]
    return {"t": t, "day": day, "time": t_str, "m": m, "kind": "armada", "order": "stop", "level": a["level"],
            "entry": e, "cap": round(e * 1.003, 4), "stop": a["stop"], "t1": a["t1"],
            "t2": a.get("t2") or round(e * (1 + T2 / 100), 4), "risk": a["risk"], "score": a["score"], "limit": False,
            "valid_m": valid_m, "status": "pendiente", "hit1": False, "mfe": 0.0, "mae": 0.0, "last": a.get("px") or e}


def break_lag(df, level: float | None, t_et: datetime) -> int | None:
    """Minutos entre la primera vela de 1 min que superó `level` (en los últimos 30 min) y ahora: cuánto tardó la
    señal en llegar después de la ruptura. None si las velas (1–2 min de retraso) aún no muestran el cruce."""
    if not level:
        return None
    d = to_et(df)
    if d.empty:
        return None
    now_m = t_et.hour * 60 + t_et.minute
    lo_m = max(OPEN_M + OR_MINUTES, now_m - 30)
    w = d[(d["day"] == t_et.date()) & (d["m"] >= lo_m) & (d["m"] <= now_m)]
    hit = w[w["High"] > level * 1.0005]
    return None if hit.empty else int(now_m - int(hit["m"].iloc[0]))


def _avg(xs: list, nd: int = 1):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), nd) if xs else None


FILLED = ("abierta", "t1", "t2", "stop", "t1-cerrada", "cierre")


LOG_BASE = os.environ.get("LOG_BASE", "https://raw.githubusercontent.com/ElprimetimeRD/radar-pre-explosion/main/data/live")


MISS = object()  # TTLCache.get sin dato vigente (un None guardado también es un dato: "esa acción no tiene opciones")


class TTLCache:
    def __init__(self):
        self.d: dict = {}

    def get(self, key, ttl, default=None):
        v = self.d.get(key)
        return v[1] if v and time.time() - v[0] < ttl else default

    def put(self, key, val):
        self.d[key] = (time.time(), val)
        return val


class Radar:
    def __init__(self, notify=True):
        self.notify = notify
        self.lock = threading.Lock()
        self.cache = TTLCache()
        self.universe: list[str] = []
        self.sources: dict[str, list[str]] = {}
        self.universe_ts = 0.0
        self.day = None
        self.baseline: dict[str, list[float] | None] = {}
        self.atr: dict[str, float | None] = {}
        self.prev: dict[str, float | None] = {}
        self.q_off_until = 0.0
        self.sec_map: dict[str, int] = {}
        self.trades: dict[str, dict] = {}
        self.arms: dict[str, dict] = {}   # compras stop de los avisos ARMA, seguidas como órdenes virtuales
        self.positions = Positions()  # posiciones reales que Priamo registra al ejecutar (avisos de caída y objetivo)
        self.sent: set[str] = set()
        self.sent_day: str | None = None  # día al que pertenecen los avisos enviados (se vacían al cambiar de día)
        self.history: dict[str, list] = {}  # días anteriores (hasta HIST_DAYS) para que GitHub los guarde aunque se salte corridas
        self.arm_history: dict[str, list] = {}
        self.snapshot: dict = {"status": "iniciando", "rows": []}
        self.last_bars: dict = {}
        self.replay_state: dict = {"status": "sin correr"}
        self.errors: list[str] = []
        self.names: dict[str, str] = {}   # nombre de cada ticker (de las pantallas), para filtrar titulares ajenos
        self.armed: dict[str, dict] = {}  # rupturas armadas: {ticker: nivel, entrada, stop…} que vigila el vigía rápido
        self.fast_state: dict = {}
        self.mem: dict = {}               # memoria del proceso tras el último ciclo (RSS y % de MEM_LIMIT_MB)
        self.wake = threading.Event()     # el vigía rápido despierta el ciclo completo cuando algo rompe
        self.bridge = Bridge()            # puente IBKR (programa en la PC de Priamo): escáneres y precios al instante
        self.breaks: dict[str, dict] = {}  # hora y fuente de cada ruptura avisada (el ciclo la pasa a la armada)
        self.brk_lock = threading.Lock()   # el puente (petición web) y el vigía rápido revisan rupturas a la vez
        self.base_universe: list[str] | None = None  # universo del último refresco, sin los agregados de IBKR
        self.bar_want: list[str] = []      # candidatas que el puente sigue con velas de IBKR (IBKR_BARS)
        self.ctx_cache: dict[str, dict] = {}  # contexto del último ciclo por ticker (noticias, mercado, spread, ATR…)
        self.early: dict[str, dict] = {}   # avisos tempranos de COMPRA con velas de IBKR: {ticker: hora y fuente}
        self.vol_k: dict[str, float] = {}  # factor de volumen IBKR→Yahoo por ticker (se recalcula con cada ciclo)
        self.reg_txt = ""
        self.tg_lock = threading.Lock()
        self.tg_state: dict = {}          # resultado del último envío a Telegram (para /health; sin el token)
        self.ctx_try: dict[str, float] = {}  # última vez que se pidió el contexto (5 días + diario) de cada acción
        self.q_fail = 0                   # fallos seguidos de las cotizaciones v7 (el descanso crece con ellos)
        # Segundo plano (hilo "contexto"): halts, refresco del universo y noticias/SEC/opciones, fuera del ciclo de 1 min
        self.bg = False                   # True cuando el hilo corre; si no (pruebas), el ciclo lo hace todo en línea
        self.bg_ev = threading.Event()
        self.halted: dict = {}
        self.halts_ts = 0.0
        self.enrich_q: dict[str, dict] = {}
        self.enrich_lock = threading.Lock()
        self.uni_lock = threading.Lock()
        self.wake_ts = 0.0
        self.cycle_s: float | None = None  # duración del último ciclo (s): el retraso de la señal es esto + el de Yahoo
        self.replay_ts = 0.0              # cuándo terminó el último replay
        self._load()

    # ---------------- persistencia (sobrevive reinicios dentro del día) ----------------
    def _state_path(self):
        return os.path.join(STATE_DIR, "state.json")

    def _load(self):
        s = read_json(self._state_path(), {}) or {}
        today = datetime.now(timezone.utc).astimezone(ET).date().isoformat()
        self.history = {d: t for d, t in (s.get("history") or {}).items() if d != today}
        self.arm_history = {d: t for d, t in (s.get("arm_history") or {}).items() if d != today}
        if s.get("day") and s.get("day") != today and s.get("trades"):
            self.history[s["day"]] = list(s["trades"].values())
        if s.get("day") and s.get("day") != today and s.get("arms"):
            self.arm_history[s["day"]] = list(s["arms"].values())
        if s.get("day") == today:
            self.trades = s.get("trades", {})
            self.arms = s.get("arms", {})
            self.sent = set(s.get("sent", []))
            self.sent_day = today
            return
        # Contenedor nuevo (redeploy): recuperar las señales del día guardadas por GitHub Actions
        try:
            r = requests.get(f"{LOG_BASE}/{today}.json", timeout=10)
            if r.ok:
                j = r.json()
                for tr in (j.get("trades") or []):
                    self.trades[tr["t"]] = tr
                    self.sent.add(f"buy:{tr['t']}")
                    for ev, st in (("t1", "t1"), ("t2", "t2"), ("stop", "stop")):
                        if tr.get("hit1") and ev == "t1" or tr.get("status") == st:
                            self.sent.add(f"{ev}:{tr['t']}")
                for o in (j.get("armadas") or []):
                    self.arms[o["t"]] = o
                    self.sent.add(f"arm:{o['t']}")
                    if o.get("fill_m") is not None:
                        self.sent.add(f"arm-fill:{o['t']}")
                    if o.get("status") in ("cancelada", "no ejecutada"):
                        self.sent.add(f"arm-x:{o['t']}")
                self.sent_day = today
                log.info("recuperadas %d señales y %d armadas de hoy desde el registro", len(self.trades), len(self.arms))
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning("no pude leer el registro del día: %s", e)

    def _save(self):
        day = datetime.now(timezone.utc).astimezone(ET).date().isoformat()
        with self.tg_lock:
            sent = sorted(self.sent)
        write_json(self._state_path(), {"day": day, "trades": self.trades, "arms": self.arms, "sent": sent,
                                        "history": self.history, "arm_history": self.arm_history})

    def _archive(self):
        """Copia las señales (y las armadas) del día actual al historial (máx. HIST_DAYS días)."""
        for src, dst in ((self.trades, self.history), (self.arms, self.arm_history)):
            if not src:
                continue
            d = next(iter(src.values())).get("day")
            if d:
                dst[d] = [dict(x) for x in src.values()]
                for old in sorted(dst)[:-HIST_DAYS]:
                    del dst[old]

    # ---------------- Telegram ----------------
    def tg(self, key: str, text: str, wait: bool = True, private: bool = False):
        """Avisa una sola vez por clave. wait=False manda en otro hilo: la petición del puente IBKR no espera a
        Telegram (si tarda, el puente se quedaría sin enviar precios de las demás armadas)."""
        if self._claim(key):
            self._emit(key, text, wait, private)

    def _claim(self, key: str) -> bool:
        """Reserva la clave del aviso (True si nadie lo mandó todavía). El ciclo, el vigía rápido y el puente avisan
        desde hilos distintos."""
        with self.tg_lock:
            if key in self.sent:
                return False
            self.sent.add(key)
            return True

    def _emit(self, key: str, text: str, wait: bool = True, private: bool = False):
        """private: el texto lleva un precio de IBKR (uso personal): al registro de Render va solo la clave."""
        log.info("ALERTA %s", key if private else text.replace("\n", " | "))
        if wait:
            self._send(text)
        else:
            threading.Thread(target=self._send, args=(text,), name="telegram", daemon=True).start()

    def _send(self, text: str) -> bool:
        """Manda a Telegram y anota si llegó. Antes un rechazo de Telegram (token o chat equivocado) no dejaba rastro, y
        el error de red llevaba la URL con el token al registro: ahora solo se guarda el motivo."""
        tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if not (self.notify and tok and chat):
            return False
        ok, err = False, None
        try:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                              json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=15)
            ok = r.status_code == 200
            if not ok:
                try:
                    err = str(r.json().get("description") or "")[:120] or f"HTTP {r.status_code}"
                except ValueError:
                    err = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            err = f"sin conexión con Telegram ({type(e).__name__})"
        self.tg_state = {"ok": ok, "error": err, "ts": datetime.now(timezone.utc).isoformat()}
        if not ok:
            log.warning("Telegram no entregó el aviso: %s", err)
        return ok

    def hello(self):
        """Al arrancar el servicio: confirma por Telegram que el semáforo está en línea (y deja ver los reinicios)."""
        t = datetime.now(timezone.utc).astimezone(ET)
        self._send(f"✅ Semáforo en línea ({t:%H:%M} ET). Te aviso aquí: 📋 lista de apertura desde las 9:15 y "
                   f"🟡 ARMA · 🟢 COMPRA · ⚡ rupturas desde las {(OPEN_M + OR_MINUTES) // 60}:{(OPEN_M + OR_MINUTES) % 60:02d}.")

    def quotes(self, syms: list[str]) -> dict[str, dict]:
        """v7/quote con cortacircuito: si Yahoo la niega (crumb 401, límite), descansa 1, 2, 5 y luego 20 min según los
        fallos seguidos. Antes un solo lote fallido apagaba 20 min el precio en vivo y el vigía rápido sin IBKR."""
        if not syms or time.time() < self.q_off_until:
            return {}
        first = yahoo.batch_quotes(syms[:40])
        if not first:
            self.q_fail += 1
            wait = Q_BACKOFF[min(self.q_fail, len(Q_BACKOFF)) - 1]
            self.q_off_until = time.time() + wait
            log.warning("v7/quote no disponible (%d fallo/s seguidos); sigo con precios de velas por %d s", self.q_fail, wait)
            return {}
        self.q_fail = 0
        if len(syms) > 40:
            first.update(yahoo.batch_quotes(syms[40:]))
        return first

    def premarket_rank(self, syms: list[str]) -> dict[str, tuple[float, float]]:
        """{ticker: (cambio % pre-market vs. cierre previo, dólares negociados en pre-market)} con velas de 5 min."""
        out = {}
        bars = yahoo.history(syms, period="2d", interval="5m", prepost=True)
        for s, df in bars.items():
            d = to_et(df)
            if d.empty:
                continue
            days = sorted(set(d["day"]))
            if len(days) < 2:
                continue
            y = d[(d["day"] == days[-2]) & (d["m"] >= OPEN_M) & (d["m"] < 16 * 60)]
            t = d[(d["day"] == days[-1]) & (d["m"] < OPEN_M)]
            if y.empty or t.empty:
                continue
            prev, last = float(y["Close"].iloc[-1]), float(t["Close"].iloc[-1])
            out[s] = (100 * (last / prev - 1), float((t["Close"] * t["Volume"]).sum()))
        return out

    # ---------------- universo ----------------
    def refresh_universe(self, now: datetime, phase: str, halted: dict):
        """Un refresco a la vez: lo hace el hilo de contexto y, si se atrasa, el ciclo."""
        if not self.uni_lock.acquire(blocking=False):
            return
        try:
            self._refresh_universe(now, phase, halted)
        finally:
            self.uni_lock.release()

    def _refresh_universe(self, now: datetime, phase: str, halted: dict):
        watch = load_watchlist()
        cand: dict[str, set] = {}

        def add(sym, src):
            s = str(sym or "").upper().strip()
            if s and re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", s):
                cand.setdefault(s, set()).add(src)

        Q = yahoo.yf.EquityQuery
        exch = Q("is-in", ["exchange", *yahoo.US_EXCH])
        custom = Q("and", [Q("gt", ["percentchange", 3]), exch, Q("gt", ["dayvolume", 300000]), Q("gte", ["intradayprice", 1])])
        sq: dict[str, dict] = {}  # cotizaciones que ya traen las pantallas (respaldo si v7/quote falla)
        r = yahoo.retry(lambda: yahoo.yf.screen(custom, size=150, sortField="percentchange", sortAsc=False), tries=2, what="screen subidas")
        for q in yahoo._quotes(r):
            add(q.get("symbol"), "subidas")
            sq[str(q.get("symbol")).upper()] = q
        for name in ("day_gainers", "most_actives", "small_cap_gainers"):
            r = yahoo.retry(lambda name=name: yahoo.yf.screen(name, count=100), tries=2, what=f"screen {name}")
            for q in yahoo._quotes(r):
                add(q.get("symbol"), name)
                sq[str(q.get("symbol")).upper()] = q
        for s in watch:
            add(s, "watchlist")
        # Escáneres de IBKR (puente): ven moverse una acción antes que las listas de Yahoo. En sesión entran directo
        # (como la lista de seguimiento); en pre-market compiten en el ranking como cualquier candidato.
        ib_top = self.bridge.scan_symbols(IBKR_TOP)
        for s in ib_top:
            add(s, "ibkr")
        for s, h in halted.items():
            add(s, "halt")
        scan_url = os.environ.get("RADAR_SCAN_URL")
        if scan_url:
            try:
                j = requests.get(scan_url, timeout=15).json()
                for it in (j.get("items") or [])[:25]:
                    add(it.get("t"), "escaneo")
            except (requests.RequestException, ValueError):
                pass
        quotes = self.quotes(sorted(cand))
        for s, q in sq.items():
            quotes.setdefault(s, q)
        for s, q in quotes.items():
            nm = q.get("shortName") or q.get("longName")
            if nm:
                self.names[s] = nm
        t = now.astimezone(ET)
        elapsed = max(0, min(390, t.hour * 60 + t.minute - OPEN_M))
        frac = 0.12 + 0.88 * elapsed / 390 if phase != "pre" else 0.05
        ranked = []
        if phase == "pre":
            pmr = self.premarket_rank(sorted(cand))
            for s, (chg, usd) in pmr.items():
                if chg >= 2 or usd >= 50_000:  # Yahoo a veces reporta volumen 0 en pre-market
                    ranked.append((max(chg, 0) / 2 + math.log1p(usd / 1e5), s))
            quotes = {}
        for s, q in quotes.items():
            px = fnum(q.get("regularMarketPrice"))
            if not px or px < 1 or q.get("quoteType") not in ("EQUITY", "ETF"):
                continue
            chg = fnum(q.get("preMarketChangePercent") if phase == "pre" else q.get("regularMarketChangePercent")) or 0
            vol = fnum(q.get("preMarketVolume") if phase == "pre" else q.get("regularMarketVolume")) or 0
            avg = fnum(q.get("averageDailyVolume10Day")) or fnum(q.get("averageDailyVolume3Month")) or 0
            rv = vol / (avg * frac) if avg else 0
            ranked.append((math.log1p(rv) * 2 + max(chg, 0) / 4, s))
        ranked.sort(reverse=True)
        top = [s for _, s in ranked[:UNIVERSE_N]]
        uni = list(dict.fromkeys(top + watch + list(halted) + (ib_top if phase == "open" else [])))
        self.universe, self.base_universe = uni, list(uni)
        self.sources = {s: sorted(cand.get(s, [])) for s in uni}
        self.universe_ts = time.time()
        log.info("universo: %d candidatos, %d cotizados → %d en seguimiento", len(cand), len(quotes), len(uni))

    def ensure_context(self, syms: list[str], today):
        """Curva de volumen de 5 días, ATR y cierre previo de cada acción. Si Yahoo falla con alguna, se vuelve a pedir
        cada CTX_RETRY_S: antes quedaba sin curva todo el día (RVOL vacío → NO) justo con las que entran a las 9:2x."""
        now = time.time()
        need = [s for s in syms if s not in self.baseline
                or ((self.baseline.get(s) is None or self.prev.get(s) is None)
                    and now - self.ctx_try.get(s, 0) >= CTX_RETRY_S)]
        if not need:
            return
        for s in need:
            self.ctx_try[s] = now
        h5 = yahoo.history(need, period="5d", interval="1m", prepost=False)
        hd = yahoo.history(need, period="3mo", interval="1d")
        for s in need:
            base = baseline_curve(h5.get(s), today)
            if base is not None or s not in self.baseline:
                self.baseline[s] = base
            d = hd.get(s)
            if d is not None and not d.empty:
                d = d.dropna(subset=["Close"])
                d = d[[ix.date() < today for ix in d.index]]
            if d is not None and not d.empty:
                self.atr[s] = atr_pct(d)
                self.prev[s] = float(d["Close"].iloc[-1])
            else:
                self.atr.setdefault(s, None)
                self.prev.setdefault(s, None)

    # ---------------- enriquecimiento con caché ----------------
    def news_ctx(self, s, now_ts, name=None, ttl=NEWS_TTL):
        c = self.cache.get(("news", s), ttl, MISS)
        if c is MISS:
            c = self.cache.put(("news", s), classify(yahoo.news(s, count=10) or rss_news(s, name), now_ts, s, name))
        return c

    def sec_ctx(self, s, today):
        c = self.cache.get(("sec", s), 6 * 3600, MISS)
        if c is MISS:
            if not self.sec_map:
                self.sec_map = other.sec_ticker_map()
            cik = self.sec_map.get(s)
            c = self.cache.put(("sec", s), other.filing_flags(other.sec_submissions(cik), today) if cik else {})
        return c

    def opt_ctx(self, s, px):
        """callVolOI de las opciones a ≤ 30 días. Un None (sin opciones) también se guarda 15 min: antes se volvía a
        pedir la cadena de opciones en cada ciclo."""
        c = self.cache.get(("opt", s), 900, MISS)
        if c is MISS:
            chains, _ = yahoo.option_chains(s, max_days=30)
            c = self.cache.put(("opt", s), options_features(chains, px).get("callVolOI"))
        return c

    def _enrich(self, s, m, ctx, now_ts, today, with_opt: bool, hot: bool, inline: bool = False) -> bool:
        """Noticias, SEC y opciones de una acción, desde la caché. Sin el hilo de contexto (pruebas) o con inline=True
        las pide en el acto, como antes; con él, lo que falte o esté viejo queda en cola y el ciclo sigue con lo último
        que haya. Devuelve True si la acción tiene noticias y SEC (aunque sean de antes): sin eso no se sabe si hay
        dilución y el ciclo no debe dar COMPRA ni ARMA (lo resuelve pidiéndolas en el acto)."""
        ttl = NEWS_TTL_HOT if hot else NEWS_TTL
        if not self.bg or inline:
            cat = self.news_ctx(s, now_ts, ctx.get("name"), ttl)
            f = self.sec_ctx(s, today)
            cvo = self.opt_ctx(s, m.get("px")) if with_opt and not inline else (
                self.cache.get(("opt", s), 3600) if with_opt else None)
            complete = True
        else:
            cat = self.cache.get(("news", s), ttl, MISS)
            f = self.cache.get(("sec", s), 6 * 3600, MISS)
            cvo = self.cache.get(("opt", s), 900, MISS) if with_opt else None
            if cat is MISS or f is MISS or cvo is MISS:
                with self.enrich_lock:
                    self.enrich_q[s] = {"name": ctx.get("name"), "px": m.get("px"), "today": today, "opt": with_opt,
                                        "ttl": ttl}
                self.bg_ev.set()
            # Mientras llega lo nuevo, lo último que haya sigue contando (titulares hasta 6 h; SEC hasta 24 h: antes
            # de las 10:10 vencía la SEC leída en pre-market y la COMPRA salía sin el filtro de dilución)
            if cat is MISS:
                cat = self.cache.get(("news", s), 6 * 3600, MISS)
            if f is MISS:
                f = self.cache.get(("sec", s), 24 * 3600, MISS)
            complete = cat is not MISS and f is not MISS
            cat = {} if cat is MISS else cat
            f = {} if f is MISS else f
            if cvo is MISS:
                cvo = self.cache.get(("opt", s), 3600)
        if cat.get("type"):
            ctx["cat"] = {k: cat.get(k) for k in ("type", "age", "title", "url", "hours")}
        ctx["offer"] = cat.get("offer")
        ctx["news_ok"] = cat.get("n", 0) > 0
        ctx["offer30"], ctx["shelf"] = f.get("offer30"), f.get("shelf36m")
        if with_opt:
            ctx["callVolOI"] = cvo
        return complete

    # ---------------- hilo de contexto (segundo plano) ----------------
    def context_once(self, now: datetime | None = None) -> dict:
        """Lo lento que no tiene que frenar al ciclo de 1 min: halts de NASDAQ, refresco del universo y las noticias,
        SEC y opciones que el ciclo dejó en cola. Si llega una noticia fresca, despierta al ciclo."""
        now = now or datetime.now(timezone.utc)
        phase = phase_of(now)
        done = {"halts": False, "universe": False, "enriched": 0}
        if phase == "closed":
            return done
        if time.time() - self.halts_ts >= HALTS_S:
            self.halted, self.halts_ts = halts_src.parse(halts_src.fetch(), now), time.time()
            done["halts"] = True
        if self.universe and time.time() - self.universe_ts > (UNIVERSE_TTL if phase == "open" else 600):
            self.refresh_universe(now, phase, self.halted)
            done["universe"] = True
        with self.enrich_lock:
            jobs, self.enrich_q = self.enrich_q, {}
        hot = False
        for s, j in jobs.items():
            try:
                before = self.cache.get(("news", s), 6 * 3600, {})
                cat = self.news_ctx(s, now.timestamp(), j["name"], j["ttl"])
                self.sec_ctx(s, j["today"])
                if j["opt"] and (j["px"] or 0) >= 3:
                    self.opt_ctx(s, j["px"])
                done["enriched"] += 1
                if cat.get("type") and cat.get("age") in ("fresh", "d1") and cat.get("title") != before.get("title"):
                    hot = True
            except Exception as e:  # noqa: BLE001
                log.warning("enriquecer %s: %s", s, e)
        if hot and phase == "open" and time.time() - self.wake_ts >= 20:
            self.wake_ts = time.time()
            self.wake.set()
        return done

    def context_forever(self):
        self.bg = True
        while True:
            try:
                self.context_once()
            except Exception as e:  # noqa: BLE001
                log.warning("hilo de contexto: %s", e)
            self.bg_ev.wait(5)
            self.bg_ev.clear()

    # ---------------- ciclo ----------------
    def cycle(self, now: datetime | None = None):
        now = now or datetime.now(timezone.utc)
        t_et = now.astimezone(ET)
        today = t_et.date()
        phase = phase_of(now)
        if self.day != today:
            self.day, self.baseline, self.atr, self.prev = today, {}, {}, {}
            self.ctx_try = {}
            self.breaks, self.early, self.vol_k, self.ctx_cache = {}, {}, {}, {}
            self.bridge.reset_bars()
            self.cache = TTLCache()  # noticias, SEC y opciones de ayer ya no sirven: sin esto la caché crece cada día
            iso = today.isoformat()
            if any(x.get("day") != iso for x in list(self.trades.values()) + list(self.arms.values())):
                self._archive()
                self.trades = {k: v for k, v in self.trades.items() if v.get("day") == iso}
                self.arms = {k: v for k, v in self.arms.items() if v.get("day") == iso}
            if self.sent_day != iso:
                # Los avisos son por día. Antes solo se vaciaban si hubo COMPRA: un ARMA de ayer sin COMPRA
                # bloqueaba el de hoy para la misma acción (y con el servicio sin reinicios eso pasa seguido).
                with self.tg_lock:
                    self.sent = set()
                self.sent_day = iso
        if phase == "closed":
            self._eod(t_et)
            self.armed, self.bar_want = {}, []
            self.snapshot = {**self.snapshot, "phase": phase, "status": "mercado cerrado", "ts": now.isoformat(),
                             "trades": list(self.trades.values()), "stats": self.stats(), "armed": [], "watching": [],
                             "armadas": list(self.arms.values()), "armStats": self.arm_stats(),
                             "bridge": self.bridge.status()}
            return
        try:  # el monitor de posiciones va antes de lo pesado: si el escaneo falla, los avisos de tus posiciones siguen
            self._watch_positions(now, phase)
        except Exception as e:  # noqa: BLE001
            log.warning("monitor de posiciones: %s", e)
        # Halts y refresco del universo: con el hilo de contexto corriendo, los hace él (el ciclo usa lo último que
        # trajo); sin él, o si se atrasó, aquí mismo como antes
        if self.bg and time.time() - self.halts_ts <= 3 * HALTS_S:
            halted = self.halted
        else:
            halted = self.halted = halts_src.parse(halts_src.fetch(), now)
            self.halts_ts = time.time()
        due = time.time() - self.universe_ts > (UNIVERSE_TTL if phase == "open" else 600)
        if not self.universe or (due and not self.bg) or (self.bg and time.time() - self.universe_ts > 4 * UNIVERSE_TTL):
            self.refresh_universe(now, phase, halted)
        elif due:
            self.bg_ev.set()
        if phase == "open":
            # Lo que los escáneres de IBKR ven AHORA entra ya, sin esperar el próximo refresco del universo. Reemplaza
            # a los agregados del ciclo anterior (no se acumulan: cada uno pide 5 días de velas y la memoria es justa).
            if self.base_universe is None:
                self.base_universe = list(self.universe)
            base = set(self.base_universe)
            extra = [s for s in self.bridge.scan_symbols(IBKR_TOP) if s not in base]
            self.universe = self.base_universe + extra
            for s in extra:
                self.sources.setdefault(s, ["ibkr"])
        # Las señales y órdenes armadas abiertas se siguen aunque su acción salga del universo (antes, SITC dejó de
        # seguirse a los 6 minutos de su COMPRA del 1-oct: sin avisos de stop ni de objetivo).
        active = [s for s, x in list(self.trades.items()) + list(self.arms.items())
                  if x.get("status") in ("pendiente", "abierta", "t1")]
        syms = list(dict.fromkeys(self.universe + active + ["SPY", "QQQ"]))
        self.ensure_context(syms, today)
        bars = yahoo.history(syms, period="1d", interval="1m", prepost=True)
        quotes = self.quotes(syms)
        self.last_bars = bars
        self.vol_k = {}  # con velas de Yahoo nuevas, la calibración del volumen de IBKR se rehace

        def prev_of(s):
            return fnum(quotes.get(s, {}).get("regularMarketPreviousClose")) or self.prev.get(s)

        def mets(s):
            q = quotes.get(s, {})
            live = fnum(q.get("preMarketPrice")) if phase == "pre" else fnum(q.get("regularMarketPrice"))
            return session_metrics(bars.get(s), prev_of(s), self.baseline.get(s), now, live)

        spy, qqq = mets("SPY"), mets("QQQ")
        reg, reg_txt = regime(spy, qqq) if phase != "pre" else ("verde", "pre-market")
        watch = set(load_watchlist())
        base = {}
        for s in self.universe:
            q = quotes.get(s, {})
            m = mets(s)
            spread = sane_spread(fnum(q.get("bid")), fnum(q.get("ask")), m.get("px"), m.get("rng1m"))
            h = halted.get(s)
            ctx = {"phase": phase, "regime": reg, "spy_chg": spy.get("chg"), "spread": spread, "atr": self.atr.get(s),
                   "halted": h if h and not h["resumed"] else None,
                   "name": q.get("shortName") or q.get("longName") or self.names.get(s), "watch": s in watch}
            if h and h["code"] == "T1":
                ctx["cat"] = {"type": "halt_news", "age": "fresh", "title": f"halt T1 {h['time']}"}
            base[s] = (m, ctx, decide(s, m, ctx))
        # Enriquecer a los más prometedores (noticias, SEC, opciones) y decidir de nuevo. También a las que una noticia
        # fresca pondría en juego (RVOL 1.5–2: el umbral baja a 1.5 con noticia), aunque hoy salgan NO.
        def prio(s):
            r = base[s][2]
            near = r["decision"] == "NO" and 1.5 <= (r.get("rvol") or 0) < D_RVOL_IN_PLAY
            return (r["decision"] != "NO" or near, r["score"])
        order = sorted(base, key=prio, reverse=True)
        now_ts = now.timestamp()
        for i, s in enumerate(order[:ENRICH_N]):
            m, ctx, first = base[s]
            # Titulares más seguido (NEWS_TTL_HOT) solo para las que están por disparar: cada pedido extra a Yahoo
            # suma al riesgo de que limite la IP y se pierdan las velas
            hot = s in self.armed or first["decision"] == "COMPRA" or (first["decision"] == "ESPERA" and first.get("level"))
            with_opt = i < 10 and (m.get("px") or 0) >= 3
            complete = True
            try:
                complete = self._enrich(s, m, ctx, now_ts, today, with_opt, hot)
            except Exception as e:  # noqa: BLE001
                log.warning("enriquecer %s: %s", s, e)
                complete = not self.bg  # en línea (sin hilo) se sigue como antes; con el hilo, se pide en el acto abajo
            r = decide(s, m, ctx)
            if not complete and (r["decision"] == "COMPRA" or (r["decision"] == "ESPERA" and r.get("level"))):
                # Sin noticias ni SEC todavía (acción nueva o servicio recién reiniciado) no se sabe si hay dilución:
                # para una que daría COMPRA o ARMA se piden en el acto, como antes del hilo de contexto
                try:
                    self._enrich(s, m, ctx, now_ts, today, with_opt, hot, inline=True)
                    r = decide(s, m, ctx)
                except Exception as e:  # noqa: BLE001
                    log.warning("enriquecer %s: %s", s, e)
                    if r["decision"] == "COMPRA" or r.get("level"):
                        r = {**r, "decision": "ESPERA", "reason": "sin noticias ni SEC todavía: no sé si hay dilución",
                             "plan": None, "level": None, "trigger": "se reevalúa en el próximo ciclo"}
            base[s] = (m, ctx, r)
        if IBKR_BARS > 0:  # lo que el aviso temprano necesita del ciclo para decidir con las velas de IBKR
            self.ctx_cache = {s: {"ctx": dict(base[s][1]), "prev": prev_of(s)} for s in base}
        self.reg_txt = reg_txt
        rows = [base[s][2] for s in base]
        for r in rows:
            r["src"] = self.sources.get(r["t"], [])
        rank = {"COMPRA": 0, "ESPERA": 1, "NO": 2}
        rows.sort(key=lambda r: (rank[r["decision"]], -((r["chg"] or 0) if phase == "pre" else r["score"])))
        self._track(rows, bars, t_et, reg_txt)
        self._want_bars(rows, phase)
        # "Mejor opción" solo existe si hay COMPRA: un ESPERA con plan arriba se leía como orden de compra (ACN, 1-oct)
        best = next((r for r in rows if r["decision"] == "COMPRA"), None)
        self._track_arms(rows, bars, t_et)
        self._arm(rows, reg, phase, t_et.hour * 60 + t_et.minute)
        if phase == "pre":
            self._pre_list(rows, t_et)
        esp = [r for r in rows if r["decision"] == "ESPERA"]
        watching = [r["t"] for r in esp if r.get("level")] + [r["t"] for r in esp if not r.get("level")]
        self.snapshot = {
            "status": "ok", "ts": now.isoformat(), "et": t_et.strftime("%H:%M:%S"), "phase": phase,
            "regime": reg, "regimeText": reg_txt, "best": best["t"] if best else None, "rows": rows,
            "watching": watching[:3], "armed": sorted(self.armed), "fast": self.fast_state,
            "counts": {k: sum(1 for r in rows if r["decision"] == k) for k in rank},
            "trades": list(self.trades.values()), "stats": self.stats(), "universe": len(self.universe),
            "armadas": list(self.arms.values()), "armStats": self.arm_stats(),
            "halts": {s: h for s, h in halted.items()}, "errors": self.errors[-5:], "bridge": self.bridge.status(),
            "cycle_s": self.cycle_s,
        }
        self._save()

    # ---------------- rupturas armadas y lista de apertura ----------------
    def _arm(self, rows, reg, phase, now_m):
        """Rupturas listas para dejar la orden puesta: ESPERA con gatillo de ruptura, fuerza ≥ ARM_MIN (+5 con mercado
        amarillo, igual que la COMPRA), riesgo ≤ RISK_MAX y el precio a ≤ ARM_NEAR % del gatillo. Avisa una vez por
        ticker y día con la orden completa (compra stop) y desde ese momento la sigue como orden virtual (_track_arms).
        Una orden ya activada, cancelada o vencida no se vuelve a armar ese día."""
        armed = {}
        need = ARM_MIN + (5 if reg == "amarillo" else 0)
        cutoff = min(ENTRY_END_M, LAST_ENTRY_M)
        if phase == "open" and reg != "rojo" and now_m < cutoff:
            for r in rows:
                lvl, p, px = r.get("level"), r.get("plan"), r.get("px")
                if (r["decision"] != "ESPERA" or not lvl or not p or not px or r["t"] in self.trades
                        or (r["t"] in self.arms and self.arms[r["t"]].get("status") != "pendiente")
                        or (p.get("risk") or 99) > RISK_MAX or r["score"] < need or px < lvl * (1 - ARM_NEAR / 100)):
                    continue
                armed[r["t"]] = {"level": lvl, "entry": p["entry"], "stop": p["stop"], "t1": p["t1"], "t2": p.get("t2"),
                                 "risk": p["risk"], "score": r["score"], "px": px, "chg": r.get("chg"), "reason": r["reason"]}
            # Si a una armada pendiente le faltan datos este ciclo (Yahoo no dio velas), el vigía y el puente la siguen
            # mirando: la orden sigue puesta y la ruptura se puede dar igual
            nodata = {r["t"] for r in rows if r.get("nodata")}
            for t, a in self.armed.items():
                if t not in armed and t in nodata and (self.arms.get(t) or {}).get("status") == "pendiente":
                    armed[t] = a
        self.armed = armed
        with self.tg_lock:  # el vigía rápido puede estar agregando avisos en otro hilo
            n = sum(1 for k in self.sent if k.startswith("arm:"))
        day = self.day.isoformat() if self.day else None
        for t, a in sorted(armed.items(), key=lambda kv: -kv[1]["score"]):
            if n >= ARM_MAX or f"arm:{t}" in self.sent:
                continue
            self.tg(f"arm:{t}", (
                f"🟡 ARMA {t} · {a['px']:.2f} ({(a['chg'] or 0):+.1f}%) · fuerza {a['score']}\n"
                f"Gatillo: rompe {a['level']:.2f}. Orden: compra stop {a['entry']:.2f} (límite {a['entry'] * 1.003:.2f}) · "
                f"stop {a['stop']:.2f} (−{a['risk']:.1f}%) · +2%: {a['t1']:.2f}\n"
                f"{a['reason']}. Si rompe, te aviso al instante; si la jugada se daña antes, te aviso para cancelarla."))
            if t not in self.arms:
                self.arms[t] = new_arm(t, a, day, f"{now_m // 60:02d}:{now_m % 60:02d}", now_m, cutoff)
            n += 1

    def _track_arms(self, rows, bars, t_et):
        """Sigue cada compra stop de un aviso ARMA como si estuviera puesta en el bróker: si se activa, su resultado
        queda registrado aparte (para comparar entrar en la ruptura contra esperar la COMPRA confirmada); si la jugada
        se daña antes de romper (pierde el stop, el semáforo la pasa a NO COMPRES, salta el límite o se acaba la hora
        de entradas), avisa para cancelarla y que no se llene tarde."""
        now_m = t_et.hour * 60 + t_et.minute
        by_t = {r["t"]: r for r in rows}
        for s, o in self.arms.items():
            b = self.breaks.get(s)
            if b and "break_t" not in o:
                o.update(b)  # cuándo y con qué datos se avisó la ruptura (⚡): mide cuánto adelanta el puente IBKR
            if o.get("status") not in ("pendiente", "abierta", "t1"):
                continue
            d = to_et(bars.get(s))
            if not d.empty:
                after = d[(d["day"] == t_et.date()) & (d["m"] > o["m"]) & (d["m"] < 16 * 60)]
                for ev in advance(o, after):
                    msg = {"fill": f"📥 {s}: se activó la compra stop {o['entry']:.2f} (≈{o.get('fill', o['entry']):.2f}). "
                                   f"Stop {o['stop']:.2f} · +2 %: {o['t1']:.2f}.",
                           "invalid": f"❌ {s} perdió {o['stop']:.2f} sin romper: cancela la compra stop {o['entry']:.2f}.",
                           "gap": f"⚠ {s} saltó por encima de {o['cap']:.2f}: la orden no se llena. Cancélala y no persigas.",
                           "expired": f"⌛ {s}: la compra stop {o['entry']:.2f} no se activó a tiempo. Cancélala."}.get(ev)
                    if msg:
                        self.tg(f"arm-fill:{s}" if ev == "fill" else f"arm-x:{s}", msg)
                self._move_cursor(o, after)
            if o["status"] != "pendiente":
                continue
            r = by_t.get(s)
            # Solo cancela un NO del mercado: no uno por falta de datos (Yahoo no entregó velas o la curva de volumen), ni
            # si el precio ya está sobre la entrada (la orden pudo activarse y las velas aún no lo muestran)
            if (r is not None and r["decision"] == "NO" and not r.get("nodata") and not d.empty
                    and not (r.get("px") and r["px"] >= o["entry"])):
                o["status"], o["cancel"] = "cancelada", r["reason"]
                self.tg(f"arm-x:{s}", f"❌ {s}: cancela la compra stop {o['entry']:.2f}. {r['reason']}.")
            elif now_m >= (o.get("valid_m") or 10**9):
                o["status"] = "no ejecutada"
                self.tg(f"arm-x:{s}", f"⌛ {s}: la compra stop {o['entry']:.2f} no se activó a tiempo. Cancélala.")

    @staticmethod
    def _move_cursor(o: dict, after):
        """Después de revisar velas nuevas: último precio y cursor. El cursor queda antes de las últimas PARTIAL_BARS
        velas (pueden estar a medio llenar) para revisarlas otra vez completas; antes se daban por vistas y un stop o un
        +2 % tocado en la segunda mitad de ese minuto no se avisaba."""
        if after is None or after.empty:
            return
        o["last"] = float(after["Close"].iloc[-1])
        if len(after) > PARTIAL_BARS:
            o["m"] = max(int(o["m"]), int(after["m"].iloc[-1 - PARTIAL_BARS]))

    def _pre_list(self, rows, t_et):
        """Desde las 9:15 ET: los gaps del pre-market con su referencia, para llegar preparado a la apertura."""
        if t_et.hour * 60 + t_et.minute < 9 * 60 + 15:
            return
        gaps = [r for r in rows if r["decision"] == "ESPERA"][:6]
        if not gaps:
            return
        lines = []
        for r in gaps:
            cat = (r.get("cat") or {}).get("type")
            pmh = f" · máx. pre {r['pmh']:.2f}" if r.get("pmh") else ""
            lines.append(f"{r['t']} {(r.get('chg') or 0):+.1f}%{pmh}" + (f" · {CAT_NAME.get(cat, cat)}" if cat else ""))
        self.tg(f"pre:{t_et.date().isoformat()}", "📋 Lista de apertura\n" + "\n".join(lines) +
                f"\nGatillo: ruptura del rango de los primeros {OR_MINUTES} min con volumen; te aviso cuando se arme cada una.")

    # ---------------- vigía rápido: precio de las rupturas armadas (IBKR al instante o Yahoo cada FAST_S s) -------
    def _check_breaks(self, prices: dict, src: str, now: datetime) -> list[str]:
        """Avisa una sola vez cada acción armada cuyo precio cruzó el gatillo y despierta el ciclo completo para que
        confirme la COMPRA sin esperar su turno. Guarda hora y fuente de la ruptura (no el precio: los datos de IBKR
        son de uso personal y las armadas se publican en /api/trades y en GitHub) para medir cuánto adelanta."""
        armed = dict(self.armed)
        fired: list[str] = []
        out: list[tuple[str, str]] = []
        ib = src == "ibkr"
        with self.brk_lock:
            for s, p in prices.items():
                a = armed.get(s)
                if not a or not p or p < a["level"] * 1.001:
                    continue
                key = f"break:{s}:{a['level']:.2f}"
                if not self._claim(key):
                    continue
                if p <= a["entry"] * 1.004:
                    msg = (f"⚡ {s} rompe {a['level']:.2f} ahora ({p:.2f}{', IBKR' if ib else ''}). Entrada ≤ "
                           f"{a['entry'] * 1.003:.2f} · stop {a['stop']:.2f} (−{a['risk']:.1f}%) · +2%: {a['t1']:.2f}\n"
                           f"Confirma volumen en tu gráfico; el semáforo lo reevalúa ya.")
                else:
                    msg = (f"⚡ {s} rompió {a['level']:.2f} y ya va en {p:.2f}{' (IBKR)' if ib else ''}: no persigas. "
                           f"Si dejaste la orden armada, ya entraste; si no, espera el retesteo que marque el semáforo.")
                out.append((key, msg))
                self.breaks.setdefault(s, {"break_t": now.astimezone(ET).strftime("%H:%M:%S"), "break_src": src})
                fired.append(s)
        if fired:
            self.wake.set()
        # El envío va fuera del candado: antes un aviso de Yahoo (hasta 15 s esperando a Telegram) frenaba al puente
        for key, msg in out:
            self._emit(key, msg, wait=not ib, private=ib)
        return fired

    def fast_once(self, now: datetime | None = None) -> list[str]:
        """Revisa las acciones armadas. Con el puente IBKR conectado, su precio (al instante) manda; Yahoo solo cubre
        las que no tengan precio fresco del puente. De Yahoo usa solo la cotización v7 (las descargas de velas de
        yfinance no son seguras en paralelo con las del ciclo); si Yahoo la niega, el vigía espera y el ciclo de 1 min
        sigue como siempre. Un fallo aquí no activa el cortacircuito de cotizaciones del ciclo."""
        now = now or datetime.now(timezone.utc)
        armed = dict(self.armed)
        fired: list[str] = []
        ib = {}
        if phase_of(now) == "open" and armed:
            for s in armed:
                p = self.bridge.price(s)
                if p:
                    ib[s] = p
            fired += self._check_breaks(ib, "ibkr", now)
            rest = sorted(s for s in armed if s not in ib)
            if rest and time.time() >= self.q_off_until:
                yq = {s: fnum(q.get("regularMarketPrice")) for s, q in (yahoo.batch_quotes(rest) or {}).items()}
                fired += self._check_breaks(yq, "yahoo", now)
        self.fast_state = {"ts": now.isoformat(), "armed": len(armed), "fired": fired, "ibkr": len(ib)}
        return fired

    # ---------------- velas de IBKR: aviso temprano de COMPRA (privado, por Telegram) ----------------
    def _want_bars(self, rows, phase):
        """Candidatas que el puente sigue con velas de IBKR: las ESPERA de más fuerza que aún no dieron COMPRA (en
        pre-market, los gaps, para llegar a la apertura con el día cargado). Una que ya se sigue conserva su lugar
        mientras siga entre las 2×N mejores: cada cambio es una suscripción nueva en IBKR."""
        if IBKR_BARS <= 0 or phase not in ("pre", "open"):
            self.bar_want = []
            return
        top = [r["t"] for r in rows if r["decision"] == "ESPERA" and r["t"] not in self.trades][:2 * IBKR_BARS]
        keep = [s for s in self.bar_want if s in top]
        self.bar_want = (keep + [s for s in top if s not in keep])[:IBKR_BARS]

    def _vol_k(self, s: str, rows: list) -> float | None:
        """Factor de volumen IBKR→Yahoo de un ticker, medido contra las velas de Yahoo del último ciclo."""
        k = self.vol_k.get(s)
        if k is None and rows:
            k = vol_scale(self.last_bars.get(s), rows_frame(rows))
            if k:
                self.vol_k[s] = k
        return k

    def _early(self, syms: list[str], now: datetime) -> list[str]:
        """Las reglas del semáforo con las velas de IBKR (al día cada ~5 s) en vez de las de Yahoo (1–2 min de retraso)
        y el resto del contexto del último ciclo (noticias, mercado, spread, ATR). Si da COMPRA antes que el ciclo,
        avisa por Telegram en el acto y despierta el ciclo. El aviso es privado: la COMPRA pública (página, API,
        GitHub) la sigue dando el ciclo con datos de Yahoo, y a esa señal solo se le anota la hora del aviso temprano."""
        if IBKR_BARS <= 0 or phase_of(now) != "open":
            return []
        t_et = now.astimezone(ET)
        now_m = t_et.hour * 60 + t_et.minute
        want = set(self.bar_want)
        out = []
        for s in syms:
            c = self.ctx_cache.get(s)
            if not c or s not in want or s in self.trades or f"early:{s}" in self.sent:
                continue
            rows = self.bridge.bar_rows(s)
            k = self._vol_k(s, rows) if rows else None
            if not k:
                continue
            m = session_metrics(rows_frame(rows, k), c["prev"], self.baseline.get(s), now, self.bridge.price(s))
            r = decide(s, m, {**c["ctx"], "phase": "open"})
            if r["decision"] != "COMPRA" or not r.get("plan"):
                continue
            tr = new_trade(r, t_et.date().isoformat(), t_et.strftime("%H:%M"), now_m)
            self.tg(f"early:{s}", buy_text(r, tr, self.reg_txt, head="🟢⚡ COMPRA", tail=(
                "\nVisto con velas de IBKR antes que el semáforo (Yahoo va 1–2 min atrás). Confirma en tu gráfico; "
                "el semáforo la reevalúa ya.")), wait=False)
            self.early.setdefault(s, {"early_t": t_et.strftime("%H:%M:%S"), "early_src": "ibkr"})
            out.append(s)
        if out:
            self.wake.set()
        return out

    def on_bridge(self, scan: dict | None = None, quotes: dict | None = None, info: dict | None = None,
                  now: datetime | None = None, bars: dict | None = None) -> dict:
        """Datos del puente IBKR: guarda escáneres, precios y velas; si una acción armada cruzó su gatillo o una
        candidata da COMPRA con las velas de IBKR, avisa en el acto. Responde qué vigilar: las acciones armadas con su
        gatillo (el puente se suscribe a su precio) y las candidatas que quiere con velas, con la hora de la última
        vela que ya tiene de cada una (el puente manda desde ahí)."""
        now = now or datetime.now(timezone.utc)
        fresh = self.bridge.update(scan, quotes, info)
        changed = self.bridge.put_bars(bars) if bars else []
        phase = phase_of(now)
        armed = dict(self.armed)
        fired, early = [], []
        if phase == "open":
            fired = self._check_breaks({s: self.bridge.price(s) for s in fresh if s in armed}, "ibkr", now)
            if changed:
                early = self._early(changed, now)
        t_et = now.astimezone(ET)
        out = {"ok": True, "phase": phase, "fired": fired,
               "armed": {s: {"level": a["level"], "entry": a["entry"]} for s, a in armed.items()},
               "bars": self.bridge.bars_have(list(self.bar_want)) if IBKR_BARS > 0 else {},
               "bars_t0": int(datetime(t_et.year, t_et.month, t_et.day, 4, 0, tzinfo=ET).timestamp())}
        if early:
            out["early"] = early
        return out

    def fast_forever(self):
        while True:
            try:
                self.fast_once()
            except Exception as e:  # noqa: BLE001
                log.warning("vigía rápido: %s", e)
            time.sleep(FAST_S)

    # ---------------- posiciones reales ----------------
    def _watch_positions(self, now: datetime, phase: str):
        """Último precio de cada posición registrada (cotización; si Yahoo la niega, última vela de 1 min) → avisos."""
        syms = self.positions.symbols()
        if not syms:
            return
        px: dict[str, float] = {}
        for s, q in self.quotes(syms).items():
            v = fnum(q.get("preMarketPrice")) if phase == "pre" else None
            v = v or fnum(q.get("regularMarketPrice"))
            if v:
                px[s] = v
        missing = [s for s in syms if not px.get(s)]
        if missing:
            for s, df in yahoo.history(missing, period="1d", interval="1m", prepost=True).items():
                d = to_et(df)
                if not d.empty:
                    px[s] = float(d["Close"].iloc[-1])
        for key, text in self.positions.check(px):
            self.tg(key, text)

    # ---------------- seguimiento de señales y alertas ----------------
    def _track(self, rows, bars, t_et, reg_txt):
        now_m = t_et.hour * 60 + t_et.minute
        day = t_et.date().isoformat()
        for r in rows:
            if r["decision"] != "COMPRA" or r["t"] in self.trades:
                continue
            tr = self.trades[r["t"]] = new_trade(r, day, t_et.strftime("%H:%M"), now_m)
            lvl, late = r.get("level"), ""
            if lvl:
                # Qué tan tarde llega la señal: minutos desde la ruptura y cuánto sobre el nivel queda la entrada
                tr["level"] = round(lvl, 4)
                tr["slip"] = round(100 * (tr["entry"] / lvl - 1), 2)
                tr["lag_min"] = break_lag(bars.get(r["t"]), lvl, t_et)
                if tr["lag_min"] is not None:
                    late = f"\nRompió {lvl:.2f} hace {tr['lag_min']} min; la entrada queda {tr['slip']:+.1f}% sobre el nivel."
            e = self.early.get(r["t"])
            if e:  # ya la avisó el aviso temprano con velas de IBKR: se anota cuándo (sin precios) para medir cuánto adelantó
                tr.update(e)
                late += f"\nIBKR la vio a las {e['early_t']} (aviso 🟢⚡)."
            self.tg(f"buy:{r['t']}", buy_text(r, tr, reg_txt, tail=late) +
                    f"\nPara operarla dime: «ejecuta {r['t']} $monto, vender a +3%»")
        for s, tr in self.trades.items():
            if tr["status"] not in ("pendiente", "abierta", "t1"):
                continue
            d = to_et(bars.get(s))
            if d.empty:
                continue
            after = d[(d["day"] == t_et.date()) & (d["m"] > tr["m"]) & (d["m"] < 16 * 60)]
            for ev in advance(tr, after):
                msg = {"fill": f"📥 {s}: se llenó la límite a {tr['entry']:.2f}. Pon el stop en {tr['stop']:.2f}.",
                       "expired": f"⌛ {s}: la límite {tr['entry']:.2f} no se llenó en {LIMIT_MIN} min. Cancelada, no persigas.",
                       "stop": f"🛑 {s} perdió el stop {tr['stop']:.2f}. Sal.",
                       "t1": f"✅ {s} tocó +2% ({tr['t1']:.2f}). Asegura: sube el stop a la entrada {tr['entry']:.2f}.",
                       "t2": f"🎯 {s} tocó +5% ({tr['t2']:.2f}). Objetivo cumplido.",
                       "be": None}.get(ev)
                if msg:
                    self.tg(f"{ev}:{s}", msg)
            self._move_cursor(tr, after)
            # Respaldo por reloj si no llegan velas; con margen por el retraso de Yahoo: la vela que la llenaba a tiempo
            # puede llegar 1–2 min después de vencer (el vencimiento exacto lo marca advance con la hora de cada vela)
            if tr["status"] == "pendiente" and now_m > (tr.get("valid_m") or 0) + PARTIAL_BARS + 1:
                tr["status"] = "no ejecutada"
        if now_m >= 15 * 60 + 50:
            open_ = [s for s, tr in self.trades.items() if tr["status"] in ("abierta", "t1")]
            open_ += [s for s, o in self.arms.items() if o.get("status") in ("abierta", "t1") and s not in open_]
            if open_:
                self.tg(f"close:{day}", "⏰ Cierra lo intradía antes de las 16:00: " + ", ".join(open_))

    def stats(self) -> dict:
        tr = list(self.trades.values())
        filled = [x for x in tr if x["status"] not in ("pendiente", "no ejecutada")]
        return {"n": len(tr), "filled": len(filled), "t1": sum(1 for x in tr if x["hit1"]),
                "t2": sum(1 for x in tr if x["status"] == "t2"), "stop": sum(1 for x in tr if x["status"] == "stop"),
                "open": sum(1 for x in tr if x["status"] in ("abierta", "t1")),
                "expired": sum(1 for x in tr if x["status"] == "no ejecutada"),
                "lag": _avg([x.get("lag_min") for x in tr]), "slip": _avg([x.get("slip") for x in tr], 2)}

    def arm_stats(self) -> dict:
        """Resultados de las compras stop de los avisos ARMA (la vía rápida), para compararlas con la COMPRA."""
        a = list(self.arms.values())
        return {"n": len(a), "filled": sum(1 for x in a if x.get("status") in FILLED),
                "t1": sum(1 for x in a if x.get("hit1")), "t2": sum(1 for x in a if x.get("status") == "t2"),
                "stop": sum(1 for x in a if x.get("status") == "stop"),
                "open": sum(1 for x in a if x.get("status") in ("abierta", "t1")),
                "pending": sum(1 for x in a if x.get("status") == "pendiente"),
                "cancelled": sum(1 for x in a if x.get("status") in ("cancelada", "no ejecutada"))}

    def _eod(self, t_et):
        if t_et.weekday() >= 5 or t_et.hour < 16 or not (self.trades or self.arms):
            return
        day = t_et.date().isoformat()
        changed = False
        for x in list(self.trades.values()) + list(self.arms.values()):
            if x["status"] == "pendiente":
                x["status"], changed = "no ejecutada", True
            elif x["status"] in ("abierta", "t1"):
                # intradía: lo que siga abierto se cierra al precio de cierre
                x["close_pct"] = round(100 * (float(x.get("last") or x["entry"]) / x["entry"] - 1), 2)
                x["status"], changed = "cierre", True
        if changed:
            self._archive()
            self._save()
        st = self.stats()
        lines = [f"Resumen {day}: {st['n']} COMPRA · {st['filled']} ejecutadas · {st['t1']} tocaron +2% · {st['t2']} +5% · {st['stop']} stop"]
        if st.get("lag") is not None:
            lines.append(f"La COMPRA llegó {st['lag']} min después de la ruptura en promedio, {st['slip']:+.2f}% sobre el nivel")
        for x in self.trades.values():
            fin = f" {x['close_pct']:+.1f}%" if x.get("close_pct") is not None else ""
            lines.append(f"{x['t']} {x['time']} · máx {x['mfe']:+.1f}% · mín {x['mae']:+.1f}% · {x['status']}{fin}")
        if self.arms:
            a = self.arm_stats()
            lines.append(f"Armadas (compra stop en la ruptura): {a['n']} · {a['filled']} se activaron · {a['t1']} tocaron +2% · "
                         f"{a['t2']} +5% · {a['stop']} stop · {a['cancelled']} canceladas")
            for x in self.arms.values():
                fin = f" {x['close_pct']:+.1f}%" if x.get("close_pct") is not None else ""
                lines.append(f"⚡ {x['t']} {x['time']} · máx {x['mfe']:+.1f}% · mín {x['mae']:+.1f}% · {x['status']}{fin}")
        if self.early:
            ok = sum(1 for s in list(self.early) if s in self.trades)
            lines.append(f"Avisos tempranos con velas de IBKR: {len(self.early)} · {ok} los confirmó el semáforo después")
        self.tg(f"eod:{day}", "\n".join(lines))
        path = os.path.join(STATE_DIR, f"log-{day}.json")
        if not os.path.exists(path):
            write_json(path, list(self.trades.values()))

    # ---------------- replay: la sesión de hoy minuto a minuto con las reglas actuales ----------------
    def replay(self, step: int = 10) -> dict:
        from datetime import datetime as _dt
        bars, day = dict(self.last_bars), self.day
        if not bars or not day:
            return {"status": "sin datos todavía"}
        now_et = _dt.now(timezone.utc).astimezone(ET)
        now_m = now_et.hour * 60 + now_et.minute if now_et.date() == day else 16 * 60
        et = {s: to_et(df) for s, df in bars.items()}
        news = {k[1]: v for k, v in list(self.cache.d.items()) if k[0] == "news"}  # el hilo de contexto la escribe

        def cut(s, m):
            d = et.get(s)
            return None if d is None or d.empty else d[(d["day"] == day) & (d["m"] <= m)]

        trades, taken = [], set()
        for m in range(OPEN_M + OR_MINUTES, min(ENTRY_END_M, now_m), step):
            t = _dt(day.year, day.month, day.day, m // 60, m % 60, tzinfo=ET)
            spy = session_metrics(cut("SPY", m), self.prev.get("SPY"), self.baseline.get("SPY"), t)
            qqq = session_metrics(cut("QQQ", m), self.prev.get("QQQ"), self.baseline.get("QQQ"), t)
            reg, _ = regime(spy, qqq)
            for s in self.universe:
                if s in taken or s not in et:
                    continue
                mm = session_metrics(cut(s, m), self.prev.get(s), self.baseline.get(s), t)
                ctx = {"phase": "open", "regime": reg, "spy_chg": spy.get("chg"), "atr": self.atr.get(s)}
                nv = news.get(s)
                if nv:
                    fetched, cat = nv
                    ctx["news_ok"] = cat.get("n", 0) > 0
                    if cat.get("type") and fetched - 3600 * (cat.get("hours") or 0) <= t.timestamp():
                        ctx["cat"] = {"type": cat["type"], "age": cat.get("age")}
                    ctx["offer"] = cat.get("offer")
                r = decide(s, mm, ctx)
                if r["decision"] == "COMPRA":
                    taken.add(s)
                    tr = new_trade(r, day.isoformat(), t.strftime("%H:%M"), m)
                    tr["why"] = r["why"]
                    trades.append(tr)
        for tr in trades:
            d = et[tr["t"]]
            after = d[(d["day"] == day) & (d["m"] > tr["m"]) & (d["m"] < 16 * 60)]
            advance(tr, after)
            if tr["status"] == "pendiente":
                tr["status"] = "no ejecutada"
            last = float(after["Close"].iloc[-1]) if not after.empty else tr["entry"]
            tr["result"] = {"t2": "+5%", "t1": "+2%", "t1-cerrada": "+2%"}.get(tr["status"], tr["status"])
            tr["last"] = round(100 * (last / tr["entry"] - 1), 2)
        n = len(trades)
        summ = {"n": n, "ejecutadas": sum(1 for x in trades if x["result"] != "no ejecutada"),
                "t1": sum(1 for x in trades if x["result"] in ("+2%", "+5%")),
                "t2": sum(1 for x in trades if x["result"] == "+5%"), "stop": sum(1 for x in trades if x["result"] == "stop"),
                "abiertas": sum(1 for x in trades if x["result"] == "abierta"),
                "no_ejecutadas": sum(1 for x in trades if x["result"] == "no ejecutada")}
        return {"status": "ok", "day": day.isoformat(), "hasta": f"{min(now_m, LAST_ENTRY_M) // 60}:{min(now_m, LAST_ENTRY_M) % 60:02d}",
                "paso_min": step, "universo": len(self.universe), "resumen": summ, "trades": trades}

    def replay_async(self, step: int = 10, fresh: bool = False) -> dict:
        """Corre el replay en otro hilo. Uno a la vez y, recalculado, como mucho cada REPLAY_MIN_S: compite con el ciclo
        por CPU y memoria, y /api/replay?fresh=1 es público (antes se podía relanzar en bucle)."""
        st = self.replay_state
        if st.get("status") == "corriendo" or (st.get("status") == "ok" and not fresh):
            return st
        wait = REPLAY_MIN_S if st.get("status") == "ok" else 60  # sin datos o con error: se puede reintentar al minuto
        if self.replay_ts and time.time() - self.replay_ts < wait:
            return {**st, "nota": f"recalculable cada {wait // 60} min"}
        self.replay_state = {"status": "corriendo", "desde": datetime.now(timezone.utc).isoformat()}

        def go():
            try:
                self.replay_state = self.replay(step)
            except Exception as e:  # noqa: BLE001
                self.replay_state = {"status": "error", "error": f"{type(e).__name__}: {e}"}
            finally:
                self.replay_ts = time.time()
        threading.Thread(target=go, name="replay", daemon=True).start()
        return self.replay_state

    # ---------------- bucle ----------------
    def after_cycle(self):
        """Después de cada ciclo: suelta los hilos de descarga terminados, devuelve memoria al sistema y la mide.
        Si pasa de MEM_ALERT_PCT avisa una vez por día: Render reinicia el servicio al llegar a 512 MB."""
        yahoo.prune_tasks()
        self.mem = memory.trim()
        self.snapshot["mem"] = self.mem
        self.errors = self.errors[-50:]
        p = self.mem.get("pct")
        if p is not None and p >= memory.ALERT_PCT:
            day = datetime.now(timezone.utc).astimezone(ET).date().isoformat()
            self.tg(f"mem:{day}", f"⚠ Semáforo con la memoria al {p:.0f}% ({self.mem['rss_mb']:.0f} MB de "
                                  f"{memory.LIMIT_MB:.0f}). Si llega al 100% Render lo reinicia y las señales se atrasan.")

    def run_forever(self):
        try:
            self.hello()
        except Exception as e:  # noqa: BLE001
            log.warning("aviso de arranque: %s", type(e).__name__)
        while True:
            t0 = time.time()
            try:
                with self.lock:
                    self.cycle()
            except Exception as e:  # noqa: BLE001
                msg = f"{datetime.now(timezone.utc).isoformat()} {type(e).__name__}: {e}"
                log.error("ciclo falló: %s\n%s", msg, traceback.format_exc())
                self.errors.append(msg)
                self.snapshot = {**self.snapshot, "status": "error", "errors": self.errors[-5:]}
            self.cycle_s = round(time.time() - t0, 1)
            self.snapshot["cycle_s"] = self.cycle_s
            if self.cycle_s > CYCLE_S:
                log.warning("el ciclo tardó %.0f s (más que CYCLE_S=%d)", self.cycle_s, CYCLE_S)
            try:
                self.after_cycle()
            except Exception as e:  # noqa: BLE001
                log.warning("memoria: %s", e)
            phase = self.snapshot.get("phase") or phase_of(datetime.now(timezone.utc))
            wait = CYCLE_S if phase in ("open", "late") else 180 if phase == "pre" else 300
            # Espera su turno, salvo que el vigía rápido vea romper una acción armada: entonces corre ya.
            self.wake.wait(max(5, wait - (time.time() - t0)))
            self.wake.clear()
