"""Órdenes a mano por Telegram, solo en IBKR PAPER (dinero simulado).

    XYZ 10.00 stop 9.50 tp 12.00 riesgo 50   →  plan: acciones = floor(riesgo / (entrada − stop)), con su resumen
    OK                                         →  la envía al ejecutor de tu PC (compra límite + stop + objetivo, bracket de IBKR)
    cualquier otro mensaje                     →  cancela el plan pendiente
    posiciones · ordenes · cancelar [ID|ticker] · estado

Nada se envía sin tu «OK» (vale PLAN_TTL s). Esto solo decide el texto y la validación; la orden la pone el ejecutor de la PC
(bridge/ejecutor_paper.py), que además tiene sus propios topes fijos y no puede conectarse a una cuenta real. Aquí no existe
ninguna ruta a la cuenta real: cada respuesta dice PAPER.

Topes por variable de entorno (Render): MAX_NOTIONAL (US$ por orden) y MAX_RISK (US$ que se pierden si salta el stop). Solo pueden
bajar los topes del estilo de paper vigente (los del ejecutor y los del semáforo mandan siempre: se aplica el más estricto).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from decimal import Decimal, InvalidOperation

from .paper import ENTRADA_FIN_M, ID, _num, hhmm, topes as topes_paper

log = logging.getLogger("radar")

MODO = "PAPER"
TAG = f"[{MODO}]"
PLAN_TTL = 120          # s que vale un plan sin confirmar
NUM = r"(\d+(?:\.\d+)?)"
ORDEN_RX = re.compile(rf"^\$?([A-Za-z]{{1,5}})\s+{NUM}\s+stop\s+{NUM}\s+tp\s+{NUM}\s+riesgo\s+\$?{NUM}$", re.I)
PARECE_ORDEN = re.compile(r"^\$?[A-Za-z0-9.]{1,8}\s+\$?\d", re.I)       # empieza como una orden: si no encaja, se explica el formato
EJEMPLO = "XYZ 10.00 stop 9.50 tp 12.00 riesgo 50"
FORMATO = (f"No entendí la orden. Formato: ticker entrada stop X tp Y riesgo Z, por ejemplo:\n{EJEMPLO}\n"
           "(solo compras; entrada = precio límite; riesgo = lo máximo que aceptas perder en US$; sin comas ni signos).")
CMDS = ("posiciones", "ordenes", "órdenes", "cancelar")


def usd(x: float | None) -> str:
    return "n/d" if x is None else f"US${x:,.2f}"


def _dec(s: str) -> Decimal:
    return Decimal(s)


class OrdenesTg:
    def __init__(self, paper, clock=time.time, env=None):
        self.paper = paper
        self.clock = clock
        self.env = os.environ if env is None else env
        self.lock = threading.RLock()
        self.pend: dict | None = None      # un solo plan o cancelación a la espera del OK

    # ---------------- topes ----------------
    def topes(self) -> dict:
        """MAX_NOTIONAL y MAX_RISK de Render, nunca por encima de los del estilo de paper vigente."""
        base = topes_paper()

        def v(k, d):
            x = _num(self.env.get(k))
            return min(x, d) if x and x > 0 else d
        return {"orden_usd": v("MAX_NOTIONAL", base["orden_usd"]), "riesgo_usd": v("MAX_RISK", base["riesgo_usd"])}

    # ---------------- plan ----------------
    def planear(self, m: re.Match, now: float) -> tuple[dict | None, str]:
        """Texto → plan validado, o el motivo por el que no se puede (nada se manda sin OK)."""
        t = m.group(1).upper()
        nums = [m.group(i) for i in range(2, 6)]
        try:
            ent, stp, tp, rsk = (_dec(x) for x in nums)
        except InvalidOperation:
            return None, FORMATO
        if min(ent, stp, tp, rsk) <= 0:
            return None, "Todos los números deben ser mayores que 0."
        dec = 2 if ent >= 1 else 4
        for nombre, x in (("entrada", ent), ("stop", stp), ("tp", tp)):
            if -x.as_tuple().exponent > dec:
                return None, (f"El {nombre} tiene demasiados decimales: IBKR cotiza con {dec} decimales "
                              f"a este precio ({x} → {round(x, dec)}).")
        if stp >= ent:
            return None, f"El stop ({stp}) debe estar por debajo de la entrada ({ent}): el bot solo compra (posición larga)."
        if tp <= ent:
            return None, f"El objetivo tp ({tp}) debe estar por encima de la entrada ({ent})."
        dist = ent - stp
        qty = int(rsk // dist)                              # floor exacto (Decimal)
        if qty < 1:
            return None, (f"Con riesgo US${rsk} y un stop a US${dist} de la entrada no alcanza ni para 1 acción "
                          f"(necesitas al menos US${dist} de riesgo).")
        entrada, stop, obj = float(ent), float(stp), float(tp)
        costo, riesgo_real = qty * entrada, float(qty * dist)
        T = self.topes()
        if costo > T["orden_usd"] + 0.01:
            maxq = int(Decimal(str(T["orden_usd"])) // ent)
            return None, (f"Pasa del máximo por orden: {qty} acc × {entrada:g} = {usd(costo)} (MAX_NOTIONAL {usd(T['orden_usd'])}; "
                          f"cabrían {maxq} acc). Baja el riesgo o ajusta el stop.")
        if riesgo_real > T["riesgo_usd"] + 0.01:
            return None, (f"Pasa del riesgo máximo por orden: perderías {usd(riesgo_real)} si salta el stop "
                          f"(MAX_RISK {usd(T['riesgo_usd'])}).")
        motivo = self.paper.validar_manual(t, entrada, stop, obj, qty, now)
        if motivo:
            return None, f"No la puedo enviar ahora: {motivo}."
        return {"tipo": "orden", "t": t, "entrada": entrada, "stop": stop, "tp": obj, "qty": qty, "costo": costo,
                "riesgo": riesgo_real, "rr": float((tp - ent) / dist), "ts": now, "T": T}, ""

    def resumen(self, p: dict) -> str:
        return (f"📝 Plan {TAG} (dinero simulado)\n"
                f"{p['t']} · comprar {p['qty']} acc\n"
                f"Entrada límite {p['entrada']:g} · Stop {p['stop']:g} · Objetivo {p['tp']:g}\n"
                f"Riesgo real {usd(p['riesgo'])} (tope {usd(p['T']['riesgo_usd'])}) · Valor {usd(p['costo'])} "
                f"(tope {usd(p['T']['orden_usd'])})\n"
                f"Premio/riesgo {p['rr']:.1f} : 1 · vence al cerrar la ventana de compras ({hhmm(ENTRADA_FIN_M)} ET)\n"
                f"Responde OK para enviarla a IBKR paper. Cualquier otro mensaje la cancela (vale {PLAN_TTL // 60} min).")

    # ---------------- entrada ----------------
    def manejar(self, text: str, now: float | None = None) -> tuple[list[str], bool]:
        """(respuestas, atendido). atendido=False: el mensaje no es nuestro y el bot sigue con sus comandos de siempre
        (/estado, /cerrar…); las respuestas (p. ej. «plan cancelado») se mandan igual."""
        now = now or self.clock()
        txt = (text or "").strip()
        out: list[str] = []
        with self.lock:
            pend = self.pend
            if pend and now - pend["ts"] > PLAN_TTL:
                self.pend, pend = None, None
                if txt.lower().rstrip(".! ") == "ok":
                    return [f"{TAG} Ese plan venció (más de {PLAN_TTL // 60} min). Envíalo otra vez."], True
            if txt.lower().rstrip("!. ") == "ok":
                if not pend:
                    return [f"{TAG} No hay nada pendiente que confirmar."], True
                self.pend = None
                return [self._ejecutar(pend, now)], True
            if pend:                                          # cualquier otro mensaje cancela lo pendiente
                self.pend = None
                log.info("ORDEN-TG cancelado el plan pendiente (%s) por otro mensaje", pend.get("t") or pend.get("ids"))
                out.append(f"{TAG} Plan cancelado: no envié nada." if pend["tipo"] == "orden"
                           else f"{TAG} Cancelación descartada: no cambié nada.")
            m = ORDEN_RX.match(txt)
            if m:
                plan, err = self.planear(m, now)
                if plan:
                    self.pend = plan
                    log.info("ORDEN-TG plan %s %s acc entrada %s stop %s tp %s riesgo %.2f (esperando OK)",
                             plan["t"], plan["qty"], plan["entrada"], plan["stop"], plan["tp"], plan["riesgo"])
                    out.append(self.resumen(plan))
                else:
                    out.append(f"✋ {TAG} {err}")
                return out, True
            palabra = txt.split()[0].lower().lstrip("/").split("@")[0] if txt else ""
            arg = " ".join(txt.split()[1:])
            if palabra == "posiciones":
                return out + [self.posiciones()], True
            if palabra in ("ordenes", "órdenes"):
                return out + [self.ordenes()], True
            if palabra == "cancelar":
                return out + [self.cancelar(arg, now)], True
            if PARECE_ORDEN.match(txt) and not txt.startswith("/"):
                return out + [f"✋ {TAG} {FORMATO}"], True
            return out, False

    # ---------------- OK ----------------
    def _ejecutar(self, p: dict, now: float) -> str:
        if p["tipo"] == "orden":
            oid, txt = self.paper.enviar_manual(p["t"], p["entrada"], p["stop"], p["tp"], p["qty"], now)
            log.info("ORDEN-TG %s %s acc entrada %s stop %s tp %s → %s", p["t"], p["qty"], p["entrada"], p["stop"], p["tp"],
                     f"enviada id {oid}" if oid else f"rechazada ({txt})")
            if not oid:
                return f"✋ {TAG} {txt}"
            return (f"✅ {TAG} {txt}\nCompra límite {p['entrada']:g} + stop {p['stop']:g} + objetivo {p['tp']:g} "
                    f"(riesgo {usd(p['riesgo'])}). Te aviso cuando se ponga, se llene, se cancele o IBKR la rechace. "
                    f"«ordenes» la muestra; «cancelar {p['t']}» la cancela mientras no se llene.")
        # cancelación
        n = self.paper.cancelar_ids(p["ids"], now)
        log.info("ORDEN-TG cancelación confirmada %s → %s pedidas", p["ids"], n)
        if not n:
            return f"{TAG} No había nada que cancelar (ya se llenó, venció o se canceló). «ordenes» muestra cómo están."
        return (f"🧹 {TAG} Pedí cancelar {n} orden(es) sin llenar. El ejecutor de tu PC confirma en unos segundos "
                f"(te aviso con «⌛ Paper: cancelé la compra…»).")

    # ---------------- comandos ----------------
    def _estado_ejecutor(self, now: float) -> str:
        if self.paper.disponible(now):
            return ""
        return "\n⚠ El ejecutor de tu PC no está conectado ahora: lo que ves puede estar desactualizado."

    def posiciones(self, now: float | None = None) -> str:
        now = now or self.clock()
        with self.paper.lock:
            pos = list(self.paper.ex.get("posiciones") or [])
            merc = dict(self.paper.ex.get("mercado") or {})
        if not pos:
            return f"📊 Posiciones {TAG}: ninguna abierta." + self._estado_ejecutor(now)
        lineas, total, hay = [f"📊 Posiciones {TAG}"], 0.0, False
        for x in pos:
            q, px = x.get("qty") or 0, x.get("px")
            mp = merc.get(x["t"])
            if mp and px:
                pnl = q * (mp - px)
                total, hay = total + pnl, True
                lineas.append(f"{x['t']} · {q:g} acc · promedio {px:.2f} · precio {mp:.2f} · P&L no realizado {pnl:+.2f} US$")
            else:
                lineas.append(f"{x['t']} · {q:g} acc · promedio {px or 0:.2f} · P&L no realizado n/d (IBKR no dio precio)")
        if hay and len(pos) > 1:
            lineas.append(f"Total no realizado (las que tienen precio): {total:+.2f} US$")
        lineas.append("El precio es el que entrega IB Gateway paper (puede venir con retraso).")
        return "\n".join(lineas) + self._estado_ejecutor(now)

    @staticmethod
    def _linea(o: dict) -> str:
        sal = (f"stop {o['stop']:.2f} · objetivo {o['objetivo']:.2f}" if o.get("stop") and o.get("objetivo") else
               f"Trailing {o['trail_pct']:g} % (sin objetivo)" if o.get("trail_pct") else
               f"Trailing {o['trail']:.2f} · objetivo {o['objetivo']:.2f}" if o.get("trail") and o.get("objetivo") else "")
        est = {"cola": "en cola (esperando al ejecutor)", "enviada": "enviada al ejecutor", "puesta": "puesta en IBKR, sin llenar",
               "cancelando": "cancelándose", "llena": "comprada, protegida por su stop"}.get(o["estado"], o["estado"])
        return f"{o['id']} · {o['t']} · {o['qty'] or 0:g} acc · compra {o['limite'] or 0:.2f} · {sal} · {est}"

    def ordenes(self, now: float | None = None) -> str:
        now = now or self.clock()
        lst = self.paper.ordenes_listado()
        if not lst:
            return f"📋 Órdenes abiertas {TAG}: ninguna." + self._estado_ejecutor(now)
        return f"📋 Órdenes abiertas {TAG}\n" + "\n".join(self._linea(o) for o in lst) + self._estado_ejecutor(now)

    def cancelar(self, arg: str, now: float) -> str:
        lst = self.paper.ordenes_listado()
        sin_llenar = [o for o in lst if o["estado"] in ("cola", "enviada", "puesta")]
        a = arg.strip().lower()
        if not a:
            if not sin_llenar:
                return f"{TAG} No hay compras sin llenar que cancelar. Uso: cancelar ID  o  cancelar TICKER."
            return (f"{TAG} ¿Cuál cancelo? Escribe cancelar ID o cancelar TICKER:\n"
                    + "\n".join(self._linea(o) for o in sin_llenar))
        if not (ID.fullmatch(a) or re.fullmatch(r"[a-z]{1,5}", a)):
            return f"✋ {TAG} Uso: cancelar ID (8 caracteres)  o  cancelar TICKER (1 a 5 letras)."
        hit = [o for o in lst if o["id"] == a or o["t"].lower() == a]
        if not hit:
            return f"{TAG} No encontré ninguna orden abierta con «{arg.strip()}». «ordenes» las lista."
        cancelables = [o for o in hit if o["estado"] in ("cola", "enviada", "puesta")]
        if not cancelables:
            return (f"✋ {TAG} {hit[0]['t']} ya está comprada: su stop y su objetivo la protegen y cancelarlos la dejaría sin "
                    f"protección. Para salir de todo usa /cerrar (vende a mercado lo comprado).")
        self.pend = {"tipo": "cancelar", "ids": [o["id"] for o in cancelables], "ts": now}
        return (f"🧹 {TAG} Cancelaría (compras sin llenar; no vende nada):\n" + "\n".join(self._linea(o) for o in cancelables)
                + f"\nResponde OK para cancelar. Cualquier otro mensaje lo deja como está (vale {PLAN_TTL // 60} min).")
