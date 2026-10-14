"""Puente IBKR: lo que manda el programa bridge/puente_ibkr.py desde la PC de Priamo (escáneres de IBKR, precios al
instante de las acciones armadas y, desde la versión 1.2 del puente, el flujo REAL de las acciones que el semáforo le
pide seguir: cada operación clasificada como compra o venta contra el bid/ask de ese momento, sumada por minuto).
Los precios solo se usan para decidir y avisar antes: la página y la API públicas no los muestran (los datos de IBKR
son para uso personal). Del flujo se publican solo totales (dinero comprador y vendedor, órdenes grandes)."""
from __future__ import annotations

import math
import re
import statistics
import threading
import time
from array import array
from datetime import datetime

from scanner.util import ET

SYM = re.compile(r"[A-Z]{1,5}")
SCAN_TTL = 180   # s: un escaneo más viejo ya no alimenta el universo
QUOTE_TTL = 10   # s: un precio más viejo ya no cuenta como "al instante"
ON_TTL = 60      # s sin noticias del puente = desconectado
MAX_CODES, MAX_ROWS, MAX_QUOTES = 10, 50, 100
MAX_KEEP = 300   # precios guardados como máximo (los más viejos se descartan)
FLOW_TTL = 180   # s: sin flujo ni precio nuevo de una acción en este tiempo, su flujo ya no cuenta como "en vivo"
FLOW_KEEP = 1200  # minutos de flujo guardados por acción (20 h)
MAX_FLOW_SYMS = 150
MAX_FLOW_RECS = 3000  # registros de flujo por envío, como máximo
NCOL = 17        # columnas de un registro de flujo (FLOW_COLS en bridge/puente_ibkr.py: m o h l c v n buy sell mid
#                  bbuy bsell nbb nbs top tside u)
RECENT = 15      # minutos que cuentan como "ahora" (igual que el radar de flujo)
UNIT_MIN_N = 3   # acciones mínimas para confirmar la unidad de tamaño de IBKR contra el volumen de Yahoo


def _ratio(a: float, b: float) -> float | None:
    """a ÷ b con tope 99 (sin entradas ni salidas queda None). Igual que en live/flujo.py."""
    if b > 0:
        return min(round(a / b, 2), 99.0)
    return 99.0 if a > 0 else None


def _hhmm(m: int) -> str:
    return datetime.fromtimestamp(m * 60, ET).strftime("%H:%M")


