"""Ejecutor de órdenes en la cuenta PAPER de IBKR (dinero simulado). Corre en la PC de Priamo junto a IB Gateway paper.

CANDADO: solo se conecta al puerto 4002 (IB Gateway en modo paper) y solo opera si TODAS las cuentas de la sesión son
de paper (empiezan por DU). No hay variable ni archivo que lo apunte a la cuenta real: haría falta cambiar este código.

Cada segundo le pregunta al semáforo (Render) si hay órdenes nuevas: las que tocaste con «✅ Ejecutar en paper» (o
todas, en modo automático). Las pone en IBKR como una orden con dos hijas (bracket estándar de IBKR):
  - compra: Stop Limit (aviso ARMA: compra sola al romper) o Limit (COMPRA y ⚡), válida hasta la hora del aviso (GTD,
    nunca después de las 12:00 ET);
  - Trailing: stop que sube con el precio a la distancia del stop del plan y nunca baja;
  - objetivo: venta límite +5 %. Si se ejecuta una hija, IBKR cancela la otra.
Y le devuelve al semáforo lo que pasa (puesta, comprada, vendida, rechazada) para que te llegue por Telegram.

Seguridad, en cada vuelta:
  - lo que cuenta es lo que IBKR ejecutó (compras − ventas de sus órdenes), no el estado que diga una orden;
  - si hay acciones compradas sin un stop vivo que las cubra, lo confirma con IBKR y, si de verdad falta, las vende;
  - si alguna vez vendiera de más (quedaría en corto), recompra la diferencia y avisa;
  - los manejadores de eventos de IBKR solo anotan: todo lo que toca IBKR se hace aquí, en el ciclo.
Límites propios (si el semáforo manda límites más estrictos, usa esos): US$500 por operación · US$1,000 comprometidos
(posiciones + compras puestas) · no pone una orden si la pérdida del día más lo que podría perder lo abierto pasaría de
US$100, y al perder US$100 se detiene el resto del día · compras solo 9:30–12:00 ET · una orden viva por acción ·
20 órdenes por día · a las 15:55 ET cancela lo suyo y vende lo que compró. Nunca toca órdenes ni posiciones que no haya
puesto él: las reconoce por su referencia "sem-<id>-<rol>".

Uso (Windows): doble clic en ARRANCAR_PAPER.bat (abre IB Gateway paper y este ejecutor; lo que ya esté abierto lo deja
como está). Usa la misma clave del puente (puente.env). Ctrl+C para salir. Registro: ejecutor.log; cada operación cerrada
queda también en diario_sem.csv (para compararlas con las de otros ejecutores).

Este archivo es el motor: bridge/ejecutor_claude.py lo reutiliza (otra estrategia, otro prefijo de órdenes "cla-", mismos
límites y mismas protecciones) para correr en paralelo en la misma cuenta paper sin tocarse.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import logging
import logging.handlers
import math
import os
import re
import sys
import time

try:
    from ib_async import IB, Order, Stock
except ImportError:  # las pruebas corren sin IBKR; main() avisa cómo instalarlo
    IB = Order = Stock = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from puente_ibkr import http_post, no_quickedit, read_env  # noqa: E402

VERSION = "1.3"
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "ejecutor.log")
STATE_FILE = os.path.join(HERE, "ejecutor_paper.json")

# ---- Candado y límites: fijos aquí, a propósito ----
PUERTO = 4002            # IB Gateway en modo PAPER (la cuenta real usa 4001: este programa nunca se conecta ahí)
CLIENT_ID = 41           # distinto del puente de datos (17) y de las pruebas (31, 32)
ORDEN_USD = 500.0
MAX_ABIERTO = 1000.0
PERDIDA_MAX = 100.0
ENTRADA_INI_M = 9 * 60 + 30
ENTRADA_FIN_M = 12 * 60
CIERRE_M = 15 * 60 + 55
MAX_ORDENES_DIA = 20
CERRAR_TTL = 300         # s: un /cerrar más viejo que esto (p. ej. de antes de arrancar) no se ejecuta
CANCEL_ESPERA = 5        # s máximos esperando que IBKR confirme una cancelación antes de recontar
AVISOS = {161, 202, 399, 404, 2109, 10089, 10090, 10148, 10167, 10168, 10197, 10349}  # informativos, no son problema
ACTIVOS = {"PendingSubmit", "ApiPending", "PreSubmitted", "Submitted"}
FIN = {"Cancelled", "ApiCancelled", "Filled", "Inactive"}
REF = re.compile(r"sem-([0-9a-f]{8})-([etoxc])")   # e compra · t trailing · o objetivo · x venta de cierre · c recompra
ID = re.compile(r"[0-9a-f]{8}")
SYM = re.compile(r"[A-Z]{1,5}")
MUERTA = ("", "")        # orderId de una orden reemplazada: sus errores ya no importan
EPS = 1e-9

log = logging.getLogger("ejecutor")

try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001 (Windows sin la base de zonas horarias)
    NY = None


def _ny_offset(utc: dt.datetime) -> int:
    """Horas de Nueva York respecto a UTC con las reglas de EE. UU. (verano: del 2.º domingo de marzo al 1.er domingo
    de noviembre, a las 2:00 locales)."""
    mar = dt.datetime(utc.year, 3, 8, 7, tzinfo=dt.timezone.utc)
    nov = dt.datetime(utc.year, 11, 1, 6, tzinfo=dt.timezone.utc)
    start = mar + dt.timedelta(days=(6 - mar.weekday()) % 7)
    end = nov + dt.timedelta(days=(6 - nov.weekday()) % 7)
    return -4 if start <= utc < end else -5


def et(ts: float) -> dt.datetime:
    if NY is not None:
        return dt.datetime.fromtimestamp(ts, NY)
    u = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return u.astimezone(dt.timezone(dt.timedelta(hours=_ny_offset(u))))


def et_ts(ts: float, minute: int) -> float:
    """Epoch del día ET de `ts` a la hora `minute` ET."""
    return et(ts).replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0).timestamp()


def gtd(ts: float) -> str:
    """Hora de vencimiento en el formato de IBKR: 'AAAAMMDD HH:MM:SS US/Eastern'."""
    return f"{et(ts):%Y%m%d %H:%M:%S} US/Eastern"


def hhmm(m: int) -> str:
    return f"{m // 60}:{m % 60:02d}"


def fnum(v) -> float | None:
    """Número finito (acepta 0); None si no lo es."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def mantener_despierto(activo: bool):
    """Pide a Windows que no se duerma mientras este programa corre en horario de mercado (SetThreadExecutionState: vale
    solo para este programa y se acaba al cerrarlo; no cambia ningún ajuste de energía). Fuera de Windows no hace nada."""
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if activo else 0))  # CONTINUOUS | SYSTEM
    except Exception:  # noqa: BLE001
        pass


