"""Ejecución en la cuenta PAPER de IBKR (dinero simulado), a pedido de Priamo.

- Los avisos 🟡 ARMA, 🟢 COMPRA y ⚡ traen un botón «✅ Ejecutar en paper». Al tocarlo, la orden entra a una cola.
- El ejecutor (bridge/ejecutor_paper.py, en la PC de Priamo, conectado SOLO a IB Gateway paper) pregunta cada segundo
  (POST /api/paper/sync con BRIDGE_TOKEN), pone la orden en IBKR y devuelve lo que pasa: puesta, comprada, vendida,
  rechazada. Cada cosa llega por Telegram.
- /auto: las órdenes entran a la cola sin botón (modo automático). /boton vuelve al botón.
- /pausa, /reanuda y /cerrar valen también para el ejecutor paralelo de Claude (live/claude_paper.py), que opera en la
  misma cuenta con órdenes "cla-…" y otra estrategia.
- Límites (los vuelve a aplicar el ejecutor, que tiene la última palabra): US$500 por operación, US$1,000
  comprometidos (posiciones + compras puestas), no más órdenes si la pérdida del día (realizada + lo que podría perder
  lo abierto) pasaría de US$100, sin compras nuevas fuera de 9:30–12:00 ET, una sola orden viva por acción, y a las
  15:55 ET el ejecutor cancela lo suyo y vende lo que compró.

Lógica pura (sin red): la prueban tests/test_paper.py.
"""
from __future__ import annotations

import math
import os
import re
import secrets
import threading
import time
from datetime import datetime

from scanner.util import ET

ORDEN_USD = float(os.environ.get("ORDEN_USD", "500"))
MAX_ABIERTO = float(os.environ.get("PAPER_MAX_ABIERTO", "1000"))
PERDIDA_MAX = float(os.environ.get("PAPER_PERDIDA_MAX", "100"))
ENTRADA_INI_M = 9 * 60 + 30
ENTRADA_FIN_M = int(os.environ.get("ENTRY_END_M", str(12 * 60)))  # el mismo corte que las COMPRA del semáforo
CIERRE_M = 15 * 60 + 55
EXEC_TTL = 20          # s sin noticias del ejecutor = desconectado (sin botón en los avisos)
CERRAR_TTL = 300       # s que vale un /cerrar confirmado para un ejecutor que se conecta tarde
CONFIRMA_TTL = 120     # s que vale el botón «Sí, cerrar todo» después de escribir /cerrar
OFERTA_TTL = {"compra": 120, "ruptura": 90}   # s para tocar el botón de una COMPRA a mercado o de una ruptura
VIVAS = ("cola", "enviada", "puesta", "llena")
ORDEN_ESTADOS = {"oferta": 0, "cola": 1, "enviada": 2, "puesta": 3, "llena": 4,
                 "cerrada": 5, "cancelada": 5, "rechazada": 5}   # un aviso viejo nunca hace retroceder el estado
CANCEL_ESPERA = 120    # s: una orden marcada para cancelar de la que el ejecutor no dice nada se da por cancelada
ID = re.compile(r"[0-9a-f]{8}")
POR = {"trailing": "el stop que sube", "objetivo": "el objetivo +5%", "cierre": "el cierre"}


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def et_min(ts: float) -> int:
    t = datetime.fromtimestamp(ts, ET)
    return t.hour * 60 + t.minute


def et_ts(ts: float, minute: int) -> float:
    """Epoch de hoy (fecha ET de `ts`) a la hora `minute` ET."""
    t = datetime.fromtimestamp(ts, ET)
    return t.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0).timestamp()


def hhmm(minute: int) -> str:
    return f"{minute // 60}:{minute % 60:02d}"


