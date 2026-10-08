"""Foco del radar (8-oct-2026, pedido de Priamo: «enfocar mi radar en Biotech»).

FOCO=biotech (por defecto) hace que el universo del semáforo solo incluya acciones de la industria «Biotechnology» de Yahoo
(pantallas de subidas y de volumen de esa industria) más tu watchlist, que siempre entra. Las listas de movers generales,
los escáneres de IBKR, los halts y las noticias de otras acciones quedan fuera del universo. Afecta a TODO lo que sale del
universo: alertas de dinero real, paper y los dos ejecutores. FOCO=general vuelve al radar de siempre (sin cambiar código).

Lo que NO cambia con el foco: el radar de flujo en penny stocks (su propia pantalla, FLUJO=0 lo apaga), las reglas de
decisión, los topes de dinero y las órdenes en curso o abiertas (se siguen aunque la acción salga del universo).
"""
from __future__ import annotations

import os

MODO = os.environ.get("FOCO", "biotech").strip().lower()
INDUSTRIA = os.environ.get("FOCO_INDUSTRIA", "Biotechnology")   # nombre exacto de la industria en Yahoo
N = int(os.environ.get("FOCO_N", "100"))                         # acciones por pantalla (subidas y volumen)


def activo() -> bool:
    return MODO == "biotech"


def screen_query(Q, exch):
    """Pantalla de Yahoo: acciones de la industria, en bolsas de EE. UU., con volumen y precio mínimos."""
    return Q("and", [Q("eq", ["industry", INDUSTRIA]), exch, Q("gt", ["dayvolume", 50000]), Q("gte", ["intradayprice", 1])])


def permitido(sym: str, bio: set, watch: set) -> bool:
    """¿Puede entrar esta acción al universo con el foco vigente? Con FOCO=general, todas."""
    return not activo() or sym in bio or sym in watch
