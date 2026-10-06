"""Ejecutor de Claude (cuenta PAPER de IBKR): corre en la PC de Priamo, en paralelo a su ejecutor (ejecutor_paper.py), en
la MISMA cuenta paper, con una estrategia distinta y órdenes marcadas "cla-<id>-<rol>" para separar los resultados: las
suyas son "sem-…". Mismo motor, mismo candado y mismas protecciones que el de Priamo (se importan de ejecutor_paper.py):

  - solo IB Gateway paper (puerto 4002) y solo cuentas DU…; no hay nada que lo apunte a la cuenta real;
  - US$500 por operación · US$1,000 comprometidos · pérdida máxima del día US$100 (se detiene al llegar) · 20 órdenes
    por día · cierra todo a las 15:55 ET · una orden o posición por acción;
  - lo que cuenta es lo que IBKR ejecutó; una posición sin su stop vivo se vende; si vendiera de más, recompra.
Lo que cambia es el cerebro. En vez de esperar órdenes del semáforo, lee velas de 1 min de Yahoo (datos_yahoo.py, hilo
aparte) y aplica estrategia_claude.py: «pullback con tendencia» (compra LÍMITE en la pausa de un líder del día, stop fijo
bajo el mínimo del retroceso, objetivo 2R; ver ese archivo). Compras de 9:50 a 15:15 ET. Desde la 1.4 con perfil volátil
(stop según el ATR, retrocesos más hondos, líderes más extendidos) pero con el mismo dinero: tamaño por riesgo (US$6).

Nunca toca órdenes ni posiciones que no haya puesto él, y antes de comprar una acción confirma que ningún otro ejecutor
(ni tú a mano en TWS) tenga órdenes o posición en ella, para no cruzar órdenes en la misma cuenta.

Le avisa al semáforo (Render, POST /api/claude/sync) lo que pasa y recibe de vuelta /pausa y /cerrar de Telegram. Si pierde
contacto con el semáforo no pone órdenes nuevas (sin el botón de parada no opera). Diario: diario_cla.csv; registro:
ejecutor_claude.log.

Uso (Windows): ARRANCAR_PAPER.bat lo abre junto con el ejecutor de Priamo. Lista de acciones: la de LISTA o, si existe,
lista_claude.txt (una por línea). Ctrl+C para salir.
"""
from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ejecutor_paper as E  # noqa: E402
import estrategia_claude as S  # noqa: E402
from datos_yahoo import Feed  # noqa: E402
from puente_ibkr import http_post, no_quickedit, read_env  # noqa: E402

VERSION = "1.4"
LOG_FILE = os.path.join(HERE, "ejecutor_claude.log")
STATE_FILE = os.path.join(HERE, "ejecutor_claude.json")
LISTA_FILE = os.path.join(HERE, "lista_claude.txt")
MAX_PENDIENTES = 2        # compras puestas sin llenar a la vez
MAX_POR_ACCION = 2        # entradas por acción y día
SYNC_MAX_S = 180          # sin contacto con el semáforo (botón de parada) no se ponen órdenes nuevas
LISTA = ("NVDA AMD AVGO MU MRVL ARM SMCI TSM INTC QCOM AMAT LRCX ON WDC STX SNDK ANET PLTR TSLA AAPL MSFT AMZN META "
         "GOOGL NFLX COIN MSTR HOOD UBER SHOP CRWD APP RDDT SOFI AFRM SOXL TQQQ TECL BE AXTI SNXX SNDQ MUU MVLL AXTX "
         "BEG").split()

log = logging.getLogger("ejecutor")


def cargar_lista(path: str = LISTA_FILE) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            ts = [x.split("#")[0].strip().upper() for x in f]
        ts = [x for x in ts if E.SYM.fullmatch(x)]
        if ts:
            return list(dict.fromkeys(ts))
    except OSError:
        pass
    return list(LISTA)


LOCK_PORT = 45042            # una sola copia de este ejecutor (el del semáforo usa 45041)