class Paper:
    def __init__(self, modo: str | None = None, clock=time.time):
        self.lock = threading.RLock()
        self.modo = "auto" if (modo or os.environ.get("PAPER_MODO", "boton")).lower().startswith("auto") else "boton"
        self.pausa = False
        self.ofertas: dict[str, dict] = {}
        self.cerrar_id: str | None = None
        self.cerrar_ts = 0.0
        self.confirma: tuple[str, float] | None = None   # (código, hora) del último /cerrar sin confirmar
        self._adoptado = False
        self.ex_seen = 0.0
        self.ex: dict = {}
        self.clock = clock
        self.msgs: list[tuple[str, str]] = []   # (clave única, texto) para Telegram; los manda el semáforo

    # ---------------- estado ----------------
    def disponible(self, now: float | None = None) -> bool:
        """Hay botón solo si el ejecutor de la PC habló hace poco, está conectado a IBKR y la cuenta es paper."""
        now = now or self.clock()
        with self.lock:
            return (bool(self.ex_seen) and now - self.ex_seen <= EXEC_TTL and bool(self.ex.get("ib"))
                    and bool(self.ex.get("paper")))

    def viva(self, t: str, excluir: str | None = None) -> dict | None:
        """La orden o posición de paper que ya hay en esa acción (una sola por acción): de esta sesión o, tras un
        reinicio del semáforo, la que informa el ejecutor."""
        with self.lock:
            for o in self.ofertas.values():
                if o["t"] == t and o["estado"] in VIVAS and o["id"] != excluir:
                    return o
            if t in (self.ex.get("vivas_t") or []):
                return {"t": t, "estado": "puesta"}
        return None

    def _en_cola(self) -> list[dict]:
        return [o for o in self.ofertas.values() if o["estado"] in ("cola", "enviada")]

    def _motivo(self, o: dict, now: float) -> str | None:
        """Por qué no se puede poner esta orden ahora (None = se puede)."""
        if self.pausa:
            return "está en pausa (/reanuda para seguir)"
        if not self.disponible(now):
            return "el ejecutor de tu PC no está conectado a IB Gateway paper"
        m = et_min(now)
        if not ENTRADA_INI_M <= m < ENTRADA_FIN_M:
            return f"fuera del horario de compras ({hhmm(ENTRADA_INI_M)}–{hhmm(ENTRADA_FIN_M)} ET)"
        if o.get("hasta") and now > o["hasta"]:
            return "la orden ya venció"
        if self.ex.get("parado"):
            return f"ya se perdió el máximo del día (US${PERDIDA_MAX:,.0f})"
        if self.viva(o["t"], excluir=o["id"]):
            return f"ya hay una orden o posición en {o['t']}"
        cola = [x for x in self._en_cola() if x["id"] != o["id"]]
        comp = (_num(self.ex.get("comprometido")) or 0) + sum(x["qty"] * x["limite"] for x in cola)
        costo = o["qty"] * o["limite"]
        if comp + costo > MAX_ABIERTO + 0.01:
            return f"pasaría de US${MAX_ABIERTO:,.0f} comprometidos (ya hay US${comp:,.0f})"
        riesgo = ((_num(self.ex.get("perdida_dia")) or 0) + (_num(self.ex.get("riesgo_abierto")) or 0)
                  + sum(x["qty"] * x["trail"] for x in cola) + o["qty"] * o["trail"])
        if riesgo > PERDIDA_MAX + 0.01:
            return f"podría pasar la pérdida máxima del día (US${PERDIDA_MAX:,.0f})"
        return None

    # ---------------- ofertas (una por aviso) ----------------
    def ofrecer(self, kind: str, t: str, entry: float, stop: float, t2: float | None, limite: float,
                gatillo: float | None = None, hasta: float | None = None, now: float | None = None) -> dict | None:
        """Prepara la orden de un aviso. None si no hay ejecutor conectado o el presupuesto no alcanza: el aviso sale
        sin botón, como siempre. En modo automático la manda a la cola en el acto (si pasa los límites) y deja en
        'nota' qué pasó; en modo botón marca 'boton'."""
        now = now or self.clock()
        if not self.disponible(now) or not entry or not stop or stop >= entry or not limite or limite <= 0:
            return None
        limite = round(limite, 2)
        qty = int(ORDEN_USD // limite)
        if qty < 1:
            return None
        trail = max(0.01, round(entry - stop, 2))
        o = {"id": secrets.token_hex(4), "kind": kind, "t": t, "tipo": "stp" if gatillo else "lmt",
             "gatillo": round(gatillo, 2) if gatillo else None, "limite": limite, "trail": trail,
             "objetivo": round(t2 or entry * 1.05, 2), "qty": qty, "hasta": hasta or now + OFERTA_TTL.get(kind, 120),
             "creada": now, "estado": "oferta"}
        with self.lock:
            self._purgar(now)
            self.ofertas[o["id"]] = o
            if self.viva(t, excluir=o["id"]):
                o["estado"], o["nota"] = "omitida", f"🤖 Paper: ya hay una orden o posición en {t}."
            elif self.modo == "auto":
                motivo = self._motivo(o, now)
                if motivo:
                    o["estado"], o["nota"] = "omitida", f"🤖 Paper: no la puse, {motivo}."
                else:
                    o["estado"], o["pedida"] = "cola", now
                    o["nota"] = "🤖 Paper: orden enviada a IBKR (modo automático)."
            else:
                o["boton"] = True
            return dict(o)

    def _purgar(self, now: float):
        """Ofertas de días anteriores fuera (memoria acotada)."""
        old = [k for k, o in self.ofertas.items() if now - o["creada"] > 86400 and o["estado"] not in VIVAS]
        for k in old:
            del self.ofertas[k]

    def pedir(self, oid: str, now: float | None = None) -> tuple[bool, str]:
        """Tocaste ✅ Ejecutar: valida y la pone en la cola. Devuelve (ok, texto para la notificación del botón)."""
        now = now or self.clock()
        with self.lock:
            o = self.ofertas.get(oid)
            if not o:
                return False, "Esa orden ya no existe (el semáforo se reinició o es de otro día)."
            if o["estado"] != "oferta":
                return False, {"cola": "Ya va en camino.", "enviada": "Ya va en camino.", "puesta": "Ya está puesta.",
                               "llena": "Ya compraste.", "vencida": "Ya no vale: la jugada se dañó o venció.",
                               }.get(o["estado"], "Esa orden ya no está disponible.")
            motivo = self._motivo(o, now)
            if motivo:
                return False, f"No la puse: {motivo}."
            o["estado"], o["pedida"] = "cola", now
            return True, f"Enviando a IBKR paper: {o['t']} {o['qty']} acc."

    def cancelar_ticker(self, t: str, motivo: str):
        """El aviso se dañó o venció: la oferta deja de valer y lo que esté en camino o puesto sin llenar se cancela.
        Lo ya comprado sigue protegido por su stop que sube."""
        with self.lock:
            for o in self.ofertas.values():
                if o["t"] != t:
                    continue
                if o["estado"] == "oferta":
                    o["estado"] = "vencida"
                elif o["estado"] == "cola":  # aún no salió al ejecutor
                    o["estado"] = "cancelada"
                elif o["estado"] in ("enviada", "puesta") and not o.get("cancelar"):
                    o["cancelar"], o["cancelar_ts"] = motivo, self.clock()

    # ---------------- el ejecutor ----------------
    def sync(self, estado: dict | None, eventos: list | None, now: float | None = None) -> dict:
        """Lo que manda el ejecutor (su estado y lo que pasó) → lo que tiene que hacer (órdenes, cancelaciones,
        cierre, pausa y límites). Las órdenes se repiten hasta que el ejecutor avise algo de ellas: él descarta las que
        ya conoce."""
        now = now or self.clock()
        with self.lock:
            if not self._adoptado and isinstance(estado, dict) and "pausa" in estado:
                # Primer contacto tras un reinicio del semáforo: la pausa y el modo los guarda el ejecutor (si no, un
                # redeploy levantaría un /pausa sin avisar)
                self.pausa = bool(estado.get("pausa"))
                if estado.get("modo") in ("boton", "auto"):
                    self.modo = estado["modo"]
            self._adoptado = True
            self.ex_seen = now
            self.ex = self._limpiar_estado(estado or {})
            for e in list(eventos or [])[:200]:
                if isinstance(e, dict):
                    self._evento(e)
            for o in self.ofertas.values():
                # El ejecutor nunca recibió esta orden y ya la pedimos cancelar hace rato: no sigue ocupando cupo
                if o["estado"] == "enviada" and o.get("cancelar") and now - o.get("cancelar_ts", now) > CANCEL_ESPERA:
                    o["estado"] = "cancelada"
            ordenes = []
            for o in self.ofertas.values():
                if o["estado"] in ("cola", "enviada") and not o.get("cancelar"):
                    o["estado"] = "enviada"
                    ordenes.append({k: o[k] for k in ("id", "t", "tipo", "gatillo", "limite", "trail", "objetivo",
                                                      "qty", "hasta")})
            cancelar = [o["id"] for o in self.ofertas.values() if o.get("cancelar") and o["estado"] in ("enviada", "puesta")]
            return {"ordenes": ordenes, "cancelar": cancelar, "pausa": self.pausa, "modo": self.modo,
                    "cerrar_id": self.cerrar_id,
                    "cerrar_hace_s": round(now - self.cerrar_ts, 1) if self.cerrar_id else None,
                    "limites": {"orden_usd": ORDEN_USD, "max_abierto": MAX_ABIERTO, "perdida_max": PERDIDA_MAX,
                                "entrada_ini_m": ENTRADA_INI_M, "entrada_fin_m": ENTRADA_FIN_M, "cierre_m": CIERRE_M}}

    def adoptar_pausa(self, estado: dict | None):
        """El ejecutor de Claude habló primero tras un reinicio del semáforo: toma la pausa que él guardó. Deja pendiente
        la adopción del ejecutor de Priamo, que además guarda el modo (automático o botón)."""
        with self.lock:
            if not self._adoptado and isinstance(estado, dict) and "pausa" in estado:
                self.pausa = bool(estado.get("pausa"))

    @staticmethod
    def _limpiar_estado(e: dict) -> dict:
        out = {}
        for k in ("ib", "paper", "parado", "pausa"):
            out[k] = bool(e.get(k))
        for k in ("comprometido", "riesgo_abierto", "pnl_dia", "perdida_dia"):
            v = _num(e.get(k))
            out[k] = round(v, 2) if v is not None else None
        for k in ("ops", "gan"):                # operaciones cerradas hoy y cuántas ganó (para el marcador)
            v = _num(e.get(k))
            out[k] = int(v) if v is not None else None
        out["cuenta"] = str(e.get("cuenta") or "")[:20] or None
        out["bloqueado"] = str(e.get("bloqueado"))[:200] if e.get("bloqueado") else None
        out["version"] = str(e.get("version") or "")[:10] or None
        pos = []
        for p in list(e.get("posiciones") or [])[:20]:
            if isinstance(p, dict) and p.get("t"):
                pos.append({"t": str(p["t"])[:6], "qty": _num(p.get("qty")), "px": _num(p.get("px"))})
        out["posiciones"] = pos
        out["vivas_t"] = [str(x)[:6] for x in list(e.get("vivas_t") or [])[:20]]
        return out

    def _msg(self, key: str, text: str):
        self.msgs.append((key, text))
        del self.msgs[:-100]

    def tomar_msgs(self) -> list[tuple[str, str]]:
        with self.lock:
            out, self.msgs = self.msgs, []
            return out

    def _evento(self, e: dict):
        oid, ev = str(e.get("id") or "")[:16], str(e.get("ev") or "")
        o = self.ofertas.get(oid)
        t = str(e.get("t") or (o or {}).get("t") or "?")[:6]
        px, qty = _num(e.get("px")), _num(e.get("qty"))
        motivo = str(e.get("motivo") or "")[:200]
        if ev == "ack":
            nuevo = e.get("estado")
            if o and nuevo in ORDEN_ESTADOS and ORDEN_ESTADOS[nuevo] > ORDEN_ESTADOS.get(o["estado"], 0) >= 2:
                o["estado"] = nuevo
            return
        if ev == "puesta":
            if o and o["estado"] in ("cola", "enviada"):
                o["estado"] = "puesta"
            d = e.get("detalle") or (self._detalle(o) if o else "")
            self._msg(f"paper:{oid}:puesta", f"🟦 Paper: orden puesta en IBKR · {t} {d}".rstrip())
        elif ev == "llena":
            if o:
                o["estado"], o["px_e"], o["qty_e"] = "llena", px, qty
            self._msg(f"paper:{oid}:llena", f"📥 Paper: compré {qty or 0:g} {t} a {px or 0:.2f}. "
                                             f"Lo cuida el stop que sube y el objetivo +5%.")
        elif ev == "salida":
            pnl, pe = _num(e.get("pnl")), _num(e.get("px_e")) or (o or {}).get("px_e")
            if o:
                o["estado"] = "cerrada"
            pct = f"{100 * (px / pe - 1):+.1f}%, " if px and pe else ""
            usd = f"{pnl:+.2f} US$" if pnl is not None else ""
            icon = "✅" if (pnl or 0) >= 0 else "🛑"
            self._msg(f"paper:{oid}:salida", f"{icon} Paper: vendí {qty or 0:g} {t} a {px or 0:.2f} por "
                                              f"{POR.get(e.get('por'), 'una venta')} ({pct}{usd}).")
        elif ev == "rechazada":
            if o:
                o["estado"] = "rechazada"
            self._msg(f"paper:{oid}:rechazada", f"⚠ Paper: no se puso la orden de {t}: {motivo or 'IBKR la rechazó'}.")
        elif ev == "cancelada":
            if o:
                o["estado"] = "cancelada"
            self._msg(f"paper:{oid}:cancelada", f"⌛ Paper: cancelé la compra de {t} ({motivo or 'sin activarse'}).")
        elif ev == "parada":
            self._msg(f"paper:parada:{e.get('dia') or ''}", f"⛔ Paper: se llegó a la pérdida máxima del día "
                                                            f"(US${PERDIDA_MAX:,.0f}). No pongo más órdenes hoy.")
        elif ev == "cierre":
            self._msg(f"paper:cierre:{e.get('dia') or ''}:{e.get('motivo') or ''}",
                      f"⏰ Paper: cerré todo ({motivo or 'fin del día'}).")
        elif ev == "error":
            self._msg(f"paper:error:{motivo[:40]}", f"⚠ Paper: {motivo}")

    @staticmethod
    def _detalle(o: dict) -> str:
        compra = (f"compra stop {o['gatillo']:.2f} (límite {o['limite']:.2f})" if o.get("gatillo")
                  else f"compra límite {o['limite']:.2f}")
        return f"· {o['qty']} acc · {compra} · Trailing {o['trail']:.2f} · objetivo {o['objetivo']:.2f}"

    # ---------------- comandos de Telegram ----------------
    def comando(self, cmd: str, now: float | None = None) -> tuple[str, dict | None]:
        """Texto de respuesta y botones (o None) para cada comando."""
        now = now or self.clock()
        cmd = (cmd or "").strip().lower()
        with self.lock:
            if cmd == "/estado":
                return self.estado_txt(now), None
            if cmd in ("/pausa", "/reanuda", "/auto", "/boton", "/botón"):
                self._adoptado = True   # una orden tuya vale más que lo que guardó el ejecutor
            if cmd == "/pausa":
                self.pausa = True
                return ("⏸ Paper en pausa (tu ejecutor y el de Claude): no pongo órdenes nuevas. Lo que ya está puesto "
                        "sigue con su stop. /reanuda para seguir."), None
            if cmd == "/reanuda":
                self.pausa = False
                return "▶ Paper activo otra vez (tu ejecutor y el de Claude).", None
            if cmd == "/auto":
                self.modo = "auto"
                return ("🤖 Modo automático: pongo en paper cada ARMA y COMPRA que pase los límites, sin preguntarte. "
                        "/boton para volver a decidir tú."), None
            if cmd in ("/boton", "/botón"):
                self.modo = "boton"
                return "✅ Modo botón: cada aviso trae «✅ Ejecutar en paper» y solo pongo las que toques.", None
            if cmd == "/cerrar":
                self.confirma = (secrets.token_hex(4), now)
                return ("¿Cierro todo lo de paper ahora, lo tuyo y lo de Claude? Cancelo las compras pendientes y vendo a "
                        "mercado lo comprado. (Este botón vale 2 min.)",
                        {"inline_keyboard": [[{"text": "Sí, cerrar todo", "callback_data": f"c:si:{self.confirma[0]}"},
                                              {"text": "No", "callback_data": "c:no"}]]})
            return self.ayuda(), None

    def confirmar_cierre(self, codigo: str | None, now: float | None = None) -> str:
        """El botón «Sí, cerrar todo» de ESTE /cerrar (un botón viejo o de otro /cerrar no cierra nada)."""
        now = now or self.clock()
        with self.lock:
            if not self.confirma or codigo != self.confirma[0] or now - self.confirma[1] > CONFIRMA_TTL:
                return "Ese botón ya venció. Si quieres cerrar todo, escribe /cerrar otra vez."
            self.confirma = None
            self.cerrar_id, self.cerrar_ts = secrets.token_hex(4), now
            for o in self.ofertas.values():
                if o["estado"] == "oferta":
                    o["estado"] = "vencida"
                elif o["estado"] == "cola":
                    o["estado"] = "cancelada"
                elif o["estado"] in ("enviada", "puesta") and not o.get("cancelar"):
                    o["cancelar"], o["cancelar_ts"] = "cerrar todo", now
            ok = self.disponible(now)
        return ("🧹 Cerrando todo en paper…" if ok else
                "🧹 Cierre pedido: lo hago en cuanto el ejecutor de tu PC se conecte (vale 5 min).")

    def ayuda(self) -> str:
        return ("Paper (dinero simulado): toca «✅ Ejecutar en paper» en un aviso y la orden se pone sola en IBKR.\n"
                "/estado · cómo va (posiciones, P/L, límites)\n/pausa · no pongo órdenes nuevas\n/reanuda · sigo\n"
                "/auto · pongo cada ARMA y COMPRA sin preguntarte\n/boton · solo las que toques\n"
                "/cerrar · cancelo y vendo todo ya (también lo de Claude)\n"
                "/claude · cómo va el ejecutor de Claude\n/marcador · el día: tú contra Claude\n"
                f"Límites: US${ORDEN_USD:,.0f} por operación · US${MAX_ABIERTO:,.0f} comprometidos · pérdida máx "
                f"US${PERDIDA_MAX:,.0f} al día · compras {hhmm(ENTRADA_INI_M)}–{hhmm(ENTRADA_FIN_M)} ET · "
                f"cierro todo a las {hhmm(CIERRE_M)} ET.")

    def estado_txt(self, now: float | None = None) -> str:
        now = now or self.clock()
        with self.lock:
            ex = self.ex
            modo = "automático" if self.modo == "auto" else "botón"
            lines = [f"📊 Paper · modo {modo}{' · EN PAUSA' if self.pausa else ''}"]
            if self.disponible(now):
                lines.append(f"Ejecutor conectado (cuenta {ex.get('cuenta') or '?'})")
            elif ex.get("bloqueado"):
                lines.append(f"Ejecutor bloqueado: {ex['bloqueado']}")
            elif self.ex_seen:
                lines.append(f"Ejecutor sin conexión (última vez hace {int((now - self.ex_seen) // 60)} min)")
            else:
                lines.append("Ejecutor sin conexión: abre ARRANCAR_PAPER.bat en tu PC (abre IB Gateway paper y los dos ejecutores)")
            pnl = ex.get("pnl_dia")
            ops = f" · {ex['ops']} ops ({ex.get('gan') or 0} ganadas)" if ex.get("ops") else ""
            lines.append(f"Comprometido US${ex.get('comprometido') or 0:,.0f} de US${MAX_ABIERTO:,.0f} · "
                         f"P/L hoy {pnl or 0:+.2f} US${ops} · pérdida máx US${PERDIDA_MAX:,.0f}"
                         + (" · ⛔ parado por pérdida" if ex.get("parado") else ""))
            pos = ex.get("posiciones") or []
            lines.append("Posiciones: " + (", ".join(f"{p['t']} {p['qty'] or 0:g} @{p['px'] or 0:.2f}" for p in pos)
                                           if pos else "ninguna"))
            vivas = [o for o in self.ofertas.values() if o["estado"] in ("cola", "enviada", "puesta")]
            if vivas:
                lines.append("Órdenes en camino o puestas: " + ", ".join(f"{o['t']} ({o['estado']})" for o in vivas))
            return "\n".join(lines)

    def status(self, now: float | None = None) -> dict:
        """Para /health: sin precios ni posiciones."""
        now = now or self.clock()
        with self.lock:
            return {"modo": self.modo, "pausa": self.pausa, "ejecutor": self.disponible(now),
                    "visto_s": round(now - self.ex_seen, 1) if self.ex_seen else None,
                    "bloqueado": self.ex.get("bloqueado"), "parado": self.ex.get("parado"),
                    "ofertas": sum(1 for o in self.ofertas.values() if o["estado"] == "oferta"),
                    "vivas": sum(1 for o in self.ofertas.values() if o["estado"] in VIVAS)}


def boton(o: dict | None) -> dict | None:
    """Teclado de Telegram del aviso: un botón que manda el id de la oferta (los datos de la orden quedan en el
    semáforo, así nadie puede inventar una orden con el botón)."""
    if not o or not o.get("boton") or not ID.fullmatch(o.get("id") or ""):
        return None
    return {"inline_keyboard": [[{"text": f"✅ Ejecutar en paper · {o['qty']} acc (~US${o['qty'] * o['limite']:,.0f})",
                                  "callback_data": f"x:{o['id']}"}]]}
