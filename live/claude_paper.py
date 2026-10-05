"""El ejecutor paralelo de Claude (bridge/ejecutor_claude.py) visto desde el semáforo, y la vigilancia de los dos ejecutores.

Claude opera en la MISMA cuenta paper que el ejecutor de Priamo, con otra estrategia y órdenes marcadas "cla-…" (las de
Priamo son "sem-…"), para comparar resultados. Aquí:
- POST /api/claude/sync (BRIDGE_TOKEN): recibe su estado y lo que pasó (puesta, comprada, vendida…), lo manda a Telegram
  como «🤖 Claude: …» y le responde con lo que comparte con el ejecutor de Priamo: /pausa, /cerrar y los límites de dinero.
  Claude no recibe órdenes del semáforo: decide solo, con datos de Yahoo que lee su propio programa.
- /claude y /marcador: cómo va Claude y el marcador del día, Priamo contra Claude.
- Vigilancia: si cualquiera de los dos ejecutores se desconecta en horario de mercado (o IB Gateway se cae, o Claude se
  queda sin datos de Yahoo), llega un aviso por Telegram, una sola vez por caída, y otro cuando vuelve.
Lógica pura (sin red): la prueba tests/test_claude_paper.py.
"""
from __future__ import annotations

import threading
import time

from .paper import _num

# Topes de dinero de Claude: los mismos que tiene fijos su ejecutor (bridge/ejecutor_claude.py). No suben con los del
# ejecutor de Priamo (live/paper.py): el tamaño de Claude lo fija su estrategia.
ORDEN_USD, MAX_ABIERTO, PERDIDA_MAX = 500.0, 1000.0, 100.0

# Días en que la bolsa de EE. UU. no abre (entre semana), para no avisar de «ejecutor caído» en un feriado. Mejor esfuerzo:
# si falta uno, solo llega un aviso de más. Los días de cierre temprano (13:00 ET) NO están aquí: ver el README.
FERIADOS = frozenset({"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26"})
EXEC_TTL = 20            # s sin noticias = desconectado (para el estado)
CAIDA_S = 90             # s sin noticias en horario de mercado = aviso de caída
IB_CAIDO_S = 120         # s con el ejecutor vivo pero sin IB Gateway = aviso
POR = {"stop": "el stop", "objetivo": "el objetivo de 2R", "cierre": "el cierre de las 15:55"}


def _txt(v, n=160) -> str:
    return str(v or "")[:n]


