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
from .decide import CAT_NAME, ENTRY_END_M, LAST_ENTRY_M, LIMIT_VALID_MIN as LIMIT_MIN, decide, regime
from .metrics import OPEN_M, atr_pct, baseline_curve, session_metrics, to_et

UNIVERSE_N = int(os.environ.get("UNIVERSE_N", "50"))
CYCLE_S = int(os.environ.get("CYCLE_S", "60"))
UNIVERSE_TTL = 600
ENRICH_N = 20
STATE_DIR = os.path.join(DATA, "live")
DEFAULT_WATCH = ["MU", "SNDK", "MRVL", "ARM", "BE", "AXTI", "NVDA", "AMD", "SNXX", "MUU"]
EARN_RX = r"(earnings|quarterly results|Q[1-4] (results|revenue)|beats?|tops? (estimates|expectations)|raises?\b.{0,40}\b(guidance|outlook|forecast)|record revenue)"


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


def classify(items: list[dict], now_ts: float) -> dict:
    """Mejor catalizador de las últimas ~2 semanas + bandera de oferta."""
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
    """Avanza una señal con velas nuevas (DataFrame con m, High, Low). Devuelve eventos:
    fill, expired, stop, t1, t2, be. Estados: pendiente → abierta → t1 → t2 | t1-cerrada | stop | no ejecutada."""
    ev = []
    for _, b in bars.iterrows():
        hi, lo, m = float(b["High"]), float(b["Low"]), int(b["m"])
        if tr["status"] == "pendiente":
            if m > tr.get("valid_m", 10**9):
                tr["status"] = "no ejecutada"
                ev.append("expired")
                break
            if lo > tr["entry"]:
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
        self.sent: set[str] = set()
        self.snapshot: dict = {"status": "iniciando", "rows": []}
        self.last_bars: dict = {}
        self.replay_state: dict = {"status": "sin correr"}
        self.errors: list[str] = []
        self._load()

    # ---------------- persistencia (sobrevive reinicios dentro del día) ----------------
    def _state_path(self):
        return os.path.join(STATE_DIR, "state.json")

    def _load(self):
        s = read_json(self._state_path(), {}) or {}
        today = datetime.now(timezone.utc).astimezone(ET).date().isoformat()
        if s.get("day") == today:
            self.trades = s.get("trades", {})
            self.sent = set(s.get("sent", []))
            return
        # Contenedor nuevo (redeploy): recuperar las señales del día guardadas por GitHub Actions
        try:
            r = requests.get(f"{LOG_BASE}/{today}.json", timeout=10)
            if r.ok:
                for tr in (r.json().get("trades") or []):
                    self.trades[tr["t"]] = tr
                    self.sent.add(f"buy:{tr['t']}")
                    for ev, st in (("t1", "t1"), ("t2", "t2"), ("stop", "stop")):
                        if tr.get("hit1") and ev == "t1" or tr.get("status") == st:
                            self.sent.add(f"{ev}:{tr['t']}")
                log.info("recuperadas %d señales de hoy desde el registro", len(self.trades))
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning("no pude leer el registro del día: %s", e)

    def _save(self):
        day = datetime.now(timezone.utc).astimezone(ET).date().isoformat()
        write_json(self._state_path(), {"day": day, "trades": self.trades, "sent": sorted(self.sent)})

    # ---------------- Telegram ----------------
    def tg(self, key: str, text: str):
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
            c = self.cache.put(("news", s), classify(yahoo.news(s, count=10) or rss_news(s, name), now_ts))
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
            if self.trades and next(iter(self.trades.values())).get("day") != today.isoformat():
                self.trades, self.sent = {}, set()
        if phase == "closed":
            self._eod(t_et)
            self.snapshot = {**self.snapshot, "phase": phase, "status": "mercado cerrado", "ts": now.isoformat(),
                             "trades": list(self.trades.values()), "stats": self.stats()}
            return
        halted = halts_src.parse(halts_src.fetch(), now)
        if not self.universe or time.time() - self.universe_ts > UNIVERSE_TTL:
            self.refresh_universe(now, phase, halted)
        syms = list(dict.fromkeys(self.universe + ["SPY", "QQQ"]))
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
            b, a = fnum(q.get("bid")), fnum(q.get("ask"))
            spread = round(100 * (a - b) / ((a + b) / 2), 2) if b and a and a > b else None
            h = halted.get(s)
            ctx = {"phase": phase, "regime": reg, "spy_chg": spy.get("chg"), "spread": spread, "atr": self.atr.get(s),
                   "halted": h if h and not h["resumed"] else None, "name": q.get("shortName") or q.get("longName"),
                   "watch": s in watch}
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
        best = next((r for r in rows if r["decision"] == "COMPRA"), None) or next((r for r in rows if r["decision"] == "ESPERA"), None)
        self.snapshot = {
            "status": "ok", "ts": now.isoformat(), "et": t_et.strftime("%H:%M:%S"), "phase": phase,
            "regime": reg, "regimeText": reg_txt, "best": best["t"] if best else None, "rows": rows,
            "counts": {k: sum(1 for r in rows if r["decision"] == k) for k in rank},
            "trades": list(self.trades.values()), "stats": self.stats(), "universe": len(self.universe),
            "halts": {s: h for s, h in halted.items()}, "errors": self.errors[-5:],
        }
        self._save()

    # ---------------- seguimiento de señales y alertas ----------------
    def _track(self, rows, bars, t_et, reg_txt):
        now_m = t_et.hour * 60 + t_et.minute
        day = t_et.date().isoformat()
        for r in rows:
            if r["decision"] != "COMPRA" or r["t"] in self.trades:
                continue
            tr = self.trades[r["t"]] = new_trade(r, day, t_et.strftime("%H:%M"), now_m)
            why = ", ".join(r["why"][:3])
            if tr["limit"]:
                vm = tr["valid_m"]
                how = f"orden LÍMITE {tr['entry']:.2f} válida hasta {vm // 60}:{vm % 60:02d} ET (no persigas {r['px']:.2f})"
            else:
                how = f"a {tr['entry']:.2f}, no pagues más de {tr['entry'] * 1.003:.2f}"
            self.tg(f"buy:{r['t']}", (
                f"🟢 COMPRA {r['t']} {how}\nFuerza {r['score']}/100 · {why}\n"
                f"Stop {tr['stop']:.2f} (−{tr['risk']:.1f}%) · +2%: {tr['t1']:.2f} · +5%: {tr['t2']:.2f}\nMercado: {reg_txt}"))
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
            if open_:
                self.tg(f"close:{day}", "⏰ Cierra lo intradía antes de las 16:00: " + ", ".join(open_))

    def stats(self) -> dict:
        tr = list(self.trades.values())
        filled = [x for x in tr if x["status"] not in ("pendiente", "no ejecutada")]
        return {"n": len(tr), "filled": len(filled), "t1": sum(1 for x in tr if x["hit1"]),
                "t2": sum(1 for x in tr if x["status"] == "t2"), "stop": sum(1 for x in tr if x["status"] == "stop"),
                "open": sum(1 for x in tr if x["status"] in ("abierta", "t1")),
                "expired": sum(1 for x in tr if x["status"] == "no ejecutada")}

    def _eod(self, t_et):
        if t_et.weekday() >= 5 or t_et.hour < 16 or not self.trades:
            return
        day = t_et.date().isoformat()
        st = self.stats()
        lines = [f"Resumen {day}: {st['n']} COMPRA · {st['filled']} ejecutadas · {st['t1']} tocaron +2% · {st['t2']} +5% · {st['stop']} stop"]
        for x in self.trades.values():
            lines.append(f"{x['t']} {x['time']} · máx {x['mfe']:+.1f}% · mín {x['mae']:+.1f}% · {x['status']}")
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
        for m in range(OPEN_M + 15, min(ENTRY_END_M, now_m), step):
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
            phase = self.snapshot.get("phase") or phase_of(datetime.now(timezone.utc))
            wait = CYCLE_S if phase in ("open", "late") else 180 if phase == "pre" else 300
            time.sleep(max(5, wait - (time.time() - t0)))
