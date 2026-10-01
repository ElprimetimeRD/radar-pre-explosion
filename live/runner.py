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
from .decide import CAT_NAME, ENTRY_END_M, LAST_ENTRY_M, LIMIT_VALID_MIN as LIMIT_MIN, RISK_MAX, T2, decide, regime
from .metrics import OPEN_M, OR_MINUTES, atr_pct, baseline_curve, session_metrics, to_et
from .positions import Positions

UNIVERSE_N = int(os.environ.get("UNIVERSE_N", "50"))
CYCLE_S = int(os.environ.get("CYCLE_S", "60"))
UNIVERSE_TTL = int(os.environ.get("UNIVERSE_TTL", "180"))  # s entre refrescos del universo en sesión (pre-market: 600)
FAST_S = int(os.environ.get("FAST_S", "15"))         # s entre vistazos del vigía rápido a las acciones armadas
ARM_MIN = int(os.environ.get("ARM_MIN", "55"))        # fuerza mínima para armar (60 con mercado amarillo): la ruptura suma 5
ARM_NEAR = float(os.environ.get("ARM_NEAR", "1.0"))   # % máximo debajo del gatillo para armarla
ARM_MAX = int(os.environ.get("ARM_MAX", "8"))         # avisos de "arma" por día (no saturar Telegram)
ENRICH_N = 20
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
    se cancela; si abre por encima del límite (cap) y no vuelve a él, no se llena (no se persigue)."""
    ev = []
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
        tr["mfe"] = max(tr["mfe"], round(100 * (hi / tr["entry"] - 1), 2))
        tr["mae"] = min(tr["mae"], round(100 * (lo / tr["entry"] - 1), 2))
        if not tr["hit1"] and lo <= tr["stop"]:
            tr["status"] = "stop"
            ev.append("stop")
            break
        just_hit = False
        if not tr["hit1"] and hi >= tr["t1"]:
            tr["hit1"], tr["status"], just_hit = True, "t1", True
            ev.append("t1")
        if tr["hit1"] and hi >= tr["t2"]:
            tr["status"] = "t2"
            ev.append("t2")
            break
        if tr["hit1"] and lo <= tr["entry"] and not just_hit:
            tr["status"] = "t1-cerrada"
            ev.append("be")
            break
    return ev


def new_trade(r: dict, day: str, t_str: str, m: int) -> dict:
    p = r["plan"]
    lim = bool(p.get("limit"))
    return {"t": r["t"], "day": day, "time": t_str, "m": m, "entry": p["entry"], "stop": p["stop"], "t1": p["t1"],
            "t2": p["t2"], "risk": p.get("risk"), "score": r["score"], "limit": lim,
            "valid_m": m + (p.get("valid_min") or 10) if lim else None,
            "status": "pendiente" if lim else "abierta", "hit1": False, "mfe": 0.0, "mae": 0.0, "last": p["entry"]}


def new_arm(t: str, a: dict, day: str | None, t_str: str, m: int, valid_m: int) -> dict:
    """La compra stop que pide el aviso 🟡 ARMA, como orden virtual: así se mide si entrar en la ruptura (rápido)
    rinde mejor que esperar la COMPRA confirmada."""
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


class TTLCache:
    def __init__(self):
        self.d: dict = {}

    def get(self, key, ttl):
        v = self.d.get(key)
        return v[1] if v and time.time() - v[0] < ttl else None

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
        self.tg_lock = threading.Lock()
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
    def tg(self, key: str, text: str):
        with self.tg_lock:  # el ciclo y el vigía rápido avisan desde hilos distintos
            if key in self.sent:
                return
            self.sent.add(key)
        tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        log.info("ALERTA %s", text.replace("\n", " | "))
        if not (self.notify and tok and chat):
            return
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=15)
        except requests.RequestException as e:
            log.warning("Telegram: %s", e)

    def quotes(self, syms: list[str]) -> dict[str, dict]:
        """v7/quote con cortacircuito: si Yahoo niega el crumb (401), no insistir por 20 min."""
        if not syms or time.time() < self.q_off_until:
            return {}
        first = yahoo.batch_quotes(syms[:40])
        if not first:
            self.q_off_until = time.time() + 1200
            log.warning("v7/quote no disponible; sigo con precios de velas por 20 min")
            return {}
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
        uni = list(dict.fromkeys(top + watch + list(halted)))
        self.universe = uni
        self.sources = {s: sorted(cand.get(s, [])) for s in uni}
        self.universe_ts = time.time()
        log.info("universo: %d candidatos, %d cotizados → %d en seguimiento", len(cand), len(quotes), len(uni))

    def ensure_context(self, syms: list[str], today):
        need = [s for s in syms if s not in self.baseline]
        if not need:
            return
        h5 = yahoo.history(need, period="5d", interval="1m", prepost=False)
        hd = yahoo.history(need, period="3mo", interval="1d")
        for s in need:
            self.baseline[s] = baseline_curve(h5.get(s), today)
            d = hd.get(s)
            if d is not None and not d.empty:
                d = d.dropna(subset=["Close"])
                d = d[[ix.date() < today for ix in d.index]]
            self.atr[s] = atr_pct(d)
            self.prev[s] = float(d["Close"].iloc[-1]) if d is not None and not d.empty else None

    # ---------------- enriquecimiento con caché ----------------
    def news_ctx(self, s, now_ts, name=None):
        c = self.cache.get(("news", s), 600)
        if c is None:
            c = self.cache.put(("news", s), classify(yahoo.news(s, count=10) or rss_news(s, name), now_ts, s, name))
        return c

    def sec_ctx(self, s, today):
        c = self.cache.get(("sec", s), 6 * 3600)
        if c is None:
            if not self.sec_map:
                self.sec_map = other.sec_ticker_map()
            cik = self.sec_map.get(s)
            c = self.cache.put(("sec", s), other.filing_flags(other.sec_submissions(cik), today) if cik else {})
        return c

    def opt_ctx(self, s, px):
        c = self.cache.get(("opt", s), 900)
        if c is None:
            chains, _ = yahoo.option_chains(s, max_days=30)
            c = self.cache.put(("opt", s), options_features(chains, px).get("callVolOI"))
        return c

    # ---------------- ciclo ----------------
    def cycle(self, now: datetime | None = None):
        now = now or datetime.now(timezone.utc)
        t_et = now.astimezone(ET)
        today = t_et.date()
        phase = phase_of(now)
        if self.day != today:
            self.day, self.baseline, self.atr, self.prev = today, {}, {}, {}
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
            self.armed = {}
            self.snapshot = {**self.snapshot, "phase": phase, "status": "mercado cerrado", "ts": now.isoformat(),
                             "trades": list(self.trades.values()), "stats": self.stats(), "armed": [], "watching": [],
                             "armadas": list(self.arms.values()), "armStats": self.arm_stats()}
            return
        try:  # el monitor de posiciones va antes de lo pesado: si el escaneo falla, los avisos de tus posiciones siguen
            self._watch_positions(now, phase)
        except Exception as e:  # noqa: BLE001
            log.warning("monitor de posiciones: %s", e)
        halted = halts_src.parse(halts_src.fetch(), now)
        if not self.universe or time.time() - self.universe_ts > (UNIVERSE_TTL if phase == "open" else 600):
            self.refresh_universe(now, phase, halted)
        # Las señales y órdenes armadas abiertas se siguen aunque su acción salga del universo (antes, SITC dejó de
        # seguirse a los 6 minutos de su COMPRA del 1-oct: sin avisos de stop ni de objetivo).
        active = [s for s, x in list(self.trades.items()) + list(self.arms.items())
                  if x.get("status") in ("pendiente", "abierta", "t1")]
        syms = list(dict.fromkeys(self.universe + active + ["SPY", "QQQ"]))
        self.ensure_context(syms, today)
        bars = yahoo.history(syms, period="1d", interval="1m", prepost=True)
        quotes = self.quotes(syms)
        self.last_bars = bars

        def mets(s):
            q = quotes.get(s, {})
            prev = fnum(q.get("regularMarketPreviousClose")) or self.prev.get(s)
            live = fnum(q.get("preMarketPrice")) if phase == "pre" else fnum(q.get("regularMarketPrice"))
            return session_metrics(bars.get(s), prev, self.baseline.get(s), now, live)

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
        # Enriquecer a los más prometedores (noticias, SEC, opciones) y decidir de nuevo
        order = sorted(base, key=lambda s: (base[s][2]["decision"] != "NO", base[s][2]["score"]), reverse=True)
        now_ts = now.timestamp()
        for i, s in enumerate(order[:ENRICH_N]):
            m, ctx, _ = base[s]
            try:
                cat = self.news_ctx(s, now_ts, ctx.get("name"))
                if cat.get("type"):
                    ctx["cat"] = {k: cat.get(k) for k in ("type", "age", "title", "url", "hours")}
                ctx["offer"] = cat.get("offer")
                ctx["news_ok"] = cat.get("n", 0) > 0
                f = self.sec_ctx(s, today)
                ctx["offer30"], ctx["shelf"] = f.get("offer30"), f.get("shelf36m")
                if i < 10 and (m.get("px") or 0) >= 3:
                    ctx["callVolOI"] = self.opt_ctx(s, m.get("px"))
            except Exception as e:  # noqa: BLE001
                log.warning("enriquecer %s: %s", s, e)
            base[s] = (m, ctx, decide(s, m, ctx))
        rows = [base[s][2] for s in base]
        for r in rows:
            r["src"] = self.sources.get(r["t"], [])
        rank = {"COMPRA": 0, "ESPERA": 1, "NO": 2}
        rows.sort(key=lambda r: (rank[r["decision"]], -((r["chg"] or 0) if phase == "pre" else r["score"])))
        self._track(rows, bars, t_et, reg_txt)
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
            "halts": {s: h for s, h in halted.items()}, "errors": self.errors[-5:],
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
                if not after.empty:
                    o["last"] = float(after["Close"].iloc[-1])
                    o["m"] = int(after["m"].iloc[-1])
            if o["status"] != "pendiente":
                continue
            r = by_t.get(s)
            if r is not None and r["decision"] == "NO":
                o["status"], o["cancel"] = "cancelada", r["reason"]
                self.tg(f"arm-x:{s}", f"❌ {s}: cancela la compra stop {o['entry']:.2f}. {r['reason']}.")
            elif now_m >= (o.get("valid_m") or 10**9):
                o["status"] = "no ejecutada"
                self.tg(f"arm-x:{s}", f"⌛ {s}: la compra stop {o['entry']:.2f} no se activó a tiempo. Cancélala.")

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

    # ---------------- vigía rápido: precio de las rupturas armadas cada FAST_S segundos ----------------
    def fast_once(self, now: datetime | None = None) -> list[str]:
        """Si una acción armada cruza su gatillo, avisa al instante y despierta el ciclo completo para que confirme la
        COMPRA sin esperar su turno. Solo usa la cotización v7 (las descargas de velas de yfinance no son seguras en
        paralelo con las del ciclo); si Yahoo la niega, el vigía espera y el ciclo de 1 min sigue como siempre. Un fallo
        aquí no activa el cortacircuito de cotizaciones del ciclo."""
        now = now or datetime.now(timezone.utc)
        armed = dict(self.armed)
        fired: list[str] = []
        if phase_of(now) == "open" and armed and time.time() >= self.q_off_until:
            for s, q in (yahoo.batch_quotes(sorted(armed)) or {}).items():
                a, p = armed.get(s), fnum(q.get("regularMarketPrice"))
                if not a or not p or p < a["level"] * 1.001:
                    continue
                key = f"break:{s}:{a['level']:.2f}"
                if key in self.sent:
                    continue
                if p <= a["entry"] * 1.004:
                    msg = (f"⚡ {s} rompe {a['level']:.2f} ahora ({p:.2f}). Entrada ≤ {a['entry'] * 1.003:.2f} · "
                           f"stop {a['stop']:.2f} (−{a['risk']:.1f}%) · +2%: {a['t1']:.2f}\n"
                           f"Confirma volumen en tu gráfico; el semáforo lo reevalúa ya.")
                else:
                    msg = (f"⚡ {s} rompió {a['level']:.2f} y ya va en {p:.2f}: no persigas. Si dejaste la orden armada, ya "
                           f"entraste; si no, espera el retesteo que marque el semáforo.")
                self.tg(key, msg)
                fired.append(s)
        if fired:
            self.wake.set()
        self.fast_state = {"ts": now.isoformat(), "armed": len(armed), "fired": fired}
        return fired

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
            why = ", ".join(r["why"][:3])
            if tr["limit"]:
                vm = tr["valid_m"]
                how = f"orden LÍMITE {tr['entry']:.2f} válida hasta {vm // 60}:{vm % 60:02d} ET (no persigas {r['px']:.2f})"
            else:
                how = f"a {tr['entry']:.2f}, no pagues más de {tr['entry'] * 1.003:.2f}"
            self.tg(f"buy:{r['t']}", (
                f"🟢 COMPRA {r['t']} {how}\nFuerza {r['score']}/100 · {why}\n"
                f"Stop {tr['stop']:.2f} (−{tr['risk']:.1f}%) · +2%: {tr['t1']:.2f} · +5%: {tr['t2']:.2f}\nMercado: {reg_txt}{late}\n"
                f"Para operarla dime: «ejecuta {r['t']} $monto, vender a +3%»"))
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
            if not after.empty:
                tr["last"] = float(after["Close"].iloc[-1])
                tr["m"] = int(after["m"].iloc[-1])
            if tr["status"] == "pendiente" and now_m > (tr.get("valid_m") or 0):
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
        news = {k[1]: v for k, v in self.cache.d.items() if k[0] == "news"}

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
        st = self.replay_state
        if st.get("status") == "corriendo" or (st.get("status") == "ok" and not fresh):
            return st
        self.replay_state = {"status": "corriendo", "desde": datetime.now(timezone.utc).isoformat()}

        def go():
            try:
                self.replay_state = self.replay(step)
            except Exception as e:  # noqa: BLE001
                self.replay_state = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        threading.Thread(target=go, daemon=True).start()
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
            try:
                self.after_cycle()
            except Exception as e:  # noqa: BLE001
                log.warning("memoria: %s", e)
            phase = self.snapshot.get("phase") or phase_of(datetime.now(timezone.utc))
            wait = CYCLE_S if phase in ("open", "late") else 180 if phase == "pre" else 300
            # Espera su turno, salvo que el vigía rápido vea romper una acción armada: entonces corre ya.
            self.wake.wait(max(5, wait - (time.time() - t0)))
            self.wake.clear()