def _flow_row(row, lo_m: int, hi_m: int) -> tuple[int, array] | None:
    """Un registro de flujo válido (minuto dentro de [lo_m, hi_m], precios positivos, montos no negativos) o None."""
    if not isinstance(row, (list, tuple)) or len(row) != NCOL:
        return None
    try:
        vals = [float(x) for x in row]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(x) for x in vals):
        return None
    m = int(vals[0])
    if m != vals[0] or not lo_m <= m <= hi_m or min(vals[1:5]) <= 0 or min(vals[5:14]) < 0 or vals[14] < 0:
        return None
    if vals[15] not in (-1.0, 0.0, 1.0) or vals[16] not in (1.0, 100.0):
        return None
    return m, array("d", vals[1:])


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _val(v):
    """Valor de `info` apto para JSON estricto: números finitos, booleanos, None o texto corto."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v if math.isfinite(v) else None
    return str(v)[:200]


def _sym(s) -> str | None:
    s = str(s or "").upper().strip()
    return s if SYM.fullmatch(s) else None


class Bridge:
    def __init__(self):
        self.lock = threading.Lock()
        self.scan: dict[str, list[str]] = {}
        self.scan_ts = 0.0
        self.quotes: dict[str, dict] = {}
        self.seen = 0.0
        self.info: dict = {}
        self.flow: dict[str, dict[int, array]] = {}   # ticker -> minuto -> registro (sin la columna m)
        self.flow_seen: dict[str, float] = {}           # ticker -> cuándo llegó su último flujo
        self.unit: float | None = None                  # acciones por unidad de tamaño, confirmada con Yahoo
        self.unit_ratio: float | None = None
        self.unit_n = 0

    def update(self, scan: dict | None = None, quotes: dict | None = None, info: dict | None = None,
               now: float | None = None, flow: dict | None = None) -> list[str]:
        """Guarda lo recibido (con tope de tamaño) y devuelve los tickers que traen precio nuevo. La hora de cada
        precio es la de llegada al servidor: así no importa si el reloj de la PC está corrido."""
        now = now or time.time()
        fresh = []
        if isinstance(flow, dict):
            self._store_flow(flow, now)
        with self.lock:
            self.seen = now
            if isinstance(info, dict):
                self.info = {str(k)[:20]: _val(v) for k, v in list(info.items())[:20]}
            if isinstance(scan, dict):
                clean = {}
                for code, syms in list(scan.items())[:MAX_CODES]:
                    ok = [s for s in (_sym(x) for x in list(syms or [])[:MAX_ROWS]) if s]
                    clean[str(code)[:40]] = list(dict.fromkeys(ok))
                self.scan, self.scan_ts = clean, now
            for s, q in list((quotes or {}).items())[:MAX_QUOTES]:
                s = _sym(s)
                last = _num(q.get("last")) if s and isinstance(q, dict) else None
                if last is None:
                    continue
                self.quotes[s] = {"last": last, "high": _num(q.get("high")), "bid": _num(q.get("bid")),
                                  "ask": _num(q.get("ask")), "vol": _num(q.get("vol")), "ts": now}
                fresh.append(s)
            old = [s for s, q in self.quotes.items() if now - q["ts"] > 3600]
            if len(self.quotes) - len(old) > MAX_KEEP:  # memoria acotada aunque lleguen muchos tickers distintos
                old += sorted(self.quotes, key=lambda s: self.quotes[s]["ts"])[:len(self.quotes) - MAX_KEEP]
            for s in set(old):
                del self.quotes[s]
        return fresh

    def _store_flow(self, flow: dict, now: float):
        """Guarda los minutos de flujo recibidos (un minuto que llega de nuevo reemplaza al anterior: el puente manda
        el minuto completo cada vez que cambia). Minutos de más de 20 h o del futuro se ignoran."""
        now_m = int(now // 60)
        lo, hi = now_m - FLOW_KEEP, now_m + 3
        budget = MAX_FLOW_RECS
        with self.lock:
            for s, rows in list(flow.items())[:MAX_FLOW_SYMS]:
                s = _sym(s)
                if not s or not isinstance(rows, list):
                    continue
                got = []
                for row in rows[:budget]:
                    r = _flow_row(row, lo, hi)
                    if r:
                        got.append(r)
                budget -= len(rows)
                if not got:
                    continue
                recs = self.flow.setdefault(s, {})
                recs.update(got)
                if len(recs) > FLOW_KEEP + 60:
                    for m in [m for m in recs if m < lo]:
                        del recs[m]
                self.flow_seen[s] = now
                if budget <= 0:
                    break
            if len(self.flow) > MAX_FLOW_SYMS:  # memoria acotada: se sueltan las que hace más tiempo no traen flujo
                for s in sorted(self.flow, key=lambda x: self.flow_seen.get(x, 0))[: len(self.flow) - MAX_FLOW_SYMS]:
                    self.flow.pop(s, None)
                    self.flow_seen.pop(s, None)

    def eff_unit(self) -> float:
        """Acciones por unidad de tamaño: la confirmada con Yahoo; si aún no, la que dice el puente; si no, 1."""
        if self.unit:
            return self.unit
        u = _num(self.info.get("unit"))
        return u if u in (1.0, 100.0) else 1.0

    def calibrate(self, yahoo_vol: dict, now: float | None = None) -> float | None:
        """Confirma en qué unidad manda IBKR los tamaños (acciones o lotes de 100: depende de un ajuste de la API en
        IB Gateway) comparando el volumen del día del puente con el de Yahoo en las mismas acciones. La razón queda
        cerca de 1 (o algo más, por el pre-market) con acciones y cerca de 0.01 con lotes: entre medio no se decide."""
        now = now or time.time()
        ratios = []
        with self.lock:
            for s, yv in yahoo_vol.items():
                q = self.quotes.get(s)
                yv = _num(yv)
                if q and q.get("vol") and yv and yv >= 50_000 and now - q["ts"] <= 120:
                    ratios.append(q["vol"] / yv)
        if len(ratios) < UNIT_MIN_N:
            return self.unit
        med = statistics.median(ratios)
        u = 1.0 if 0.3 <= med <= 30 else 100.0 if 0.003 <= med <= 0.08 else None
        if u:
            self.unit, self.unit_ratio, self.unit_n = u, round(med, 3), len(ratios)
        return self.unit

    def quote(self, s: str, now: float | None = None, ttl: float = QUOTE_TTL) -> dict | None:
        """Último precio, bid y ask de IBKR si están frescos (para el spread y la decisión; no se publican)."""
        now = now or time.time()
        with self.lock:
            q = self.quotes.get(s)
        if not q or now - q["ts"] > ttl:
            return None
        return {"last": q["last"], "bid": q["bid"], "ask": q["ask"]}

    def flow_stats(self, s: str, now: float | None = None, recent: int = RECENT) -> dict | None:
        """Flujo REAL de hoy (pre-market incluido) de una acción, con las mismas claves que el estimado de las velas
        (live/flujo.py metrics()["flow"]) para usarlo en su lugar. None si el puente no la está siguiendo ahora
        (sin flujo ni precio frescos): entonces vale el estimado. Las órdenes grandes solo cuentan en los minutos en que
        el puente ya usaba la unidad de tamaño correcta."""
        now = now or time.time()
        now_m = int(now // 60)
        t = datetime.fromtimestamp(now, ET)
        day0 = int(datetime(t.year, t.month, t.day, tzinfo=ET).timestamp() // 60)
        with self.lock:
            q = self.quotes.get(s)
            live = (now - self.flow_seen.get(s, -1e9) <= FLOW_TTL) or bool(q and now - q["ts"] <= 60)
            recs = sorted((m, r) for m, r in self.flow.get(s, {}).items() if day0 <= m <= now_m + 1)
        if not recs or not live:
            return None
        u = self.eff_unit()
        large = _num(self.info.get("large_usd")) or 25000.0
        tin = tout = rin = rout = lin = lout = vol = 0.0
        n = nb = ns = rb = rec_n = 0
        last_big = None
        cut = now_m - (recent - 1)
        for m, r in recs:
            _o, _h, _l, _c, v, nn, b, se, mi, bb, bs, nbb, nbs, top, ts, ru = r
            bi, so = (b + mi / 2) * u, (se + mi / 2) * u
            tin, tout, vol, n = tin + bi, tout + so, vol + v * u, n + int(nn)
            if m >= cut:
                rin, rout, rec_n = rin + bi, rout + so, rec_n + 1
            if ru == u and (nbb or nbs):
                lin, lout, nb, ns = lin + bb * u, lout + bs * u, nb + int(nbb), ns + int(nbs)
                if m >= cut:
                    rb += int(nbb)
                big = top * u if top * u >= large else max(bb, bs) * u
                side = ts if top * u >= large else (1 if bb >= bs else -1)
                last_big = {"t": _hhmm(m), "side": "buy" if side > 0 else "sell" if side < 0 else "absorb",
                            "usd": round(big), "x": round(big / large, 1)}
        total = tin + tout
        if total <= 0:
            return None
        return {"in": round(tin), "out": round(tout), "ratio": _ratio(tin, tout), "net": round(100 * (tin - tout) / total, 1),
                "in15": round(rin), "out15": round(rout), "ratio15": _ratio(rin, rout) if rec_n >= 3 else None,
                "large": {"n": nb + ns, "buy": nb, "sell": ns, "in": round(lin), "out": round(lout),
                          "ratio": _ratio(lin, lout), "share": round(100 * (lin + lout) / total, 1), "rec_buy": rb,
                          "last": last_big},
                "trades": n, "vol": round(vol), "usd": round(total), "desde": _hhmm(recs[0][0]),
                "covered": max(0, now_m - recs[0][0]), "src": "ibkr", "large_usd": large}

    def flowing(self, now: float | None = None) -> int:
        """Acciones con flujo de IBKR en vivo ahora."""
        now = now or time.time()
        with self.lock:
            return sum(1 for s in self.flow if now - self.flow_seen.get(s, -1e9) <= FLOW_TTL)

    def price(self, s: str, now: float | None = None, ttl: float = QUOTE_TTL) -> float | None:
        now = now or time.time()
        with self.lock:
            q = self.quotes.get(s)
        return q["last"] if q and now - q["ts"] <= ttl else None

    def scan_symbols(self, n: int, now: float | None = None) -> list[str]:
        """Los primeros n tickers de los escaneos frescos, intercalados: el 1.º de cada escaneo, luego el 2.º…"""
        now = now or time.time()
        if n <= 0:
            return []
        with self.lock:
            if not self.scan or now - self.scan_ts > SCAN_TTL:
                return []
            lists = list(self.scan.values())
        out: list[str] = []
        for i in range(MAX_ROWS):
            for lst in lists:
                if i < len(lst) and lst[i] not in out:
                    out.append(lst[i])
                    if len(out) >= n:
                        return out
        return out

    def on(self, now: float | None = None) -> bool:
        return bool(self.seen) and (now or time.time()) - self.seen <= ON_TTL

    def status(self, now: float | None = None) -> dict:
        """Estado para /health y la página: si está conectado y qué tan fresco, sin precios."""
        now = now or time.time()
        with self.lock:
            scan_ok = bool(self.scan) and now - self.scan_ts <= SCAN_TTL
            flowing = sum(1 for s in self.flow if now - self.flow_seen.get(s, -1e9) <= FLOW_TTL)
            return {"on": bool(self.seen) and now - self.seen <= ON_TTL,
                    "age_s": round(now - self.seen, 1) if self.seen else None,
                    "scan": {k: len(v) for k, v in self.scan.items()} if scan_ok else {},
                    "quotes": sum(1 for q in self.quotes.values() if now - q["ts"] <= QUOTE_TTL),
                    "ib": self.info.get("ib"), "error": self.info.get("error"), "ver": self.info.get("ver"),
                    "lines": self.info.get("lines"), "max_lines": self.info.get("max_lines"), "flow": flowing,
                    "unit": self.unit or self.info.get("unit"), "unit_ok": self.unit is not None,
                    "unit_ratio": self.unit_ratio}
