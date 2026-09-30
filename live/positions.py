"""Monitor de posiciones reales: avisa por Telegram si una posición cae 2 % / 3 % desde tu entrada o llega a tu objetivo.

No ejecuta ninguna orden: solo vigila y avisa. Las posiciones las registra quien ejecuta la compra
(POST /api/positions con el token POSITIONS_TOKEN) y se quitan al vender (DELETE /api/positions/{T}).
Lógica pura: no toca la red, así se prueba sin internet.
"""
from __future__ import annotations

import os
import re
import threading
import time

from scanner.util import DATA, read_json, write_json

PATH = os.path.join(DATA, "live", "positions.json")
TICKER = re.compile(r"[A-Z]{1,5}(\.[A-Z])?")


def parse_levels(raw: str, default: str) -> tuple[float, ...]:
    out = []
    for x in (raw or default).split(","):
        try:
            v = float(x)
        except ValueError:
            continue
        if v > 0:
            out.append(v)
    return tuple(sorted(set(out))) or tuple(sorted({float(x) for x in default.split(",")}))


DROP_ALERTS = parse_levels(os.environ.get("DROP_ALERTS", ""), "2,3")   # % de caída desde la entrada que avisan
DEFAULT_TP = float(os.environ.get("DEFAULT_TP", "3"))                  # % objetivo si la posición no trae el suyo


class Positions:
    def __init__(self, path: str = PATH, drops: tuple[float, ...] = DROP_ALERTS, default_tp: float = DEFAULT_TP):
        self.path, self.drops, self.default_tp = path, tuple(sorted(drops)), default_tp
        self.lock = threading.Lock()
        self.items: dict[str, dict] = read_json(path, {}) or {}

    def _save(self):
        write_json(self.path, self.items)

    def symbols(self) -> list[str]:
        with self.lock:
            return sorted(self.items)

    def upsert(self, t: str, entry: float, qty: float | None = None, tp: float | None = None) -> dict:
        """Registra o actualiza una posición. Si cambia la entrada, los avisos ya enviados se reinician."""
        t = str(t or "").upper().strip()
        if not TICKER.fullmatch(t):
            raise ValueError(f"ticker inválido: {t!r}")
        if not entry or entry <= 0:
            raise ValueError("entry debe ser > 0")
        if qty is not None and qty <= 0:
            raise ValueError("qty debe ser > 0")
        if tp is not None and not 0.2 <= tp <= 100:
            raise ValueError("tp debe estar entre 0.2 y 100 (%)")
        with self.lock:
            old = self.items.get(t)
            same = bool(old) and abs(old["entry"] - entry) < 1e-9
            p = {"t": t, "entry": float(entry), "qty": qty, "tp": tp if tp is not None else self.default_tp,
                 "added": old["added"] if same else time.time(), "sent": list(old["sent"]) if same else [],
                 "last": old.get("last") if same else None, "pct": old.get("pct") if same else None,
                 "min_pct": old.get("min_pct", 0.0) if same else 0.0, "max_pct": old.get("max_pct", 0.0) if same else 0.0}
            self.items[t] = p
            self._save()
            return dict(p)

    def remove(self, t: str) -> bool:
        with self.lock:
            gone = self.items.pop(str(t).upper().strip(), None) is not None
            if gone:
                self._save()
            return gone

    def listing(self) -> list[dict]:
        with self.lock:
            return [dict(p) for p in self.items.values()]

    def check(self, prices: dict[str, float]) -> list[tuple[str, str]]:
        """Compara el último precio de cada posición con su entrada. Devuelve [(clave_única, mensaje)] de los avisos
        NUEVOS (cada nivel avisa una sola vez por posición). Si cruza varios niveles de golpe, avisa solo el más hondo."""
        alerts: list[tuple[str, str]] = []
        with self.lock:
            for t, p in self.items.items():
                px = prices.get(t)
                if not px or px <= 0:
                    continue
                e = p["entry"]
                pct = 100 * (px / e - 1)
                p["last"], p["pct"] = round(px, 4), round(pct, 2)
                p["min_pct"], p["max_pct"] = round(min(p["min_pct"], pct), 2), round(max(p["max_pct"], pct), 2)
                pl = f" · {p['qty'] * (px - e):+.2f} US$ sobre {p['qty']:g} acc." if p.get("qty") else ""
                crossed = [d for d in self.drops if pct <= -d and f"d{d:g}" not in p["sent"]]
                if crossed:
                    p["sent"] += [f"d{d:g}" for d in crossed]
                    deepest = max(crossed)
                    alerts.append((f"pos:{t}:{e:g}:d{deepest:g}",
                                   f"📉 {t} cayó {pct:+.1f}% desde tu entrada {e:.2f} (ahora {px:.2f}){pl}. "
                                   f"Si quieres salir, dime «véndela {t}»."))
                tp = p.get("tp")
                if tp and pct >= tp and "tp" not in p["sent"]:
                    p["sent"].append("tp")
                    alerts.append((f"pos:{t}:{e:g}:tp",
                                   f"🎯 {t} llegó a {pct:+.1f}% (objetivo +{tp:g}%, ahora {px:.2f}){pl}. "
                                   f"Si no dejaste la orden límite de venta puesta, dime «véndela {t}»."))
            if self.items:
                self._save()
        return alerts