class EjecutorClaude(E.Ejecutor):
    PREFIJO = "cla"
    CLIENT_ID = 42                      # el de Priamo usa 41, el puente de datos 17, las pruebas 31 y 32
    RUTA = "/api/claude/sync"
    NOMBRE = "claude"
    DIARIO = "diario_cla.csv"
    ENTRADA_INI_M = S.CFG.ini_m         # 9:50
    ENTRADA_FIN_M = S.CFG.fin_m         # 15:15 (los límites de dinero y el cierre de las 15:55 son los del motor)

    def __init__(self, cfg: dict, ib, feed, post=http_post, reloj=time.time, estado_path: str = STATE_FILE,
                 diario_path: str | None = None, scfg: S.Cfg = S.CFG):
        super().__init__(cfg, ib, post=post, reloj=reloj, estado_path=estado_path,
                         diario_path=diario_path or (os.path.join(HERE, self.DIARIO) if estado_path == STATE_FILE else None))
        self.feed, self.scfg = feed, scfg
        self.ult_sync_ok = 0.0
        self.ult_eval = 0.0
        self.cand: list[dict] = []
        self.ult_resumen = 0.0
        self.ult_sin_datos = 0.0
        self.vetadas: dict[str, float] = {}          # acciones que IBKR rechazó: no se reintentan por un rato
        self.omitidas: dict[tuple[str, str], float] = {}   # (acción, motivo) -> última vez que se anotó en el registro

    # ---------------- contacto con el semáforo ----------------
    def sync(self, now: float, res: dict | None = None):
        out = super().sync(now, res)
        if out is not None:
            self.ult_sync_ok = now
        return out

    def extra_estado(self) -> dict:
        now = self.reloj()
        return {**self.feed.estado(now), "estrategia": "pullback con tendencia",
                "candidatas": [f"{j['t']} {j['texto']}" for j in self.cand[:3]]}

    # ---------------- no tocar lo de otros ----------------
    def ajeno(self, t: str) -> str | None:
        """¿Otro ejecutor (o tú a mano en TWS) tiene órdenes o una posición en esta acción? Si no se puede comprobar, se
        asume que sí (mejor no poner la orden que cruzarla con otra)."""
        try:
            for tr in self.ib.reqAllOpenOrders():
                if tr.contract.symbol == t and not (tr.order.orderRef or "").startswith(self.PREFIJO + "-"):
                    return f"otro ejecutor (o tú a mano) tiene una orden en {t}"
            propia = sum(self.abierta(o) for o in self.ord.values() if o["t"] == t)
            neto = sum(float(p.position) for p in self.ib.positions() if p.contract.symbol == t)
            if abs(neto - propia) > 1e-6:
                return f"hay una posición que no es mía en {t}"
        except Exception as e:  # noqa: BLE001
            return f"no pude revisar las órdenes de otros ({type(e).__name__})"
        return None

    def motivo(self, o: dict, now: float) -> str | None:
        return super().motivo(o, now) or self.ajeno(o["t"])

    # ---------------- el cerebro ----------------
    def paso(self) -> float:
        espera = super().paso()
        try:
            self.cerebro(self.reloj())
        except Exception:  # noqa: BLE001 (la estrategia nunca debe tumbar al ejecutor)
            log.exception("Error en la estrategia de Claude; sigo.")
        return espera

    def datos_hoy(self, now: float) -> tuple[dict, dict]:
        hoy = E.et(now).date().isoformat()
        datos, spy = self.feed.snapshot(hoy)
        buenos = {}
        # si Yahoo limitó las peticiones y el lector va más despacio, la última vela es más vieja a propósito
        vieja = self.scfg.vela_vieja_s + max(0, getattr(self.feed, "intervalo", 60) - 60)
        for t, d in datos.items():
            b = d.get("bars") or []
            if d.get("dia") == hoy and b and now - E.et_ts(now, b[-1][0] + 1) <= vieja:
                buenos[t] = d
        if spy.get("bars") and spy.get("dia") != hoy:
            spy = {}
        return buenos, spy

    def cerebro(self, now: float):
        t = E.et(now)
        if not self.paper or t.weekday() >= 5:
            return
        if not (self.pausa or self.parado or self.cerrado_hoy):
            self.vigilar_pendientes(now)
        if self.pausa or self.parado or self.cerrado_hoy or now - self.ult_sync_ok > SYNC_MAX_S:
            return
        ini, fin, _ = self.horario()
        m = t.hour * 60 + t.minute
        if not ini <= m < fin:
            return
        if not self.feed.ok(now):
            self.sin_datos_log(now)
            return
        if self.feed.ult_barrido == self.ult_eval:
            return
        self.ult_eval = self.feed.ult_barrido        # una evaluación por cada lectura nueva de Yahoo (cada minuto)
        buenos, spy = self.datos_hoy(now)
        reg = S.regimen(spy.get("bars"), spy.get("prev"), self.scfg)
        jugadas, motivos = S.buscar(buenos, reg, self.scfg)
        self.cand = jugadas
        self.resumen_log(now, buenos, motivos, reg, jugadas)
        pend = sum(1 for o in self.ord.values() if o["estado"] in ("nueva", "puesta") and not self.cant(o["id"], "e")[0])
        for j in jugadas:
            if pend >= MAX_PENDIENTES:
                break
            if self.vetadas.get(j["t"], 0) > now:
                continue
            if sum(1 for o in self.ord.values() if o["t"] == j["t"] and o.get("oids", {}).get("e")) >= MAX_POR_ACCION:
                continue
            raw = {"id": secrets.token_hex(4), "t": j["t"], "tipo": "lmt", "gatillo": None, "limite": j["limite"],
                   "trail": j["trail"], "stop": j["stop"], "objetivo": j["objetivo"], "qty": j["qty"],
                   "hasta": now + self.scfg.ttl_s, "setup": j["texto"]}
            # Antes de crear la orden se miran los límites: si algo la frena (otro ejecutor en esa acción, tope de dinero…)
            # se omite sin ruido; solo lo que IBKR rechaza de verdad queda como orden rechazada y llega a Telegram
            previa = {**raw, "hasta": min(raw["hasta"], E.et_ts(now, fin)), "oids": {}, "estado": "nueva"}
            m_previo = self.motivo(previa, now)
            if m_previo:
                self.omitir(j["t"], m_previo, now)
                continue
            self.poner(raw, now)
            o = self.ord.get(raw["id"])
            if o and o["estado"] == "puesta":
                pend += 1
            elif o and o["estado"] == "rechazada":
                self.vetadas[j["t"]] = now + 1800

    def sin_datos_log(self, now: float):
        """Yahoo no responde o respondió a medias: lo deja en el registro cada 5 min (si no, el día entero pasa en silencio)."""
        if now - self.ult_sin_datos < 300:
            return
        self.ult_sin_datos = now
        est = self.feed.estado(now)
        log.warning("Sin datos de Yahoo suficientes (%s de %s acciones, última lectura hace %s s, leyendo cada %s s): no pongo "
                    "órdenes nuevas. %s", est["feed_n"], est["feed_de"], est["feed_edad_s"], est.get("feed_intervalo_s", 60),
                    est["feed_error"] or "")

    def omitir(self, t: str, motivo: str, now: float):
        k = (t, motivo)
        if now - self.omitidas.get(k, 0) >= 600:
            self.omitidas[k] = now
            log.info("No pongo %s: %s", t, motivo)

    def vigilar_pendientes(self, now: float):
        """Una compra puesta que todavía no se llenó se cancela si la jugada se rompió: el precio cerró bajo su stop
        o el mercado entró en pánico. Lo demás lo hace el vencimiento (GTD) de la propia orden."""
        buenos, spy = self.datos_hoy(now)
        panico = bool(spy.get("bars")) and not S.regimen(spy["bars"], spy.get("prev"), self.scfg)[0] and bool(spy.get("prev"))
        for o in list(self.ord.values()):
            if o["estado"] != "puesta" or self.cant(o["id"], "e")[0] > 0 or not o.get("stop"):
                continue
            d = buenos.get(o["t"])
            if panico:
                self.cancelar(o["id"], "el mercado cayó más de 0.5 %: cancelo la compra")
            elif d and d["bars"] and d["bars"][-1][4] < o["stop"]:
                self.cancelar(o["id"], "el precio perdió el stop sin llenarse: la jugada se rompió")

    def resumen_log(self, now: float, buenos: dict, motivos: dict, reg: tuple, jugadas: list):
        """Cada 5 min deja en el registro por qué no hay compras: SPY y los tres que más suben con su motivo."""
        if jugadas or now - self.ult_resumen < 300:
            return
        self.ult_resumen = now
        lid = sorted(((d["bars"][-1][4] / d["prev"] - 1, t) for t, d in buenos.items() if d.get("prev")), reverse=True)[:3]
        log.info("Sin jugadas · %s · %d acciones con datos · líderes: %s", reg[1], len(buenos),
                 "; ".join(f"{t} {c * 100:+.1f}% ({motivos.get(t, '?')})" for c, t in lid) or "ninguno")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Ejecutor de Claude en la cuenta PAPER de IBKR (dinero simulado)")
    ap.parse_args(argv)
    E.configurar_log(LOG_FILE)
    cfg = read_env()
    if not cfg["BRIDGE_TOKEN"]:
        print("Falta la clave del semáforo (BRIDGE_TOKEN en puente.env). Es la misma del puente IBKR.")
        return 2   # 2 = no tiene sentido reintentar: ejecutor.bat no lo reabre
    if E.IB is None:
        print("Falta la librería de IBKR. Instálala con:  py -m pip install -r requirements.txt")
        return 2
    cerrojo, hay_otro = E.tomar_cerrojo(LOCK_PORT, "claude")
    if hay_otro:
        return E.ya_hay_otro(LOCK_PORT, "ejecutor de Claude")
    if cerrojo is None:
        log.warning("No pude reservar el puerto local %d (una sola copia a la vez); sigo sin ese seguro.", LOCK_PORT)
    no_quickedit()
    lista = cargar_lista()
    log.info("Ejecutor de CLAUDE %s (motor %s) -> %s. Candado: solo IB Gateway paper (puerto %d, cuentas DU). %d acciones. "
             "Ctrl+C para salir.", VERSION, E.VERSION, cfg["SEMAFORO_URL"], E.PUERTO, len(lista))
    feed = Feed(lista)
    feed.start()
    ib = E.IB()
    ex = EjecutorClaude(cfg, ib, feed)
    try:
        E.bucle(ex, ib, "ejecutor de Claude")   # un corte de IB Gateway (reinicio de las 23:30) no lo detiene: reconecta
    except KeyboardInterrupt:
        pass
    finally:
        E.cerrar(ex, ib)
        if cerrojo:
            cerrojo.close()
    log.info("Ejecutor de Claude detenido.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
