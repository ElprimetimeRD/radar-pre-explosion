"""Puente IBKR: lo que manda el programa bridge/puente_ibkr.py desde la PC de Priamo (escáneres de IBKR y precios al
instante de las acciones armadas). Los precios solo se usan para avisar rupturas antes: la página y la API públicas
no los muestran (los datos de IBKR son para uso personal)."""
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
                    "ib": self.info.get("ib"), "error": self.info.get("error")}
