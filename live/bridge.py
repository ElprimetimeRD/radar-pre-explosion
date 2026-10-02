"""Puente IBKR: lo que manda el programa bridge/puente_ibkr.py desde la PC de Priamo (escáneres de IBKR, precios al
instante de las acciones armadas y velas de 1 min de las candidatas principales). Los precios y las velas solo se usan
para avisar antes por Telegram: la página y la API públicas no los muestran (los datos de IBKR son para uso personal)."""
from __future__ import annotations

import math
import re
import threading
import time

SYM = re.compile(r"[A-Z]{1,5}")
SCAN_TTL = 180   # s: un escaneo más viejo ya no alimenta el universo
QUOTE_TTL = 10   # s: un precio más viejo ya no cuenta como "al instante"
ON_TTL = 60      # s sin noticias del puente = desconectado
MAX_CODES, MAX_ROWS, MAX_QUOTES = 10, 50, 100
MAX_KEEP = 300   # precios guardados como máximo (los más viejos se descartan)
BARS_TTL = 30    # s: velas sin actualizar hace más que esto ya no sirven para el aviso temprano (IBKR las manda cada ~5 s)
MAX_BAR_SYMS = 40     # acciones con velas guardadas como máximo
MAX_BAR_ROWS = 1000   # velas por acción (un día con pre-market y after-hours tiene 960)
MAX_BAR_IN = 4000     # velas por envío
T_MIN, T_MAX = 1.5e9, 4e9  # hora de una vela (segundos desde 1970): fuera de esto es basura


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


def _bar(r) -> tuple | None:
    """[hora, apertura, máximo, mínimo, cierre, volumen] → tupla limpia, o None si algo no cuadra."""
    if not isinstance(r, (list, tuple)) or len(r) != 6:
        return None
    try:
        t = int(r[0])
        o, h, lo, c, v = (float(x) for x in r[1:])
    except (TypeError, ValueError, OverflowError):
        return None
    if not (T_MIN < t < T_MAX and all(math.isfinite(x) and x > 0 for x in (o, h, lo, c))
            and math.isfinite(v) and v >= 0 and h >= lo):
        return None
    return (t, o, h, lo, c, v)


class Bridge:
    def __init__(self):
        self.lock = threading.Lock()
        self.scan: dict[str, list[str]] = {}
        self.scan_ts = 0.0
        self.quotes: dict[str, dict] = {}
        self.seen = 0.0
        self.info: dict = {}
        self.bars: dict[str, list[tuple]] = {}  # {ticker: [(hora, o, h, l, c, v), …]} velas de 1 min de IBKR, en orden
        self.bars_ts: dict[str, float] = {}     # llegada de la última actualización de cada ticker

    def update(self, scan: dict | None = None, quotes: dict | None = None, info: dict | None = None,
               now: float | None = None) -> list[str]:
        """Guarda lo recibido (con tope de tamaño) y devuelve los tickers que traen precio nuevo. La hora de cada
        precio es la de llegada al servidor: así no importa si el reloj de la PC está corrido."""
        now = now or time.time()
        fresh = []
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
                                  "ask": _num(q.get("ask")), "ts": now}
                fresh.append(s)
            old = [s for s, q in self.quotes.items() if now - q["ts"] > 3600]
            if len(self.quotes) - len(old) > MAX_KEEP:  # memoria acotada aunque lleguen muchos tickers distintos
                old += sorted(self.quotes, key=lambda s: self.quotes[s]["ts"])[:len(self.quotes) - MAX_KEEP]
            for s in set(old):
                del self.quotes[s]
        return fresh

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

    def put_bars(self, bars: dict | None, now: float | None = None) -> list[str]:
        """Velas de 1 min que manda el puente. La primera vez llega el día completo de una acción; después, desde la
        última vela que el servidor ya tiene (la que se está formando cambia cada ~5 s). Lo que llega manda desde su
        primera vela en adelante. Devuelve los tickers cuyas velas cambiaron."""
        now = now or time.time()
        changed = []
        if not isinstance(bars, dict):
            return changed
        budget = MAX_BAR_IN
        with self.lock:
            for s, rows in list(bars.items())[:MAX_BAR_SYMS]:
                s = _sym(s)
                if not s or not isinstance(rows, (list, tuple)) or len(rows) > budget:
                    continue  # nunca se guarda una parte: cortar el final dejaría la serie sin las velas más nuevas
                budget -= len(rows)
                clean = {}
                for r in rows:
                    b = _bar(r)
                    if b:
                        clean[b[0]] = b
                if not clean:
                    continue
                new = [clean[t] for t in sorted(clean)]
                old = self.bars.get(s, [])
                merged = ([b for b in old if b[0] < new[0][0]] + new)[-MAX_BAR_ROWS:]
                self.bars_ts[s] = now
                if merged != old:
                    self.bars[s] = merged
                    changed.append(s)
            old = [s for s, ts in self.bars_ts.items() if now - ts > 3600]
            if len(self.bars_ts) - len(old) > MAX_BAR_SYMS:  # memoria acotada: se van las que llevan más sin actualizar
                old += sorted(self.bars_ts, key=self.bars_ts.get)[:len(self.bars_ts) - MAX_BAR_SYMS]
            for s in set(old):
                self.bars.pop(s, None)
                self.bars_ts.pop(s, None)
        return [s for s in changed if s in self.bars]

    def bar_rows(self, s: str, now: float | None = None, ttl: float = BARS_TTL) -> list[tuple] | None:
        """Copia de las velas de IBKR de un ticker si están al día (actualizadas hace ≤ ttl s); si no, None."""
        now = now or time.time()
        with self.lock:
            rows = self.bars.get(s)
            return list(rows) if rows and now - self.bars_ts.get(s, 0) <= ttl else None

    def bars_have(self, syms: list[str]) -> dict[str, int]:
        """Para cada ticker pedido, la hora de la última vela que ya tiene el servidor (0 = ninguna): el puente manda
        desde ahí. Si el servidor se reinicia, todo vuelve a 0 y el puente manda el día completo otra vez."""
        with self.lock:
            return {s: int(self.bars[s][-1][0]) if self.bars.get(s) else 0 for s in syms}

    def reset_bars(self):
        with self.lock:
            self.bars, self.bars_ts = {}, {}

    def on(self, now: float | None = None) -> bool:
        return bool(self.seen) and (now or time.time()) - self.seen <= ON_TTL

    def status(self, now: float | None = None) -> dict:
        """Estado para /health y la página: si está conectado y qué tan fresco, sin precios."""
        now = now or time.time()
        with self.lock:
            scan_ok = bool(self.scan) and now - self.scan_ts <= SCAN_TTL
            return {"on": bool(self.seen) and now - self.seen <= ON_TTL,
                    "age_s": round(now - self.seen, 1) if self.seen else None,
                    "scan": {k: len(v) for k, v in self.scan.items()} if scan_ok else {},
                    "quotes": sum(1 for q in self.quotes.values() if now - q["ts"] <= QUOTE_TTL),
                    "bars": sum(1 for s, ts in self.bars_ts.items() if now - ts <= BARS_TTL and self.bars.get(s)),
                    "ib": self.info.get("ib"), "error": self.info.get("error")}