def configurar_log(archivo: str):
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    handlers = [logging.StreamHandler()]
    try:
        handlers.append(logging.handlers.RotatingFileHandler(archivo, maxBytes=1_000_000, backupCount=2,
                                                             encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", handlers=handlers)
    logging.getLogger("ib_async").setLevel(logging.ERROR)


DIARIO_COLS = ["fecha", "hora", "ejecutor", "evento", "simbolo", "qty", "px_entrada", "px_salida", "pnl_usd",
               "comision_usd", "riesgo_usd", "R", "por", "setup", "motivo"]


class Ejecutor:
    # Lo que cambia entre los ejecutores que comparten este motor (ejecutor_claude.py lo redefine)
    PREFIJO = "sem"                 # las órdenes llevan la referencia <prefijo>-<id>-<rol>; nunca toca las de otro prefijo
    CLIENT_ID = CLIENT_ID
    RUTA = "/api/paper/sync"
    NOMBRE = "semáforo"
    DIARIO = "diario_sem.csv"
    ORDEN_USD, MAX_ABIERTO, PERDIDA_MAX = ORDEN_USD, MAX_ABIERTO, PERDIDA_MAX
    ENTRADA_INI_M, ENTRADA_FIN_M, CIERRE_M = ENTRADA_INI_M, ENTRADA_FIN_M, CIERRE_M
    MAX_ORDENES_DIA = MAX_ORDENES_DIA

    def __init__(self, cfg: dict, ib, post=http_post, reloj=time.time, estado_path: str = STATE_FILE,
                 diario_path: str | None = None):
        self.ib, self.post, self.reloj, self.path = ib, post, reloj, estado_path
        self.diario = diario_path or (os.path.join(HERE, self.DIARIO) if estado_path == STATE_FILE
                                      else estado_path + ".diario.csv")
        self.ref_re = re.compile(rf"{re.escape(self.PREFIJO)}-([0-9a-f]{{8}})-([etoxc])")
        self.url = cfg["SEMAFORO_URL"].rstrip("/") + self.RUTA
        self._despierto: bool | None = None
        self.token = cfg["BRIDGE_TOKEN"]
        self.cuenta: str | None = None
        self.paper = False                     # conectado a IBKR y con todas las cuentas paper
        self.bloqueado: str | None = None      # motivo si el candado impidió operar
        self.dia: str | None = None
        self.ord: dict[str, dict] = {}         # id de la orden del semáforo -> lo que se hizo con ella hoy
        self.por_oid: dict[int, tuple[str, str]] = {}   # orderId de IBKR -> (id, rol)
        self.contratos: dict[str, object] = {}
        self.eventos: list[dict] = []          # lo que pasó y el semáforo aún no recibió (se reintenta)
        self.err_q: list[tuple[int, int, str]] = []   # errores de IBKR anotados por el manejador, se atienden en paso()
        self.pausa = False
        self.modo: str | None = None
        self.lim: dict = {}                    # límites del semáforo (se usa el más estricto)
        self.cerrar_visto: str | None = None
        self.cerrado_hoy = False
        self.parado = False
        self.cancel_ids: list[str] = []        # cancelaciones de órdenes que aún no llegaron (no se ponen si llegan)
        self.ack_t: dict[str, float] = {}
        self.err_red: str | None = None
        self.err_ib: str | None = None
        self.fallos_ib = 0                     # intentos seguidos de conectar con IB Gateway que fallaron
        self.errores_vistos: set[tuple[str, int]] = set()
        self._vivas: set[int] | None = None    # orderIds que IBKR dice abiertos (se pide solo si hace falta)
        self._hooked = False
        self.cargar()

    # ---------------- estado del día (sobrevive reinicios del programa) ----------------
    def cargar(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, ValueError):
            return
        self.cerrar_visto = s.get("cerrar_visto")
        self.pausa, self.modo = bool(s.get("pausa")), s.get("modo")
        hoy = et(self.reloj()).date().isoformat()
        if s.get("dia") == hoy:
            self.dia = hoy
            self.ord = {k: v for k, v in (s.get("ord") or {}).items() if ID.fullmatch(k) and isinstance(v, dict)}
            self.cerrado_hoy, self.parado = bool(s.get("cerrado_hoy")), bool(s.get("parado"))
            for oid, o in self.ord.items():
                for rol, n in (o.get("oids") or {}).items():
                    self.por_oid[int(n)] = (oid, rol)
                for n in o.get("muertas") or []:
                    self.por_oid[int(n)] = MUERTA

    def guardar(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"dia": self.dia, "ord": self.ord, "cerrar_visto": self.cerrar_visto, "pausa": self.pausa,
                           "modo": self.modo, "cerrado_hoy": self.cerrado_hoy, "parado": self.parado}, f,
                          ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("No pude guardar %s: %s", self.path, e)

    def nuevo_dia(self, dia: str):
        if self.dia and self.dia != dia and any(abs(self.abierta(o)) > EPS or o["estado"] in ("puesta", "cancelando")
                                                for o in self.ord.values()):
            log.warning("Cambió el día con órdenes o posiciones de ayer sin cerrar: revísalas en IBKR.")
        self.dia, self.ord, self.por_oid, self.contratos = dia, {}, {}, {}
        self.cerrado_hoy = self.parado = False
        self.ack_t, self.errores_vistos, self.cancel_ids = {}, set(), []
        self.guardar()

    # ---------------- conexión con IBKR (con candado) ----------------
    def conectar(self) -> bool:
        if not self._hooked:
            self.ib.errorEvent += self.on_error
            self.ib.disconnectedEvent += self.on_disconnect
            self._hooked = True
        try:
            self.ib.connect("127.0.0.1", PUERTO, clientId=self.CLIENT_ID, timeout=15)
        except Exception as e:  # noqa: BLE001 (rechazada, tiempo agotado, errores de la API)
            self.paper = False
            if self.err_ib != type(e).__name__:
                log.warning("No pude conectar con IB Gateway paper en el puerto %d (%s). Ábrelo con ARRANCAR_PAPER.bat "
                            "y revisa que la API esté habilitada y sin «Read-Only API».", PUERTO, type(e).__name__)
                self.err_ib = type(e).__name__
            return False
        cuentas = [str(a) for a in (self.ib.managedAccounts() or [])]
        if not cuentas or not all(a.upper().startswith("DU") for a in cuentas):
            self.bloqueado = f"la cuenta {', '.join(cuentas) or '?'} no es paper: no opero"
            self.paper = False
            log.error("CANDADO: %s. Este programa solo opera cuentas paper (DU…).", self.bloqueado)
            self.ib.disconnect()
            return False
        self.bloqueado, self.paper, self.cuenta, self.err_ib = None, True, cuentas[0], None
        self.reconciliar()
        log.info("Conectado a IB Gateway PAPER (cuenta %s).", self.cuenta)
        return True

    def on_disconnect(self):
        self.paper = False
        log.warning("Se cortó la conexión con IB Gateway; reintento en unos segundos.")

    def reconciliar(self):
        """Tras (re)conectar: reconoce sus órdenes y ejecuciones de hoy por la referencia sem-<id>-<rol>, aunque el
        archivo de estado se haya perdido."""
        for tr in self.ib.trades():
            self._mapear(getattr(tr.order, "orderRef", ""), tr.order.orderId, tr.contract, tr.order)
        for f in self.ib.fills():
            self._mapear(getattr(f.execution, "orderRef", ""), f.execution.orderId, f.contract, None)

    def _mapear(self, ref, order_id, contract, order):
        m = self.ref_re.fullmatch(ref or "")
        if not m or self.por_oid.get(int(order_id)) == MUERTA:
            return
        oid, rol = m.group(1), m.group(2)
        self.por_oid[int(order_id)] = (oid, rol)
        o = self.ord.get(oid)
        if o is None:
            sym = str(getattr(contract, "symbol", "") or "?")
            o = self.ord[oid] = {"id": oid, "t": sym, "tipo": None, "gatillo": None, "limite": 0.0, "trail": 0.0,
                                 "objetivo": None, "qty": 0, "hasta": 0, "estado": "puesta", "oids": {},
                                 "reconstruida": True}
            log.info("Reconozco una orden mía de hoy (%s) que no estaba en el archivo de estado.", sym)
        o.setdefault("oids", {}).setdefault(rol, int(order_id))
        if contract is not None and getattr(contract, "conId", 0):
            self.contratos[oid] = contract
        if order is not None and o.get("reconstruida"):
            if rol == "e":
                o["qty"], o["limite"] = int(order.totalQuantity or 0), fnum(order.lmtPrice) or 0.0
                if o.get("stop") and o["limite"]:
                    o["trail"] = round(max(0.0, o["limite"] - o["stop"]), 2)
            elif rol == "t":
                if getattr(order, "orderType", "") == "STP":     # stop fijo: auxPrice es el precio, no la distancia
                    o["stop"] = fnum(order.auxPrice) or 0.0
                    o["trail"] = round(max(0.0, (o.get("limite") or 0.0) - o["stop"]), 2) if o.get("limite") else 0.0
                else:
                    o["trail"] = fnum(order.auxPrice) or 0.0

    # ---------------- cuentas: lo que IBKR ejecutó es la verdad ----------------
    def ejecs(self, oid: str, roles: str) -> list:
        out = []
        for f in self.ib.fills():
            m = self.ref_re.fullmatch(getattr(f.execution, "orderRef", "") or "")
            if m and m.group(1) == oid and m.group(2) in roles:
                out.append(f.execution)
        return out

    def cant(self, oid: str, roles: str) -> tuple[float, float | None]:
        ex = self.ejecs(oid, roles)
        q = sum(float(e.shares) for e in ex)
        return q, (sum(float(e.shares) * float(e.price) for e in ex) / q if q else None)

    def abierta(self, o: dict) -> float:
        """Acciones de esta orden en cartera ahora: compras (e, c) − ventas (t, o, x). Negativo = vendió de más."""
        return self.cant(o["id"], "ec")[0] - self.cant(o["id"], "tox")[0]

    def resumen(self) -> dict:
        comp = riesgo = pnl = 0.0
        pos, vivas = [], []
        ops = sum(1 for o in self.ord.values() if o["estado"] == "cerrada")
        gan = sum(1 for o in self.ord.values() if o["estado"] == "cerrada" and (o.get("pnl") or 0) > 0)
        for o in self.ord.values():
            qe, _ = self.cant(o["id"], "e")
            qb, pb = self.cant(o["id"], "ec")
            qs, ps = self.cant(o["id"], "tox")
            ab = qb - qs
            pendiente = o["estado"] in ("puesta", "cancelando")
            if pendiente:
                falta = max(0.0, (o.get("qty") or 0) - qe)
                comp += falta * (o.get("limite") or 0)
                riesgo += falta * (o.get("trail") or 0)
            if ab > EPS and pb:
                comp += ab * pb
                riesgo += ab * (o.get("trail") or 0)
                pos.append({"t": o["t"], "qty": ab, "px": round(pb, 4)})
            if qs > 0 and pb and ps:
                pnl += qs * (ps - pb)
            if pendiente or abs(ab) > EPS:
                vivas.append(o["t"])
        return {"comprometido": round(comp, 2), "riesgo_abierto": round(riesgo, 2), "pnl_dia": round(pnl, 2),
                "perdida_dia": round(max(0.0, -pnl), 2), "posiciones": pos, "vivas_t": vivas, "ops": ops, "gan": gan}

    # ---------------- límites (el más estricto entre los propios y los del semáforo) ----------------
    def tope(self, k: str, propio: float) -> float:
        v = fnum(self.lim.get(k))
        return min(propio, v) if v and v > 0 else propio

    def horario(self) -> tuple[int, int, int]:
        ini = max(self.ENTRADA_INI_M, int(fnum(self.lim.get("entrada_ini_m")) or 0))
        fin = min(self.ENTRADA_FIN_M, int(fnum(self.lim.get("entrada_fin_m")) or self.ENTRADA_FIN_M))
        cierre = min(self.CIERRE_M, int(fnum(self.lim.get("cierre_m")) or self.CIERRE_M))
        return ini, fin, cierre

    def motivo(self, o: dict, now: float) -> str | None:
        """Por qué NO poner esta orden (None = se puede)."""
        if not self.paper:
            return "el ejecutor no está conectado a IB Gateway paper"
        if self.pausa:
            return "está en pausa"
        if self.parado:
            return "ya se perdió el máximo del día"
        if self.cerrado_hoy:
            return "ya cerré el día"
        t = et(now)
        ini, fin, _ = self.horario()
        if t.weekday() >= 5 or not ini <= t.hour * 60 + t.minute < fin:
            return f"fuera del horario de compras ({hhmm(ini)}–{hhmm(fin)} ET)"
        if not SYM.fullmatch(o["t"] or ""):
            return "símbolo inválido"
        lim, trail, obj, qty, gat = o["limite"], o["trail"], o["objetivo"], o["qty"], o["gatillo"]
        if not (lim and lim > 0 and trail and trail > 0 and obj and obj > lim and qty >= 1):
            return "orden incompleta o inválida"
        if o.get("stop") is not None and not (0 < o["stop"] < lim and abs(lim - o["stop"] - trail) < 0.011):
            return "stop fijo inválido"
        if o["tipo"] not in ("stp", "lmt") or (o["tipo"] == "stp" and not (gat and 0 < gat <= lim)):
            return "tipo de orden inválido"
        if not o["hasta"] or o["hasta"] <= now:
            return "la orden ya venció"
        costo = qty * lim
        if costo > self.tope("orden_usd", self.ORDEN_USD) + 0.01:
            return f"US${costo:,.0f} pasa del máximo de US${self.tope('orden_usd', self.ORDEN_USD):,.0f} por operación"
        res = self.resumen()
        if o["t"] in res["vivas_t"]:
            return f"ya hay una orden o posición en {o['t']}"
        tope = self.tope("max_abierto", self.MAX_ABIERTO)
        if res["comprometido"] + costo > tope + 0.01:
            return f"pasaría de US${tope:,.0f} comprometidos (ya hay US${res['comprometido']:,.0f})"
        pmax = self.tope("perdida_max", self.PERDIDA_MAX)
        if res["perdida_dia"] + res["riesgo_abierto"] + qty * trail > pmax + 0.01:
            return f"podría pasar la pérdida máxima del día (US${pmax:,.0f})"
        if sum(1 for x in self.ord.values() if x.get("oids", {}).get("e")) >= self.MAX_ORDENES_DIA:
            return f"ya puse {self.MAX_ORDENES_DIA} órdenes hoy"
        return None

    # ---------------- utilidades de IBKR ----------------
    def evento(self, ev: str, oid: str | None = None, **kw):
        self.eventos.append({"ev": ev, "id": oid, **kw})
        del self.eventos[:-300]
        log.info("%s %s %s", ev.upper(), oid or "", " ".join(f"{k}={v}" for k, v in kw.items() if k != "detalle"))
        if ev in ("salida", "cancelada", "rechazada") and oid in self.ord:
            self.diario_fila(ev, self.ord[oid], kw)

    def diario_fila(self, ev: str, o: dict, kw: dict):
        """Una línea en el diario CSV (para comparar ejecutores y revisar días pasados). Nunca debe estorbar a las
        órdenes: cualquier fallo solo se anota."""
        try:
            t = et(self.reloj())
            qty = fnum(kw.get("qty")) or 0.0
            riesgo = qty * (o.get("trail") or 0.0)
            pnl = fnum(kw.get("pnl"))
            com = sum(float(getattr(getattr(f, "commissionReport", None), "commission", 0) or 0)
                      for f in self.ib.fills() if self.ref_re.fullmatch(getattr(f.execution, "orderRef", "") or "")
                      and self.ref_re.fullmatch(f.execution.orderRef).group(1) == o["id"]) if ev == "salida" else 0.0
            fila = [t.date().isoformat(), f"{t:%H:%M:%S}", self.NOMBRE, ev, o.get("t"), qty or "", kw.get("px_e") or "",
                    kw.get("px") or "", "" if pnl is None else pnl, round(com, 2) if com else "",
                    round(riesgo, 2) if riesgo else "", round(pnl / riesgo, 2) if (pnl is not None and riesgo) else "",
                    kw.get("por") or "", o.get("setup") or o.get("tipo") or "", kw.get("motivo") or ""]
            nuevo = not os.path.exists(self.diario)
            with open(self.diario, "a", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                if nuevo:
                    w.writerow(DIARIO_COLS)
                w.writerow(fila)
        except Exception as e:  # noqa: BLE001
            log.warning("No pude escribir el diario %s: %s", self.diario, e)

    @staticmethod
    def detalle(o: dict) -> str:
        compra = (f"compra stop {o['gatillo']:.2f} (límite {o['limite']:.2f})" if o["tipo"] == "stp"
                  else f"compra límite {o['limite']:.2f}")
        if o.get("stop"):
            return (f"· {o['qty']} acc · {compra} · stop {o['stop']:.2f} · objetivo {o['objetivo']:.2f}"
                    + (f" · {o['setup']}" if o.get("setup") else ""))
        return f"· {o['qty']} acc · {compra} · Trailing {o['trail']:.2f} · objetivo {o['objetivo']:.2f}"

    def _trade(self, order_id):
        if not order_id:
            return None
        for tr in self.ib.trades():
            if tr.order.orderId == order_id:
                return tr
        return None

    def _viva(self, order_id) -> bool:
        """¿Sigue viva en IBKR? El estado que guarda la librería puede quedar mal (marca «Cancelled» ante ciertos
        avisos): si dice que no, se le pregunta a IBKR una vez por vuelta antes de actuar."""
        tr = self._trade(order_id)
        if tr is not None and tr.orderStatus.status in ACTIVOS:
            return True
        if self._vivas is None:
            try:  # IBKR solo devuelve las órdenes abiertas: estar en la lista es la prueba
                self._vivas = {t.order.orderId for t in self.ib.reqOpenOrders()}
            except Exception:  # noqa: BLE001
                self._vivas = set()
        return order_id in self._vivas

    def _cancelar_oid(self, order_id):
        if not order_id:
            return
        tr = self._trade(order_id)
        try:
            self.ib.cancelOrder(tr.order if tr else Order(orderId=int(order_id)))
        except Exception as e:  # noqa: BLE001
            log.warning("No pude cancelar la orden %s: %s", order_id, e)

    def _contrato(self, o: dict):
        c = self.contratos.get(o["id"])
        if c is None:
            c = Stock(o["t"], "SMART", "USD")
            try:
                self.ib.qualifyContracts(c)
            except Exception:  # noqa: BLE001
                pass
            self.contratos[o["id"]] = c
        return c

    def _rol(self, o: dict, rol: str, order_id: int):
        o["oids"][rol] = int(order_id)
        self.por_oid[int(order_id)] = (o["id"], rol)

    # ---------------- poner ----------------
    def poner(self, raw: dict, now: float):
        oid = str(raw.get("id") or "")
        if not ID.fullmatch(oid):
            return
        _, fin, _ = self.horario()
        hasta = fnum(raw.get("hasta")) or 0
        o = {"id": oid, "t": str(raw.get("t") or "").upper().strip(), "tipo": raw.get("tipo"),
             "gatillo": fnum(raw.get("gatillo")), "limite": fnum(raw.get("limite")), "trail": fnum(raw.get("trail")),
             "objetivo": fnum(raw.get("objetivo")), "qty": int(fnum(raw.get("qty")) or 0),
             "hasta": min(hasta, et_ts(now, fin)) if hasta else 0, "estado": "nueva", "oids": {}, "recibida": now}
        if raw.get("stop") is not None:
            o["stop"] = fnum(raw.get("stop"))      # stop fijo en vez de Trailing (ejecutor de Claude)
            o["setup"] = str(raw.get("setup") or "")[:160]
        self.ord[oid] = o
        m = "se canceló antes de llegar" if oid in self.cancel_ids else self.motivo(o, now)
        c = None
        if not m:
            c = Stock(o["t"], "SMART", "USD")
            try:
                self.ib.qualifyContracts(c)
            except Exception:  # noqa: BLE001
                pass
            if not getattr(c, "conId", 0):
                m = f"IBKR no reconoce el símbolo {o['t']}"
        if m:
            o["estado"], o["motivo"] = ("cancelada" if oid in self.cancel_ids else "rechazada"), m
            self.evento(o["estado"], oid, t=o["t"], motivo=m)
            self.guardar()
            return
        self.contratos[oid] = c
        self._colocar(o, c, con_gtd=True)
        self.evento("puesta", oid, t=o["t"], detalle=self.detalle(o))
        self.guardar()

    def _colocar(self, o: dict, c, con_gtd: bool):
        """Bracket estándar de IBKR: compra + Trailing (o stop fijo) + objetivo como hijas (IBKR las enlaza: si una se ejecuta,
        cancela la otra; si una se llena en parte, reduce la otra). La compra sale con transmit=False y las hijas
        detrás: IBKR recibe las tres al transmitir la última, así nunca hay una compra sin su stop."""
        oid = o["id"]
        e = Order(action="BUY", totalQuantity=o["qty"], orderType="STP LMT" if o["tipo"] == "stp" else "LMT",
                  lmtPrice=o["limite"], orderRef=f"{self.PREFIJO}-{oid}-e", transmit=False)
        if o["tipo"] == "stp":
            e.auxPrice = o["gatillo"]
        if con_gtd:
            e.tif, e.goodTillDate = "GTD", gtd(o["hasta"])
        else:
            e.tif = "DAY"
        self.ib.placeOrder(c, e)
        if o.get("stop"):   # stop fijo (nativo de IBKR): sale al mercado si el precio toca el nivel
            t = Order(action="SELL", totalQuantity=o["qty"], orderType="STP", auxPrice=o["stop"], parentId=e.orderId,
                      tif="DAY", orderRef=f"{self.PREFIJO}-{oid}-t", transmit=False)
        else:
            t = Order(action="SELL", totalQuantity=o["qty"], orderType="TRAIL", auxPrice=o["trail"], parentId=e.orderId,
                      tif="DAY", orderRef=f"{self.PREFIJO}-{oid}-t", transmit=False)
        self.ib.placeOrder(c, t)
        ob = Order(action="SELL", totalQuantity=o["qty"], orderType="LMT", lmtPrice=o["objetivo"], parentId=e.orderId,
                   tif="DAY", orderRef=f"{self.PREFIJO}-{oid}-o", transmit=True)
        self.ib.placeOrder(c, ob)
        o["oids"] = {}
        for rol, x in (("e", e), ("t", t), ("o", ob)):
            self._rol(o, rol, x.orderId)
        o["estado"], o["puesta"], o["gtd"] = "puesta", self.reloj(), con_gtd

    # ---------------- cancelar, ajustar, vender ----------------
    def cancelar(self, oid: str, motivo: str):
        """Pide cancelar la compra (IBKR cancela sus hijas). El resultado se cuenta en revisar(), cuando IBKR confirma
        (o a los CANCEL_ESPERA s): un llenado que llegó justo antes de la cancelación no se pierde."""
        o = self.ord.get(oid)
        if not o or o["estado"] != "puesta":
            return
        self._cancelar_oid(o["oids"].get("e"))
        o["estado"], o["cancel_ts"], o["motivo"] = "cancelando", self.reloj(), motivo
        self.guardar()

    def _ajustar_hijas(self, o: dict, qty: float):
        """Las hijas venden exactamente lo que hay en cartera (si no, podrían vender de más y quedar en corto). El
        total de una orden incluye lo que ya ejecutó: lo ya vendido por esa hija + lo que falta cubrir."""
        for rol in ("t", "o"):
            tr = self._trade(o["oids"].get(rol))
            if tr is None or not self._viva(tr.order.orderId):
                continue
            tr.order.totalQuantity = self.cant(o["id"], rol)[0] + qty
            tr.order.transmit = True   # la hija se creó con transmit=False: la modificación tiene que salir a IBKR
            self.ib.placeOrder(tr.contract, tr.order)

    def pedir_venta(self, o: dict, motivo: str):
        """Fase 1 de una venta de cierre: cancela las hijas. La venta a mercado sale en una vuelta posterior, ya con
        las cancelaciones confirmadas: si una hija alcanzó a vender, no se vende de más."""
        if o.get("vender_ts"):
            return
        for rol in ("t", "o"):
            if o["oids"].get(rol):
                self._cancelar_oid(o["oids"][rol])
        o["vender"], o["vender_ts"] = motivo, self.reloj()
        self.guardar()

    def _vender(self, o: dict, ab: float, now: float):
        """Fase 2: vende a mercado lo que quede, una orden a la vez (como máximo 3 intentos)."""
        if now - o["vender_ts"] < 1.5:
            return
        x = o["oids"].get("x")
        if x and self._viva(x):
            return
        if o.get("ventas", 0) >= 3:
            if not o.get("aviso_venta"):
                o["aviso_venta"] = True
                self.evento("error", o["id"], t=o["t"], motivo=f"No pude vender {o['t']} ({ab:g} acc). Ciérrala tú en IBKR.")
            return
        orden = Order(action="SELL", totalQuantity=ab, orderType="MKT", tif="DAY", orderRef=f"{self.PREFIJO}-{o['id']}-x")
        self.ib.placeOrder(self._contrato(o), orden)
        self._rol(o, "x", orden.orderId)
        o["ventas"] = o.get("ventas", 0) + 1
        log.info("Vendo a mercado %g %s (%s).", ab, o["t"], o.get("vender"))
        self.guardar()

    def _recomprar(self, o: dict, falta: float):
        """Vendió de más (quedó en corto): recompra la diferencia a mercado, una orden a la vez."""
        c = o["oids"].get("c")
        if c and self._viva(c):
            return
        if o.get("recompras", 0) >= 3:
            return
        if not o.get("aviso_corto"):
            o["aviso_corto"] = True
            self.evento("error", o["id"], t=o["t"], motivo=f"{o['t']}: se vendió de más ({falta:g} acc en corto); "
                                                           f"recompro la diferencia")
        orden = Order(action="BUY", totalQuantity=falta, orderType="MKT", tif="DAY", orderRef=f"{self.PREFIJO}-{o['id']}-c")
        self.ib.placeOrder(self._contrato(o), orden)
        self._rol(o, "c", orden.orderId)
        o["recompras"] = o.get("recompras", 0) + 1
        self.guardar()

    def _proteger(self, o: dict, ab: float):
        """Toda posición tiene que tener su Trailing vivo y del tamaño justo. Sin stop dos vueltas seguidas (y
        confirmado con IBKR) se vende; con otro tamaño dos vueltas seguidas se ajusta."""
        n = o["oids"].get("t")
        tr = self._trade(n)
        q_t = (float(tr.order.totalQuantity) - self.cant(o["id"], "t")[0]) if tr is not None else 0.0
        if not (n and q_t > EPS and self._viva(n)):
            o["sin_stop"] = o.get("sin_stop", 0) + 1
            if o["sin_stop"] >= 2:
                self.evento("error", o["id"], t=o["t"], motivo=f"{o['t']} quedó sin su stop que sube: vendo la posición")
                self.pedir_venta(o, "quedó sin stop")
            return
        o["sin_stop"] = 0
        if abs(q_t - ab) > EPS:
            o["dif"] = o.get("dif", 0) + 1
            if o["dif"] >= 2:
                log.info("Ajusto las hijas de %s a %g acciones (tenían %g).", o["t"], ab, q_t)
                self._ajustar_hijas(o, ab)
                o["dif"] = 0
        else:
            o["dif"] = 0

    def cerrar_todo(self, motivo: str):
        algo = False
        for o in list(self.ord.values()):
            if o["estado"] == "puesta":
                self.cancelar(o["id"], motivo)
                algo = True
            o["cerrar"] = motivo   # lo que se llene más tarde hoy también se vende
            if self.abierta(o) > EPS:
                self.pedir_venta(o, motivo)
                algo = True
        if algo:
            self.evento("cierre", None, motivo=motivo, dia=self.dia)
        self.guardar()

    # ---------------- seguimiento (cada vuelta) ----------------
    def revisar(self, now: float):
        for o in list(self.ord.values()):
            oid = o["id"]
            qe, pe = self.cant(oid, "e")
            if o["estado"] == "puesta":
                if qe > 0 and qe >= (o.get("qty") or qe):
                    o["estado"], o["qty_e"], o["px_e"] = "llena", qe, pe
                    self.evento("llena", oid, t=o["t"], px=round(pe, 4), qty=qe)
                elif o.get("hasta") and now > o["hasta"] + 2:   # 2 s de gracia sobre el GTD de IBKR
                    self.cancelar(oid, "venció sin activarse")
            if o["estado"] == "cancelando":
                tr = self._trade(o["oids"].get("e"))
                if tr is None or tr.orderStatus.status in FIN or now - o.get("cancel_ts", now) >= CANCEL_ESPERA:
                    qe, pe = self.cant(oid, "e")
                    if qe > 0:
                        if qe < (o.get("qty") or qe):
                            self._ajustar_hijas(o, max(0.0, self.abierta(o)))   # lo que queda, no lo comprado
                        o["estado"], o["qty_e"], o["px_e"] = "llena", qe, pe
                        self.evento("llena", oid, t=o["t"], px=round(pe, 4), qty=qe, parcial=qe < (o.get("qty") or qe))
                    else:
                        o["estado"] = "cancelada"
                        self.evento("cancelada", oid, t=o["t"], motivo=o.get("motivo") or "cancelada")
            qb, pb = self.cant(oid, "ec")
            qs, ps = self.cant(oid, "tox")
            ab = qb - qs
            if ab < -EPS:
                # Vendió de más (en corto), en cualquier estado: emergencia. No compra ni vende nada más por las
                # órdenes de siempre y lleva la posición a cero (recompra lo que falte; si luego sobra, lo vende)
                if not o.get("corto"):
                    o["corto"] = True
                    for rol in ("e", "t", "o"):
                        if o["oids"].get(rol):
                            self._cancelar_oid(o["oids"][rol])
                    o["estado"], o["vender"], o["vender_ts"] = "llena", "quedó en corto", o.get("vender_ts") or now
                self._recomprar(o, -ab)
                continue
            if o["estado"] in ("cancelada", "rechazada", "cerrada") and ab > EPS:
                # Llenado tardío (llegó después de cancelar o de cerrar): es una posición y se cuida como tal
                o["estado"], o["qty_e"], o["px_e"] = "llena", ab, pb
                self.evento("llena", oid, t=o["t"], px=round(pb, 4), qty=ab, tarde=True)
            if o["estado"] != "llena":
                continue
            if ab < -EPS:
                self._recomprar(o, -ab)
            elif ab <= EPS:
                if qb > 0:
                    roles = {self.ref_re.fullmatch(e.orderRef).group(2) for e in self.ejecs(oid, "tox")}
                    por = ("cierre" if "x" in roles else ("stop" if o.get("stop") else "trailing") if "t" in roles
                           else "objetivo")
                    pnl = qs * ps - qb * pb
                    o["estado"], o["pnl"] = "cerrada", round(pnl, 2)
                    self.evento("salida", oid, t=o["t"], px=round(ps, 4), qty=qs, por=por, pnl=round(pnl, 2),
                                px_e=round(pb, 4))
            elif o.get("vender_ts"):
                self._vender(o, ab, now)
            elif o.get("cerrar") or self.cerrado_hoy:
                self.pedir_venta(o, o.get("cerrar") or "fin del día")
            else:
                self._proteger(o, ab)
        self.guardar()

    # ---------------- errores de IBKR ----------------
    def on_error(self, req_id, code, msg, contract=None):
        """Manejador de IBKR: SOLO anota. Desde aquí no se puede esperar ni pedir nada a IBKR (la librería corre su
        bucle de eventos); el error se atiende en la próxima vuelta del ciclo."""
        try:
            code = int(code)
        except (TypeError, ValueError):
            return
        if code in AVISOS or 2100 <= code < 2200:
            return
        if code in (1100, 1300):
            log.warning("IBKR %s: %s", code, msg)
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(msg))).strip()[:200]
        self.err_q.append((req_id if isinstance(req_id, int) else -1, code, txt))
        del self.err_q[:-200]

    def atender_errores(self):
        q, self.err_q = self.err_q, []
        for req_id, code, txt in q:
            k = self.por_oid.get(req_id)
            if k == MUERTA:
                continue
            if not k:
                if code not in (1100, 1101, 1102, 1300):
                    log.info("IBKR %s: %s", code, txt)
                continue
            oid, rol = k
            o = self.ord.get(oid)
            if not o:
                continue
            solo_lectura = "read-only" in txt.lower() or "read only" in txt.lower()
            if solo_lectura and rol == "e":
                txt = ("IB Gateway paper está en solo lectura: desmarca «Read-Only API» en Configure → Settings → API → "
                       "Settings (o vuelve a abrir ARRANCAR_PAPER.bat)")
            if not (code in (201, 203) or "reject" in txt.lower() or solo_lectura):
                if (oid, code) not in self.errores_vistos:
                    self.errores_vistos.add((oid, code))
                    log.warning("IBKR %s sobre %s (%s): %s", code, o["t"], rol, txt)
                continue
            ab = self.abierta(o)
            if rol == "e":
                if o["estado"] in ("puesta", "cancelando") and self.cant(oid, "e")[0] == 0:
                    if o.get("gtd") and not o.get("reintento") and re.search(r"gtd|good.?till|goodtilldate", txt, re.I):
                        # IBKR no aceptó el vencimiento: se reemplaza por una DAY que vence este programa. Las órdenes
                        # viejas se cancelan y sus errores se ignoran (si no, cancelarían la nueva)
                        log.warning("IBKR no aceptó el vencimiento GTD de %s; la pongo como DAY: %s", o["t"], txt)
                        o["reintento"] = True
                        o.setdefault("muertas", [])
                        for n in list(o["oids"].values()):
                            self.por_oid[int(n)] = MUERTA
                            o["muertas"].append(int(n))
                            self._cancelar_oid(n)
                        self._colocar(o, self._contrato(o), con_gtd=False)
                        self.guardar()
                        continue
                    o["estado"], o["motivo"] = "rechazada", txt
                    self.evento("rechazada", oid, t=o["t"], motivo=txt)
                    self.guardar()
            elif rol in ("t", "o"):
                # Una compra no puede quedar sin su stop: si IBKR rechaza una hija, se cancela la compra o se vende
                m = f"IBKR rechazó la orden de {'stop' if rol == 't' else 'objetivo'} ({txt})"
                if ab > EPS:
                    self.evento("error", oid, t=o["t"], motivo=f"{o['t']}: {m}; vendo la posición")
                    self.pedir_venta(o, m)
                elif o["estado"] == "puesta":
                    self.cancelar(oid, m)
            else:
                que = "la venta de cierre" if rol == "x" else "la recompra"
                self.evento("error", oid, t=o["t"], motivo=f"IBKR rechazó {que} de {o['t']} ({txt}). Revísala en IBKR.")

    # ---------------- una vuelta ----------------
    def paso(self) -> float:
        now = self.reloj()
        t = et(now)
        m = t.hour * 60 + t.minute
        dia = t.date().isoformat()
        if dia != self.dia:
            self.nuevo_dia(dia)
        despierto = t.weekday() < 5 and 8 * 60 + 30 <= m < 16 * 60 + 30
        if despierto != self._despierto:   # Windows no se duerme mientras el mercado está abierto (y la PC está prendida)
            self._despierto = despierto
            mantener_despierto(despierto)
        if not self.ib.isConnected():
            self.paper = False
            if not self.conectar():
                self.fallos_ib += 1
                self.sync(now)  # el semáforo ve «desconectado» (o el candado) y no ofrece botón
                en_horas = t.weekday() < 5 and 9 * 60 + 20 <= m < 16 * 60 + 5
                # IB Gateway tarda 1-2 min en volver tras su reinicio de cada noche: con el mercado abierto se insiste
                # cada 10 s; fuera de horario, tras unos intentos, se espera más para no llenar el registro.
                return 10 if en_horas or self.fallos_ib <= 6 else 30
            if self.fallos_ib:
                log.info("Reconectado con IB Gateway tras %d intento(s) fallido(s).", self.fallos_ib)
            self.fallos_ib = 0
        self._vivas = None
        self.atender_errores()
        self.revisar(now)
        res = self.resumen()
        if not self.parado and res["perdida_dia"] >= self.tope("perdida_max", self.PERDIDA_MAX) - 0.01:
            self.parado = True
            for o in list(self.ord.values()):
                if o["estado"] == "puesta":
                    self.cancelar(o["id"], "se llegó a la pérdida máxima del día")
            self.evento("parada", None, dia=dia, perdida=res["perdida_dia"])
            self.guardar()
            res = self.resumen()
        _, _, cierre = self.horario()
        if m >= cierre and not self.cerrado_hoy:
            self.cerrar_todo(f"{hhmm(cierre)} ET, fin del día")
            self.cerrado_hoy = True
            self.guardar()
            res = self.resumen()
        resp = self.sync(now, res)
        if resp is not None:
            self.atender(resp, now)
        return 1 if t.weekday() < 5 and 9 * 60 + 20 <= m < 16 * 60 + 5 else 10

    def sync(self, now: float, res: dict | None = None) -> dict | None:
        if res is None:
            res = self.resumen() if self.paper else {}
        estado = {"ib": bool(self.paper), "paper": self.paper, "cuenta": self.cuenta, "bloqueado": self.bloqueado,
                  "parado": self.parado, "pausa": self.pausa, "modo": self.modo, "version": VERSION, **res,
                  **self.extra_estado()}
        lote = self.eventos[:100]
        try:
            out = self.post(self.url, self.token, {"v": 1, "estado": estado, "eventos": lote}, timeout=5)
        except PermissionError as e:
            if self.err_red != str(e):
                log.error("%s", e)
                self.err_red = str(e)
            return None
        except ConnectionError as e:
            if self.err_red != str(e):
                log.warning("%s; reintento.", e)
                self.err_red = str(e)
            return None
        if self.err_red:
            log.info("Conectado con el semáforo otra vez.")
            self.err_red = None
        del self.eventos[:len(lote)]
        return out if isinstance(out, dict) else None

    def extra_estado(self) -> dict:
        """Campos propios de cada ejecutor para el estado que se manda al semáforo (el de Claude añade los suyos)."""
        return {}

    def atender(self, resp: dict, now: float):
        pausa, modo = bool(resp.get("pausa")), resp.get("modo") if resp.get("modo") in ("boton", "auto") else self.modo
        if (pausa, modo) != (self.pausa, self.modo):
            self.pausa, self.modo = pausa, modo   # se guarda: si el semáforo se reinicia, lo recupera de aquí
            self.guardar()
        self.lim = resp.get("limites") if isinstance(resp.get("limites"), dict) else {}
        cid, hace = resp.get("cerrar_id"), fnum(resp.get("cerrar_hace_s"))
        if cid and cid != self.cerrar_visto:
            self.cerrar_visto = cid
            if hace is not None and hace <= CERRAR_TTL:
                self.cerrar_todo("lo pediste con /cerrar")
            self.guardar()
        for oid in list(resp.get("cancelar") or [])[:50]:
            oid = str(oid)
            if not ID.fullmatch(oid):
                continue
            if oid in self.ord:
                self.cancelar(oid, "el semáforo la canceló: la jugada se dañó o venció")
                if self.ord[oid]["estado"] != "puesta" and now - self.ack_t.get(oid, 0) >= 10:
                    self.ack_t[oid] = now   # el semáforo sigue pidiendo cancelarla: le repito cómo terminó
                    self.evento("ack", oid, estado=self.ord[oid]["estado"])
            elif oid not in self.cancel_ids:
                # Nunca llegó: queda anotada (si llega después, no se pone) y el semáforo se entera
                self.cancel_ids = (self.cancel_ids + [oid])[-200:]
                self.evento("cancelada", oid, motivo="se canceló antes de llegar a IBKR")
        for raw in list(resp.get("ordenes") or [])[:20]:
            if not isinstance(raw, dict):
                continue
            oid = str(raw.get("id") or "")
            if oid in self.ord:
                if now - self.ack_t.get(oid, 0) >= 10:  # el semáforo no supo qué pasó: se lo repito
                    self.ack_t[oid] = now
                    self.evento("ack", oid, estado=self.ord[oid]["estado"])
                continue
            self.poner(raw, now)

    def salir(self):
        """Al cerrar el programa: cancela las compras que aún no se llenaron (nadie las vigilaría). Lo comprado queda
        en IBKR con su stop y su objetivo hasta las 16:00."""
        if not self.ib.isConnected():
            return
        for o in list(self.ord.values()):
            if o["estado"] == "puesta":
                self.cancelar(o["id"], "cerraste el ejecutor")
        abiertas = [o["t"] for o in self.ord.values() if self.abierta(o) > EPS]
        if abiertas:
            log.warning("Quedan posiciones paper con su stop en IBKR: %s. Vuelve a abrir el ejecutor antes de las "
                        "15:55 ET para que las cierre, o ciérralas tú.", ", ".join(abiertas))


def esperar(ib, segundos: float):
    """Espera procesando lo que llega de IBKR. Si el Gateway se cae (o se reinicia, como cada noche a las 23:30), la
    librería convierte el corte en un ConnectionError que sale justo de esta espera: no debe matar al programa, el
    siguiente paso() ve que no hay conexión y reconecta."""
    try:
        ib.sleep(segundos)
    except (Exception, asyncio.CancelledError) as e:   # noqa: BLE001 (Ctrl+C no entra aquí: es BaseException)
        log.warning("Espera interrumpida (%s: %s); sigo.", type(e).__name__, e)
        time.sleep(1)


def bucle(ex, ib, nombre: str = "ejecutor"):
    """El ciclo de siempre: un paso, esperar, otro paso. Solo sale con Ctrl+C. Ningún error de un paso, ni un corte de
    la conexión con IBKR, lo detiene."""
    while True:
        try:
            espera = ex.paso()
        except (ConnectionError, asyncio.CancelledError) as e:   # corte con IBKR a mitad de un paso: se reconecta solo
            log.warning("Se cortó la conexión con IBKR en mitad de un paso (%s); reintento en 3 s.", type(e).__name__)
            espera = 3
        except Exception:  # noqa: BLE001 (nunca dejarlo caer en plena sesión)
            log.exception("Error inesperado en el %s; sigo en 5 s.", nombre)
            espera = 5
        esperar(ib, espera)   # mientras espera, procesa lo que llega de IBKR (aquí sí se puede esperar)


def cerrar(ex, ib):
    """Al salir con Ctrl+C: cancela las compras sin llenar, avisa al semáforo y desconecta. Un fallo aquí no impide
    desconectar."""
    try:
        ex.salir()
        if ib.isConnected():
            esperar(ib, 1)   # que salgan las cancelaciones antes de desconectar
            ex.sync(ex.reloj())
    except (Exception, asyncio.CancelledError):  # noqa: BLE001
        log.exception("Error al cerrar el ejecutor.")
    finally:
        if ib.isConnected():
            ib.disconnect()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Ejecutor de órdenes en la cuenta PAPER de IBKR (dinero simulado)")
    ap.parse_args(argv)
    configurar_log(LOG_FILE)
    cfg = read_env()
    if not cfg["BRIDGE_TOKEN"]:
        print("Falta la clave del semáforo (BRIDGE_TOKEN en puente.env). Es la misma del puente IBKR.")
        return 2   # 2 = no tiene sentido reintentar: ejecutor.bat no lo reabre
    if IB is None:
        print("Falta la librería de IBKR. Instálala con:  py -m pip install -r requirements.txt")
        return 2
    no_quickedit()
    log.info("Ejecutor PAPER %s -> %s. Candado: solo IB Gateway paper (puerto %d, cuentas DU). Ctrl+C para salir.",
             VERSION, cfg["SEMAFORO_URL"], PUERTO)
    ib = IB()
    ex = Ejecutor(cfg, ib)
    try:
        bucle(ex, ib, "ejecutor")
    except KeyboardInterrupt:
        pass
    finally:
        cerrar(ex, ib)
    log.info("Ejecutor detenido.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
