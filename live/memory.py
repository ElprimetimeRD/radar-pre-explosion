"""Memoria del proceso. El plan Starter de Render da 512 MB y el 1-oct el semáforo se quedó sin memoria a las 10:18,
en plena sesión: el proceso venía al ~95 % desde el día anterior (hilos de descarga retenidos por `multitasking`,
ver scanner.sources.yahoo.prune_tasks) y además pandas deja memoria liberada pero retenida por malloc.
Aquí: medir (RSS en /health y en la página) y devolver al sistema lo que ya se liberó después de cada ciclo."""
from __future__ import annotations

import ctypes
import gc
import os

LIMIT_MB = float(os.environ.get("MEM_LIMIT_MB", "512"))
ALERT_PCT = float(os.environ.get("MEM_ALERT_PCT", "85"))  # aviso por Telegram (una vez por día) desde este %
M_ARENA_MAX = -8  # constante de glibc para mallopt

try:
    _libc = ctypes.CDLL("libc.so.6")
except OSError:  # no es glibc (macOS, Alpine): solo se mide
    _libc = None


def limit_arenas(n: int = 2) -> bool:
    """Pocas arenas de malloc: cada descarga abre un hilo por ticker y glibc les da arenas propias que se fragmentan.
    Se llama al arrancar, antes de crear hilos."""
    if _libc is None:
        return False
    try:
        return bool(_libc.mallopt(M_ARENA_MAX, int(n)))
    except (AttributeError, OSError):
        return False


def rss_mb() -> float | None:
    """Memoria residente del proceso en MB (Linux); None si no se puede leer."""
    try:
        with open("/proc/self/status", encoding="ascii", errors="ignore") as f:
            for ln in f:
                if ln.startswith("VmRSS:"):
                    return round(int(ln.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    return None


def pct(rss: float | None) -> float | None:
    return round(100 * rss / LIMIT_MB, 1) if rss and LIMIT_MB > 0 else None


def trim() -> dict:
    """Recolecta basura, devuelve al sistema la memoria libre que malloc retiene y mide. {'rss_mb', 'pct'}"""
    gc.collect()
    if _libc is not None:
        try:
            _libc.malloc_trim(0)
        except (AttributeError, OSError):
            pass
    r = rss_mb()
    return {"rss_mb": r, "pct": pct(r)}