def _dur(s: float) -> str:
    m = int(s // 60)
    return f"{m} min" if m < 120 else f"{m // 60} h"


class ClaudePaper:
    def __init__(self, paper, clock=time.time):
        self.paper = paper                      # el de Priamo: de él salen /pausa, /cerrar y los límites
        self.clock = clock
        self.lock = threading.RLock()
        self.ex_seen = 0.0
        self.ex: dict = {}
        self.msgs: list[tuple[str, str]] = []
        self.hoy: dict = self._dia_nuevo(None)
        self.alertas: dict[str, dict] = {}

    @staticmethod
    def _dia_nuevo(dia: str | None) -> dict:
        return {"dia": dia, "puestas": 0, "llenas": 0, "canceladas": 0, "salidas": []}

    # ---------------- el ejecutor de Claude ----------------
    def conectado(self, now: float | None = None) -> bool:
        now = now or self.clock()
        with self.lock:
            return bool(self.ex_seen) and now - self.ex_seen <= EXEC_TTL and bool(self.ex.get("ib")) and bool(self.ex.get("paper"))

    def sync(self, estado: dict | None, eventos: list | None, now: float | None = None, dia: str | None = None) -> dict:
        """Estado y eventos de Claude → lo que comparte con el ejecutor de Priamo (pausa, cerrar, límites)."""
        now = now or self.clock()
        with self.lock:
            if dia and self.hoy["dia"] != dia:
                self.hoy = self._dia_nuevo(dia)
            self.ex_seen = now
            self.ex = self._limpiar(estado or {})
            for e in list(eventos or [])[:200]:
                if isinstance(e, dict):
                    self._evento(e)
        self.paper.adoptar_pausa(estado)
        with self.paper.lock:
            pausa, cid = self.paper.pausa, self.paper.cerrar_id
            hace = round(now - self.paper.cerrar_ts, 1) if cid else None
        return {"ordenes": [], "cancelar": [], "pausa": pausa, "modo": "auto", "cerrar_id": cid, "cerrar_hace_s": hace,
                "limites": {"orden_usd": ORDEN_USD, "max_abierto": MAX_ABIERTO, "perdida_max": PERDIDA_MAX}}

    @staticmethod
    def _limpiar(e: dict) -> dict:
        out = {}
        for k in ("ib", "paper", "parado", "pausa", "feed_ok"):
            out[k] = bool(e.get(k))
        for k in ("comprometido", "riesgo_abierto", "pnl_dia", "perdida_dia"):
            v = _num(e.get(k))
            out[k] = round(v, 2) if v is not None else None
        for k in ("ops", "gan", "feed_n", "feed_de", "feed_edad_s"):
            v = _num(e.get(k))
            out[k] = int(v) if v is not None else None
        out["cuenta"] = _txt(e.get("cuenta"), 20) or None
        out["bloqueado"] = _txt(e.get("bloqueado"), 200) or None
        out["version"] = _txt(e.get("version"), 10) or None
        out["estrategia"] = _txt(e.get("estrategia"), 60) or None
        out["feed_error"] = _txt(e.get("feed_error"), 120) or None
        pos = []
        for p in list(e.get("posiciones") or [])[:20]:
            if isinstance(p, dict) and p.get("t"):
                pos.append({"t": _txt(p["t"], 6), "qty": _num(p.get("qty")), "px": _num(p.get("px"))})
        out["posiciones"] = pos
        out["vivas_t"] = [_txt(x, 6) for x in list(e.get("vivas_t") or [])[:20]]
        out["candidatas"] = [_txt(x) for x in list(e.get("candidatas") or [])[:3]]
        return out

    def _msg(self, key: str, text: str):
        self.msgs.append((key, text))
        del self.msgs[:-100]

    def tomar_msgs(self) -> list[tuple[str, str]]:
        with self.lock:
            out, self.msgs = self.msgs, []
            return out

    def _evento(self, e: dict):
        oid, ev = _txt(e.get("id"), 16), _txt(e.get("ev"), 20)
        t = _txt(e.get("t") or "?", 6)
        px, qty = _num(e.get("px")), _num(e.get("qty"))
        motivo = _txt(e.get("motivo"), 200)
        if ev == "puesta":
            self.hoy["puestas"] += 1
            self._msg(f"claude:{oid}:puesta", f"🟪 Claude: orden puesta en IBKR · {t} {_txt(e.get('detalle'), 300)}".rstrip())
        elif ev == "llena":
            self.hoy["llenas"] += 1
            self._msg(f"claude:{oid}:llena", f"📥 Claude: compré {qty or 0:g} {t} a {px or 0:.2f}. Su stop fijo y su objetivo ya "
                                              f"están puestos en IBKR.")
        elif ev == "salida":
            pnl, pe = _num(e.get("pnl")), _num(e.get("px_e"))
            self.hoy["salidas"].append({"t": t, "pnl": pnl or 0.0})
            pct = f"{100 * (px / pe - 1):+.1f}%, " if px and pe else ""
            usd = f"{pnl:+.2f} US$" if pnl is not None else ""
            self._msg(f"claude:{oid}:salida", f"{'✅' if (pnl or 0) >= 0 else '🛑'} Claude: vendí {qty or 0:g} {t} a {px or 0:.2f} "
                                               f"por {POR.get(e.get('por'), 'una venta')} ({pct}{usd}).")
        elif ev == "rechazada":
            self._msg(f"claude:{oid}:rechazada", f"⚠ Claude: no se puso la orden de {t}: {motivo or 'IBKR la rechazó'}.")
        elif ev == "cancelada":
            self.hoy["canceladas"] += 1
            if "venció" not in motivo:      # las compras que vencen sin llenarse son lo normal: no hacen ruido
                self._msg(f"claude:{oid}:cancelada", f"⌛ Claude: cancelé la compra de {t} ({motivo or 'sin llenarse'}).")
        elif ev == "parada":
            self._msg(f"claude:parada:{_txt(e.get('dia'), 12)}", f"⛔ Claude: llegó a la pérdida máxima del día "
                                                                  f"(US${PERDIDA_MAX:,.0f}). No pone más órdenes hoy.")
        elif ev == "cierre":
            self._msg(f"claude:cierre:{_txt(e.get('dia'), 12)}:{motivo}", f"⏰ Claude: cerré todo ({motivo or 'fin del día'}).")
        elif ev == "error":
            self._msg(f"claude:error:{motivo[:40]}", f"⚠ Claude: {motivo}")

    # ---------------- textos ----------------
    def estado_txt(self, now: float | None = None) -> str:
        now = now or self.clock()
        with self.lock:
            ex = self.ex
            lines = [f"🤖 Claude (paper) · {ex.get('estrategia') or 'pullback con tendencia'}"
                     + (" · EN PAUSA" if self.paper.pausa else "")]
            if self.conectado(now):
                feed = (f"datos de Yahoo {ex.get('feed_n') or 0}/{ex.get('feed_de') or 0} acciones, hace {ex.get('feed_edad_s')} s"
                        if ex.get("feed_ok") else f"SIN datos de Yahoo ({ex.get('feed_error') or 'sin lecturas'})")
                lines.append(f"Ejecutor conectado (cuenta {ex.get('cuenta') or '?'}) · {feed}")
            elif ex.get("bloqueado"):
                lines.append(f"Ejecutor bloqueado: {ex['bloqueado']}")
            elif self.ex_seen:
                lines.append(f"Ejecutor sin conexión (última vez hace {_dur(now - self.ex_seen)})")
            else:
                lines.append("Ejecutor sin conexión: abre ARRANCAR_PAPER.bat en tu PC (abre IB Gateway paper y los dos ejecutores)")
            ops = ex.get("ops") or 0
            lines.append(f"Comprometido US${ex.get('comprometido') or 0:,.0f} de US${MAX_ABIERTO:,.0f} · P/L hoy "
                         f"{ex.get('pnl_dia') or 0:+.2f} US$ · {ops} ops ({ex.get('gan') or 0} ganadas) · pérdida máx "
                         f"US${PERDIDA_MAX:,.0f}" + (" · ⛔ parado por pérdida" if ex.get("parado") else ""))
            pos = ex.get("posiciones") or []
            lines.append("Posiciones: " + (", ".join(f"{p['t']} {p['qty'] or 0:g} @{p['px'] or 0:.2f}" for p in pos) if pos else "ninguna"))
            pend = [t for t in ex.get("vivas_t") or [] if t not in {p["t"] for p in pos}]
            if pend:
                lines.append("Compras puestas esperando: " + ", ".join(pend))
            lines.append(f"Hoy: {self.hoy['puestas']} órdenes puestas · {self.hoy['llenas']} llenadas · {self.hoy['canceladas']} canceladas")
            if ex.get("candidatas"):
                lines.append("Jugadas que ve ahora: " + " | ".join(ex["candidatas"]))
            return "\n".join(lines)

    def marcador(self, now: float | None = None) -> str:
        """Priamo (semáforo) contra Claude, solo lo realizado hoy (lo abierto aún no cuenta)."""
        now = now or self.clock()

        def fila(nombre, ex, visto):
            if not visto:
                return f"{nombre}: sin datos (el ejecutor no se conectó)"
            ops = ex.get("ops") or 0
            viejo = f" · dato de hace {_dur(now - visto)} (sin conexión)" if now - visto > 300 else ""
            return (f"{nombre}: {ex.get('pnl_dia') or 0:+.2f} US$ · {ops} ops cerradas ({ex.get('gan') or 0} ganadas)"
                    + (f" · {len(ex.get('posiciones') or [])} posiciones abiertas" if ex.get("posiciones") else "") + viejo)
        with self.lock, self.paper.lock:
            lineas = ["📊 Marcador de hoy (paper, lo realizado)",
                      fila("Tú (semáforo)", self.paper.ex, self.paper.ex_seen),
                      fila("Claude (pullback)", self.ex, self.ex_seen),
                      f"Claude: {self.hoy['puestas']} órdenes puestas, {self.hoy['llenas']} llenadas, {self.hoy['canceladas']} canceladas."]
            a, b = self.paper.ex.get("pnl_dia"), self.ex.get("pnl_dia")
            if a is not None and b is not None and (self.paper.ex.get("ops") or self.ex.get("ops")):
                lineas.append("Va ganando: " + ("Priamo" if a > b else "Claude" if b > a else "empate"))
            lineas.append("Una sola sesión no demuestra nada: lo que cuenta es el acumulado de varios días (diario_sem.csv y "
                          "diario_cla.csv; COMPARAR.bat los junta).")
            return "\n".join(lineas)

    def actividad(self) -> bool:
        with self.lock, self.paper.lock:
            return bool(self.hoy["puestas"] or self.paper.ex.get("ops") or self.ex.get("ops") or self.paper.ex.get("posiciones"))

    def status(self, now: float | None = None) -> dict:
        """Para /health: sin precios ni posiciones."""
        now = now or self.clock()
        with self.lock:
            return {"ejecutor": self.conectado(now), "visto_s": round(now - self.ex_seen, 1) if self.ex_seen else None,
                    "datos_yahoo": self.ex.get("feed_ok"), "bloqueado": self.ex.get("bloqueado"),
                    "parado": self.ex.get("parado"), "puestas": self.hoy["puestas"], "llenas": self.hoy["llenas"]}

    # ---------------- vigilancia ----------------
    def vigilar(self, now: float, hoy: str, minuto: int, laborable: bool, uptime_s: float) -> list[tuple[str, str]]:
        """Avisos de caída de los dos ejecutores (una vez por caída) y de que Claude no tiene datos. Entre las 9:25 y las
        16:00 ET de un día hábil."""
        with self.lock:
            if self.hoy["dia"] != hoy:
                self.hoy = self._dia_nuevo(hoy)
        if not laborable or not 9 * 60 + 25 <= minuto < 16 * 60:
            self.alertas.clear()
            return []
        out: list[tuple[str, str]] = []
        with self.lock, self.paper.lock:
            for nombre, seen, ex, quien in (("paper", self.paper.ex_seen, self.paper.ex, "El ejecutor de Priamo"),
                                            ("claude", self.ex_seen, self.ex, "El ejecutor de Claude")):
                st = self.alertas.setdefault(nombre, {})
                conectado = bool(ex.get("ib")) and bool(ex.get("paper"))
                if not seen:
                    if uptime_s > 240 and not st.get("caido"):
                        st["caido"] = True
                        out.append((f"ej:{nombre}:nunca:{hoy}", f"⚠ {quien} no está conectado y el mercado abre o ya abrió "
                                                                 f"(9:30 ET). Abre ARRANCAR_PAPER.bat en tu PC."))
                elif now - seen > CAIDA_S:
                    if not st.get("caido"):
                        st["caido"] = True
                        out.append((f"ej:{nombre}:caido:{hoy}:{int(seen)}",
                                    f"⚠ {quien} dejó de conectarse (última vez hace {_dur(now - seen)}). ¿Se apagó o se durmió la "
                                    f"PC? Abre ARRANCAR_PAPER.bat. Mientras tanto no pone órdenes y lo que ya compró sigue "
                                    f"protegido por su stop en IBKR."))
                elif not conectado:
                    st.setdefault("ib_desde", now)
                    if now - st["ib_desde"] > IB_CAIDO_S and not st.get("ib_avisado"):
                        st["ib_avisado"] = True
                        motivo = ex.get("bloqueado") or "IB Gateway paper no responde"
                        out.append((f"ej:{nombre}:ib:{hoy}:{int(st['ib_desde'])}",
                                    f"⚠ {quien} está vivo pero sin conexión con IB Gateway paper ({motivo}). Abre "
                                    f"ARRANCAR_PAPER.bat para que lo vuelva a abrir."))
                else:
                    if st.get("caido") or st.get("ib_avisado"):
                        out.append((f"ej:{nombre}:vuelve:{hoy}:{int(now)}", f"✅ {quien} está conectado otra vez."))
                    st.clear()
            c = self.alertas.setdefault("claude_feed", {})
            if (self.conectado(now) and self.ex.get("feed_ok") is False and 9 * 60 + 55 <= minuto < 15 * 60 + 10):
                if now - c.get("feed_t", 0) > 3600:
                    c["feed_t"] = now
                    out.append((f"claude:feed:{hoy}:{minuto // 60}",
                                f"⚠ Claude no tiene datos de Yahoo ({self.ex.get('feed_error') or 'sin lecturas'}): no pone "
                                f"órdenes nuevas hasta que vuelvan. Lo ya comprado sigue con su stop."))
        return out
