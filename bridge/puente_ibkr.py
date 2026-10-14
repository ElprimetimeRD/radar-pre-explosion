"""Puente IBKR -> semáforo. Corre en la PC de Priamo, conectado a TWS o IB Gateway en modo de SOLO LECTURA:

- cada 30 s le pasa al semáforo lo que ven los escáneres de IBKR (los que más suben, volumen inusual y ritmo de
  operaciones), que detectan una acción que arranca antes que las listas de Yahoo;
- se suscribe al precio al instante de las acciones armadas (las que el semáforo le dice) y, si una cruza su
  gatillo, se lo manda en el acto para que llegue el aviso ⚡ por Telegram;
- (1.2) sigue además las acciones que el semáforo le pide (radar de flujo, Biotech, la lista de seguimiento): recibe
  cada operación (RT Volume), la clasifica como compra o venta contra el bid/ask de ese momento (Lee-Ready: en el ask o
  arriba del medio = compra; en el bid o abajo = venta; en el medio, según si subió o bajó) y le manda al semáforo, por
  minuto, el dinero comprador y vendedor, las órdenes grandes (≥ LARGE_USD por operación) y el bid/ask. Es el flujo
  REAL que el semáforo antes estimaba con las velas de Yahoo.

No manda órdenes: se conecta como solo lectura y no tiene código para operar.

Uso (Windows):
    py puente_ibkr.py --crear-clave   crea puente.env con una clave nueva para BRIDGE_TOKEN (cópiala en Render)
    py puente_ibkr.py                 arranca el puente (o doble clic en iniciar_puente.bat)
Deja un registro en puente.log (junto a este archivo).
"""
from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import logging
import logging.handlers
import math
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request

try:
    from ib_async import IB, ScannerSubscription, Stock
    from ib_async.ib import StartupFetch
except ImportError:  # las pruebas corren sin IBKR; main() avisa cómo instalarlo
    IB = ScannerSubscription = Stock = StartupFetch = None

VERSION = "1.2"
HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, "puente.env")
LOG_FILE = os.path.join(HERE, "puente.log")
DEFAULTS = {
    "SEMAFORO_URL": "https://radar-semaforo.onrender.com",
    "BRIDGE_TOKEN": "",
    "IB_HOST": "127.0.0.1",
    "IB_PORT": "4001",           # IB Gateway, cuenta real: 4001 · TWS, cuenta real: 7496
    "IB_CLIENT_ID": "17",
    "SCANS": "TOP_PERC_GAIN,HOT_BY_VOLUME,TOP_TRADE_RATE",
    "MIN_PRICE": "1",
    "MIN_VOLUME": "100000",
    "SCAN_EVERY": "30",          # s entre escaneos (pre-market y sesión)
    "SCAN_TIMEOUT": "20",        # s máximos de espera por cada escáner
    "MAX_LINES": "60",           # tope de acciones seguidas a la vez (IBKR da 100 líneas por usuario)
    "FLOW": "1",                 # 0 = solo precios de las armadas, sin flujo de operaciones
    "LARGE_USD": "25000",        # una operación de este monto o más cuenta como orden grande
    "SIZE_UNIT": "auto",         # acciones por unidad de tamaño de IBKR: auto (lo confirma el semáforo), 1 o 100
}
# Errores y avisos de IBKR: (texto corto para el semáforo, qué hacer)
HINTS = {
    354: ("sin datos en vivo (354)", "IBKR dice que no tienes datos en vivo por la API. En Client Portal > Settings > "
          "Market Data Subscriptions revisa que el 'Market Data API Acknowledgement' esté firmado y los dos paquetes "
          "activos; luego cierra sesión en todas las aplicaciones de IBKR y vuelve a entrar."),
    10089: ("falta suscripción para la API (10089)", "Esa acción necesita una suscripción de datos para la API: revisa "
            "los dos paquetes (Snapshot y Add-On Streaming) y el API Acknowledgement."),
    10090: ("datos incompletos (10090)", "Parte de los datos no está suscrita; el precio puede llegar incompleto."),
    10167: ("datos con retraso (10167)", "IBKR está mandando datos con retraso: falta la suscripción en vivo."),
    10168: ("sin suscripción (10168)", "No hay datos en vivo para la API: revisa suscripciones y API Acknowledgement."),
    10197: ("sesión en otra plataforma (10197)", "Sin datos: tu usuario está abierto en otra plataforma (celular, web o "
            "TWS) y esa sesión se quedó con los datos. Ciérrala o usa un segundo usuario para el puente."),
    326: ("número de cliente en uso (326)", "Otro programa usa el mismo IB_CLIENT_ID: cambia IB_CLIENT_ID en puente.env."),
    101: ("tope de líneas de datos (101)", "IBKR no da más líneas de datos a la vez: el puente sigue menos acciones. Si "
          "tienes IBKR abierto en otra plataforma con muchas cotizaciones, esas también cuentan."),
    1100: ("IBKR sin conexión (1100)", "IBKR perdió la conexión con sus servidores; el puente espera a que vuelva."),
    2110: ("IBKR sin conexión (2110)", "TWS/IB Gateway perdió la conexión con los servidores de IBKR."),
    2103: ("precios de IBKR caídos (2103)", "TWS/IB Gateway perdió el servidor de precios de IBKR; suele volver solo."),
    2105: ("escáneres de IBKR caídos (2105)", "TWS/IB Gateway perdió el servidor de datos históricos; suele volver solo."),
}
CLEARS = {1101: (1100, 2110), 1102: (1100, 2110), 2104: (2103,), 2106: (2105,)}  # aviso de OK -> errores que limpia
DATA_ERRORS = {354, 10089, 10090, 10167, 10168, 10197, 2103}  # se limpian solos cuando vuelven a llegar precios

log = logging.getLogger("puente")

TRADE_TICKS = (48, 77)       # RT Volume / RT Trade Volume: cada operación con su precio y tamaño
BID_TICKS, ASK_TICKS = (1, 66), (2, 67)
GENERIC = "233"              # pide RT Volume (las operaciones una por una, como el Time & Sales)
FLOW_KEEP_MIN = 1200         # minutos de flujo que se guardan (20 h: pre-market, sesión y after-hours de hoy)
FLOW_MAX_SEND = 800          # registros de flujo por envío (uno atrasado no pasa el tope de tamaño del semáforo)
PUSH_S = 3                   # s mínimos entre envíos por precios o flujo nuevos (un cruce o una armada salen ya)
QUOTE_RESEND_S = 8           # s: el precio de una acción quieta se vuelve a mandar para que el semáforo lo vea fresco
CONTRACT_BAD_S = 600         # s sin volver a pedir una acción que IBKR rechazó (sin suscripción para su bolsa)
PER_CONTRACT = (354, 10089, 10168)  # errores de datos que llegan con el contrato: solo esa acción queda fuera un rato
# Columnas de cada registro de flujo (un minuto de una acción). El semáforo (live/bridge.py) las lee en este orden.
#   m: minuto (segundos de época ÷ 60) · o h l c: precios de las operaciones · v: tamaño sumado (en unidades de IBKR)
#   n: operaciones · buy sell mid: precio × tamaño de las compras, ventas y las que no se pudieron clasificar
#   bbuy bsell nbb nbs: lo mismo y cuántas, solo de las órdenes grandes · top tside: la operación más grande y su lado
#   u: acciones por unidad de tamaño con que se decidió qué es "grande" en ese minuto
FLOW_COLS = ("m", "o", "h", "l", "c", "v", "n", "buy", "sell", "mid", "bbuy", "bsell", "nbb", "nbs", "top", "tside", "u")
SYM_RX = re.compile(r"[A-Z]{1,5}(\.[A-Z])?")


def side_of(px: float, bid, ask, prev_px, prev_side: int) -> int:
    """Lado de una operación (Lee-Ready): +1 compra, −1 venta, 0 sin saber. Contra el bid/ask de ese momento: en el
    ask o arriba del medio, compra; en el bid o abajo del medio, venta. Justo en el medio (o sin bid/ask válido), según
    si el precio subió o bajó respecto de la operación anterior; si tampoco cambió, el lado de la anterior."""
    if bid and ask and ask >= bid:
        if px >= ask:
            return 1
        if px <= bid:
            return -1
        mid = (bid + ask) / 2
        if px > mid:
            return 1
        if px < mid:
            return -1
    if prev_px:
        if px > prev_px:
            return 1
        if px < prev_px:
            return -1
    return prev_side or 0


def num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def read_env(path: str = ENV_FILE) -> dict:
    """KEY=VALOR por línea (# comenta). Acepta el archivo guardado por el Bloc de notas (UTF-8 con BOM).
    Las variables de entorno de Windows pisan el archivo."""
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip().upper()] = v.strip().strip('"').strip("'")
    for k in DEFAULTS:
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


def create_env(path: str = ENV_FILE) -> str:
    """Crea (o completa) puente.env con una clave nueva. Devuelve la clave."""
    cfg = read_env(path)
    token = cfg.get("BRIDGE_TOKEN") or secrets.token_urlsafe(32)
    cfg["BRIDGE_TOKEN"] = token
    lines = ["# Configuración del puente IBKR -> semáforo (no subas este archivo a GitHub: lleva tu clave)"]
    lines += [f"{k}={cfg[k]}" for k in DEFAULTS]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return token


def http_post(url: str, token: str, payload: dict, timeout: float = 10) -> dict:
    data = json.dumps(payload, allow_nan=False).encode()  # nunca NaN (ValueError aquí es un error del puente)
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "X-Token": token, "User-Agent": f"puente-ibkr/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise PermissionError("El semáforo rechazó la clave: BRIDGE_TOKEN de puente.env no es igual al de Render.")
        if e.code == 503:
            raise PermissionError("Falta BRIDGE_TOKEN en Render (radar-semaforo > Environment).")
        raise ConnectionError(f"el semáforo respondió {e.code}")
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError) as e:
        raise ConnectionError(f"sin conexión con el semáforo ({type(e).__name__})")
    if not isinstance(out, dict):
        raise ConnectionError("respuesta inesperada del semáforo")
    return out


def last_price(t) -> float | None:
    """Último precio negociado del Ticker (NaN mientras IBKR no mande una operación)."""
    return num(getattr(t, "last", None))


class Puente:
    def __init__(self, cfg: dict, ib=None, post=http_post):
        self.cfg = cfg
        self.ib = ib
        self.post = post
        self.url = cfg["SEMAFORO_URL"].rstrip("/") + "/api/bridge"
        self.codes: list[str] | None = None   # escáneres que existen en IBKR (se leen al conectar)
        self.scan: dict[str, list[str]] = {}
        self.scan_new = False                  # hay un escaneo terminado que el semáforo aún no recibió
        self.scan_at = -1e9
        self.scan_task: asyncio.Task | None = None
        self.contracts: dict = {}              # ticker -> contrato de IBKR (de los escáneres o buscado)
        self.bad: dict[str, float] = {}        # tickers que IBKR no reconoce -> cuándo reintentar
        self.tickers: dict = {}                # ticker -> Ticker con precio al instante
        self.armed: dict[str, dict] = {}       # lo que el semáforo dice vigilar: {ticker: {level, entry}}
        self.follow: list[str] = []            # lo que el semáforo pide seguir (flujo), en orden de prioridad
        self.phase: str | None = None
        self.sent_px: dict[str, tuple] = {}    # último (precio, bid, ask) mandado de cada acción
        self.sent_at: dict[str, float] = {}
        self.flow_on = str(cfg.get("FLOW", "1")).strip() != "0"
        self.large = float(cfg.get("LARGE_USD") or 25000)
        su = str(cfg.get("SIZE_UNIT") or "auto").strip().lower()
        self.unit = 100.0 if su == "100" else 1.0  # IBKR manda los tamaños en acciones salvo que la API esté en lotes
        self.unit_auto = su not in ("1", "100")    # en auto, el semáforo lo confirma comparando con el volumen de Yahoo
        self.max_lines = max(1, int(cfg.get("MAX_LINES") or 60))
        self.qs: dict[str, dict] = {}          # ticker -> bid/ask/último precio y lado (para clasificar operaciones)
        self.flow: dict[str, dict[int, list]] = {}   # ticker -> minuto -> registro (FLOW_COLS sin "m")
        self.flow_ver: dict[tuple, int] = {}   # (ticker, minuto) cambiados que el semáforo aún no recibió
        self.pruned_at = 0
        self.crossed: set[str] = set()
        self.error: str | None = None
        self.error_code: int | None = None
        self.error_t = -1e9
        self.state = "desconectado"
        self.last_push = -1e9
        self.wake = asyncio.Event()
        self.stopping = False
        self._hooked = False

    # ---------------- IBKR ----------------
    def set_error(self, code: int | None, short: str | None):
        self.error, self.error_code, self.error_t = short, code, time.monotonic()

    def on_error(self, req_id, code, msg, contract=None):
        try:
            code = int(code)
        except (TypeError, ValueError):
            return
        if code in CLEARS:
            if self.error_code in CLEARS[code]:
                log.info("IBKR: %s", msg)
                self.set_error(None, None)
            if code == 1101:  # conexión recuperada pero con datos perdidos: hay que volver a pedir los precios
                log.info("IBKR pide volver a suscribir los precios; lo hago ya.")
                self.drop_lines()
                self.wake.set()
            return
        if code == 101:  # tope de líneas: se siguen menos acciones (las de más prioridad) y se vuelven a pedir
            short, hint = HINTS[code]
            self.max_lines = max(5, min(self.max_lines, len(self.tickers)) - 5)
            log.warning("IBKR 101: %s Sigo como máximo %d.", hint, self.max_lines)
            self.set_error(code, short)
            self.drop_lines()
            self.wake.set()
            return
        sym = getattr(contract, "symbol", None) if contract is not None else None
        if code in PER_CONTRACT and sym:
            if time.monotonic() >= self.bad.get(sym, 0):
                log.warning("IBKR %s con %s: %s La dejo fuera %d min.", code, sym, HINTS[code][1], CONTRACT_BAD_S // 60)
            self.bad[sym] = time.monotonic() + CONTRACT_BAD_S
            t = self.tickers.pop(sym, None)
            if t is not None:
                try:
                    self.ib.cancelMktData(t.contract)
                except Exception:  # noqa: BLE001
                    pass
            self.set_error(code, HINTS[code][0])
            return
        if code in HINTS:
            short, hint = HINTS[code]
            if self.error != short:
                log.warning("IBKR %s: %s", code, hint)
            self.set_error(code, short)
        elif code == 200 and contract is not None:
            log.warning("IBKR no reconoce %s: %s", getattr(contract, "symbol", contract), msg)
        elif not (2100 <= code < 2200):  # 21xx son avisos informativos (granjas de datos OK, etc.)
            log.info("IBKR %s: %s", code, msg)

    def on_tickers(self, tickers):
        """Cada actualización de precio: si una armada cruza su gatillo, despierta el envío inmediato; las operaciones
        nuevas se clasifican y se suman al flujo del minuto."""
        for t in tickers:
            s = getattr(getattr(t, "contract", None), "symbol", None)
            a, p = self.armed.get(s), last_price(t)
            if p and self.error_code in DATA_ERRORS:
                log.info("IBKR vuelve a mandar precios.")
                self.set_error(None, None)
            if a and p and p >= a["level"] * 1.001 and s not in self.crossed:
                self.crossed.add(s)
                log.info("%s cruzó %.2f (gatillo %.2f): aviso al semáforo ya.", s, p, a["level"])
                self.wake.set()
            if self.flow_on and s:
                try:
                    self.ingest(s, t)
                except Exception:  # noqa: BLE001 (un dato raro de una acción no frena las demás)
                    log.exception("No pude sumar el flujo de %s.", s)

    def ingest(self, s: str, t):
        """Recorre los ticks de esta tanda en orden: bid y ask actualizan la cotización; cada operación (RT Volume)
        se clasifica contra la cotización de ESE momento y se suma a su minuto."""
        st = self.qs.get(s)
        if st is None:
            st = self.qs[s] = {"bid": num(getattr(t, "bid", None)), "ask": num(getattr(t, "ask", None)),
                               "px": None, "side": 0}
        for tk in getattr(t, "ticks", None) or ():
            tt = getattr(tk, "tickType", None)
            if tt in BID_TICKS:
                st["bid"] = num(getattr(tk, "price", None))
            elif tt in ASK_TICKS:
                st["ask"] = num(getattr(tk, "price", None))
            elif tt in TRADE_TICKS:
                px, sz = num(getattr(tk, "price", None)), num(getattr(tk, "size", None))
                if not px or not sz:
                    continue
                side = side_of(px, st["bid"], st["ask"], st["px"], st["side"])
                st["px"] = px
                if side:
                    st["side"] = side
                try:
                    minute = int(tk.time.timestamp() // 60)
                except (AttributeError, TypeError, ValueError, OSError):
                    minute = int(time.time() // 60)
                self.add_trade(s, minute, px, sz, side)

    def add_trade(self, s: str, minute: int, px: float, size: float, side: int):
        rec = self.flow.setdefault(s, {}).get(minute)
        if rec is None:
            rec = self.flow[s][minute] = [px, px, px, px, 0.0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0, 0.0, 0, self.unit]
        rec[1], rec[2], rec[3] = max(rec[1], px), min(rec[2], px), px
        rec[4] += size
        rec[5] += 1
        val = px * size
        rec[6 if side > 0 else 7 if side < 0 else 8] += val
        if side and val * rec[15] >= self.large:
            rec[9 if side > 0 else 10] += val
            rec[11 if side > 0 else 12] += 1
        if val > rec[13]:
            rec[13], rec[14] = val, side
        k = (s, minute)
        self.flow_ver[k] = self.flow_ver.get(k, 0) + 1

    def prune(self, now_min: int | None = None):
        """Suelta el flujo de hace más de FLOW_KEEP_MIN minutos (una vez por minuto)."""
        now_min = now_min or int(time.time() // 60)
        if now_min == self.pruned_at:
            return
        self.pruned_at = now_min
        cut = now_min - FLOW_KEEP_MIN
        for s in list(self.flow):
            recs = self.flow[s]
            for m in [m for m in recs if m < cut]:
                del recs[m]
                self.flow_ver.pop((s, m), None)
            if not recs:
                del self.flow[s]

    def flow_payload(self) -> tuple[dict, dict]:
        """Los minutos cambiados desde el último envío bueno (los más viejos primero, con tope) y sus versiones."""
        keys = sorted(self.flow_ver, key=lambda k: k[1])[:FLOW_MAX_SEND]
        out: dict[str, list] = {}
        sent = {}
        for s, m in keys:
            rec = self.flow.get(s, {}).get(m)
            if rec is None:
                continue
            o, h, lo, c, v, n, b, se, mi, bb, bs, nbb, nbs, top, ts, u = rec
            out.setdefault(s, []).append([m, round(o, 4), round(h, 4), round(lo, 4), round(c, 4), round(v, 4), n,
                                          round(b, 2), round(se, 2), round(mi, 2), round(bb, 2), round(bs, 2), nbb,
                                          nbs, round(top, 2), ts, u])
            sent[(s, m)] = self.flow_ver[(s, m)]
        return out, sent

    def on_disconnect(self):
        self.state, self.tickers, self.sent_px, self.qs = "desconectado", {}, {}, {}
        if not self.stopping:
            log.warning("Se cortó la conexión con TWS/IB Gateway; reintento en unos segundos.")

    def drop_lines(self):
        for t in list(self.tickers.values()):
            try:
                self.ib.cancelMktData(t.contract)
            except Exception:  # noqa: BLE001 (desconectado o ya cancelada)
                pass
        self.tickers, self.sent_px, self.qs = {}, {}, {}

    async def connect(self) -> bool:
        if not self._hooked:
            self.ib.errorEvent += self.on_error
            self.ib.pendingTickersEvent += self.on_tickers
            self.ib.disconnectedEvent += self.on_disconnect
            self._hooked = True
        self.state, t0 = "conectando", time.monotonic()
        host, port, cid = self.cfg["IB_HOST"], int(self.cfg["IB_PORT"]), int(self.cfg["IB_CLIENT_ID"])
        try:
            await self.ib.connectAsync(host, port, clientId=cid, timeout=15, readonly=True,
                                       fetchFields=StartupFetch(0) if StartupFetch else 0)
        except Exception as e:  # noqa: BLE001 (ConnectionRefusedError, TimeoutError, errores de la API)
            self.state = "desconectado"
            if self.error_t < t0:  # sin un motivo concreto de IBKR (p. ej. 326), el genérico
                self.set_error(None, "sin conexión con TWS/IB Gateway")
                log.warning("No pude conectar con TWS/IB Gateway en %s:%s (%s). Ábrelo, entra con tu usuario y revisa "
                            "el puerto (IB Gateway 4001, TWS 7496) y que la API esté habilitada.", host, port,
                            type(e).__name__)
            return False
        self.ib.reqMarketDataType(1)  # en vivo
        self.state, self.tickers, self.sent_px = "conectado", {}, {}
        self.set_error(None, None)
        log.info("Conectado a IBKR en %s:%s (solo lectura).", host, port)
        if self.codes is None:
            await self.load_codes()
        return True

    async def load_codes(self):
        wanted = [c.strip().upper() for c in self.cfg["SCANS"].split(",") if c.strip()]
        try:
            xml = await asyncio.wait_for(self.ib.reqScannerParametersAsync(), 30)
            have = set(re.findall(r"<scanCode>([^<]+)</scanCode>", xml or ""))
        except Exception as e:  # noqa: BLE001
            log.warning("No pude leer la lista de escáneres de IBKR (%s); uso los configurados.", type(e).__name__)
            have = set()
        if have:
            missing = [c for c in wanted if c not in have]
            if missing:
                log.warning("Estos escáneres no existen en IBKR y los salto: %s", ", ".join(missing))
            wanted = [c for c in wanted if c in have]
        self.codes = wanted
        log.info("Escáneres: %s", ", ".join(wanted) or "ninguno")

    async def scan_one(self, code: str):
        """Un escaneo: se suscribe, toma el primer resultado y SIEMPRE cancela la suscripción (en IBKR hay un tope
        de escaneos activos y la librería guarda en memoria los que no se cancelan). None si falló."""
        sub = ScannerSubscription(instrument="STK", locationCode="STK.US.MAJOR", scanCode=code, numberOfRows=50,
                                  abovePrice=float(self.cfg["MIN_PRICE"]), aboveVolume=int(self.cfg["MIN_VOLUME"]))
        try:
            data = self.ib.reqScannerSubscription(sub)
        except Exception as e:  # noqa: BLE001 (desconectado)
            log.warning("Escáner %s no se pudo pedir (%s).", code, type(e).__name__)
            return None
        got = asyncio.get_running_loop().create_future()

        def ready(*_):
            if not got.done():
                got.set_result(None)
        data.updateEvent += ready
        try:
            await asyncio.wait_for(got, float(self.cfg["SCAN_TIMEOUT"]))
            return list(data)
        except asyncio.TimeoutError:
            log.warning("El escáner %s no respondió a tiempo.", code)
            return None
        finally:
            try:
                self.ib.cancelScannerSubscription(data)
            except Exception:  # noqa: BLE001
                pass

    async def run_scans(self):
        """Los escaneos corren en segundo plano: un escáner lento no atrasa el aviso de una ruptura."""
        try:
            out = {}
            for code in list(self.codes or []):
                rows = await self.scan_one(code)
                if rows is None:
                    continue
                syms = []
                for d in rows:
                    c = getattr(getattr(d, "contractDetails", None), "contract", None)
                    s = getattr(c, "symbol", None)
                    if s and s not in syms:
                        syms.append(s)
                        self.contracts.setdefault(s, c)
                out[code] = syms
            if out:
                self.scan, self.scan_new = out, True
                log.info("Escáneres: %s",
                         " · ".join(f"{k} {', '.join(v[:5])}" for k, v in out.items() if v) or "sin resultados")
                self.wake.set()
        except Exception:  # noqa: BLE001
            log.exception("Los escáneres fallaron; reintento en el próximo turno.")

    def scans_due(self) -> bool:
        return (self.phase in ("pre", "open") and bool(self.codes) and self.ib.isConnected()
                and (self.scan_task is None or self.scan_task.done())
                and time.monotonic() - self.scan_at >= float(self.cfg["SCAN_EVERY"]))

    async def contract_for(self, s: str):
        c = self.contracts.get(s)
        if c is None:
            try:
                res = await asyncio.wait_for(self.ib.qualifyContractsAsync(Stock(s, "SMART", "USD")), 10)
            except Exception:  # noqa: BLE001
                res = []
            c = res[0] if res and not isinstance(res[0], list) else None
            if c is None:
                return None
            self.contracts[s] = c
        c.exchange = "SMART"  # precio consolidado (el escáner puede traer la bolsa principal)
        return c

    def wanted(self) -> list[str]:
        """Qué seguir, en orden de prioridad y con tope de líneas: primero las armadas (su ruptura es lo más urgente),
        después lo que pide el semáforo para el flujo."""
        want = list(self.armed) + (self.follow if self.flow_on else [])
        now = time.monotonic()
        want = [s for s in dict.fromkeys(want) if now >= self.bad.get(s, 0)]  # las rechazadas no gastan línea
        return want[: self.max_lines]

    async def sync_lines(self):
        """Precio al instante (con las operaciones, RT Volume) solo de lo que el semáforo pide: las líneas de datos son
        limitadas."""
        if not self.ib.isConnected():
            return
        want = self.wanted()
        for s in [s for s in self.tickers if s not in want]:
            t = self.tickers.pop(s)
            self.sent_px.pop(s, None)
            self.qs.pop(s, None)
            try:
                self.ib.cancelMktData(t.contract)
            except Exception:  # noqa: BLE001
                pass
        for s in want:
            if s in self.tickers or time.monotonic() < self.bad.get(s, 0):
                continue
            c = await self.contract_for(s)
            if c is None:
                log.warning("IBKR no encuentra %s; reintento en 5 min.", s)
                self.bad[s] = time.monotonic() + 300
                continue
            if not self.ib.isConnected():
                return
            try:
                self.tickers[s] = self.ib.reqMktData(c, GENERIC if self.flow_on else "", False, False)
            except Exception as e:  # noqa: BLE001 (se cortó la conexión en medio)
                log.warning("No pude pedir el precio de %s (%s).", s, type(e).__name__)
                return
        self.crossed &= set(self.armed)

    # ---------------- semáforo ----------------
    def quotes(self, now: float | None = None) -> dict:
        """Precio, bid y ask de lo que cambió (o de lo que lleva QUOTE_RESEND_S sin mandarse: así el semáforo sabe que
        sigue fresco), con el volumen del día en unidades de IBKR (con él el semáforo confirma la unidad)."""
        now = now or time.monotonic()
        out = {}
        for s, t in list(self.tickers.items()):
            p = last_price(t)
            if not p:
                continue
            bid, ask = num(getattr(t, "bid", None)), num(getattr(t, "ask", None))
            if self.sent_px.get(s) != (p, bid, ask) or now - self.sent_at.get(s, -1e9) >= QUOTE_RESEND_S:
                out[s] = {"last": p, "high": num(getattr(t, "high", None)), "bid": bid, "ask": ask,
                          "vol": num(getattr(t, "rtVolume", None)) or num(getattr(t, "volume", None))}
        return out

    async def push(self, scan: bool = False) -> str:
        """Manda precios nuevos, el flujo de los minutos que cambiaron (y el último escaneo) y recibe qué vigilar y qué
        seguir. Devuelve 'ok', 'auth' o 'net'."""
        q = self.quotes()
        sent_scan = self.scan if scan else None
        flow, flow_sent = self.flow_payload() if self.flow_on else ({}, {})
        payload = {"v": 1, "quotes": q, "info": {"ib": self.state, "error": self.error, "lines": len(self.tickers),
                                                 "max_lines": self.max_lines, "ver": VERSION, "unit": self.unit,
                                                 "large_usd": self.large, "flow": self.flow_on}}
        if scan:
            payload["scan"] = sent_scan
        if flow:
            payload["flow"] = flow
        try:
            resp = await asyncio.to_thread(self.post, self.url, self.cfg["BRIDGE_TOKEN"], payload)
        except PermissionError as e:
            log.error("%s", e)
            return "auth"
        except ConnectionError as e:
            log.warning("No pude avisar al semáforo: %s", e)
            return "net"
        self.last_push = time.monotonic()
        if scan and self.scan is sent_scan:
            self.scan_new = False
        self.sent_px.update({s: (v["last"], v["bid"], v["ask"]) for s, v in q.items()})
        self.sent_at.update({s: self.last_push for s in q})
        for k, ver in flow_sent.items():  # lo que cambió mientras se mandaba queda para el próximo envío
            if self.flow_ver.get(k) == ver:
                del self.flow_ver[k]
        phase = resp.get("phase")
        armed = {str(s): a for s, a in (resp.get("armed") or {}).items() if isinstance(a, dict) and num(a.get("level"))}
        if phase != self.phase:
            log.info("Mercado: %s", {"pre": "pre-market", "open": "sesión", "late": "última media hora",
                                     "closed": "cerrado"}.get(phase, phase))
        if set(armed) != set(self.armed):
            log.info("Vigilando: %s", ", ".join(f"{s} (gatillo {float(a['level']):.2f})" for s, a in armed.items())
                     or "nada armado")
        for s in resp.get("fired") or []:
            log.info("El semáforo avisó la ruptura de %s por Telegram.", s)
        if "follow" in resp:  # un semáforo viejo no lo manda: entonces solo las armadas, como antes
            follow = [x for x in (str(v).upper() for v in (resp.get("follow") or [])[:200]) if SYM_RX.fullmatch(x)]
            if len(follow) != len(self.follow) and (not self.follow or not follow or abs(len(follow) - len(self.follow)) >= 5):
                log.info("Sigo el flujo de %d acciones (tope %d líneas).", len(follow), self.max_lines)
            self.follow = follow
        u = resp.get("unit")
        if self.unit_auto and u in (1, 100) and float(u) != self.unit:
            log.info("El semáforo confirmó que IBKR manda los tamaños %s: ajusto la cuenta.",
                     "en acciones" if u == 1 else "en lotes de 100")
            self.unit = float(u)
        self.phase, self.armed = phase, armed
        return "ok"

    # ---------------- bucle ----------------
    async def step(self) -> float:
        """Una vuelta: conectar si hace falta, lanzar escaneos, ajustar precios al instante y avisar al semáforo.
        Devuelve cuántos segundos esperar (1 s si hay algo armado en sesión; un cruce despierta antes)."""
        woke = self.wake.is_set()
        self.wake.clear()
        if not self.ib.isConnected():
            self.tickers, self.sent_px = {}, {}
            if not await self.connect():
                await self.push()  # el semáforo ve "desconectado" y el motivo
                return 15
        if self.scans_due():
            self.scan_at = time.monotonic()
            self.scan_task = asyncio.create_task(self.run_scans())
        await self.sync_lines()
        self.prune()
        live = self.phase in ("pre", "open", "late")  # en la última media hora sigue el flujo (no hay escaneos)
        q = self.quotes()
        since = time.monotonic() - self.last_push
        urgent = self.scan_new or woke or any(s in self.armed for s in q)  # una armada que se mueve sale ya
        if urgent or (since >= PUSH_S and (q or self.flow_ver)) or since >= (5 if live else 30):
            res = await self.push(self.scan_new)
            if res == "auth":
                return 30
            if res == "net":
                return 3 if live else 15
            await self.sync_lines()  # lo recién armado se suscribe ya, sin esperar otra vuelta
        if live and (self.armed or self.tickers):
            return 1
        return 5 if live else 30

    async def run(self):
        while not self.stopping:
            try:
                delay = await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 (nunca dejar caer el puente en plena sesión)
                log.exception("Error inesperado en el puente; sigo en 5 s.")
                delay = 5
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass


def no_quickedit():
    """En la consola clásica de Windows, un clic dentro de la ventana (modo QuickEdit) congela el programa hasta
    apretar una tecla. Se apaga para que un clic sin querer no deje el puente detenido en plena sesión."""
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-10)  # entrada estándar
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)  # sin QUICK_EDIT, con EXTENDED_FLAGS
    except Exception:  # noqa: BLE001
        pass


async def amain(cfg: dict):
    ib = IB()
    p = Puente(cfg, ib)
    try:
        await p.run()
    finally:
        p.stopping = True
        if ib.isConnected():
            ib.disconnect()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Puente IBKR -> semáforo (solo datos, sin órdenes)")
    ap.add_argument("--crear-clave", action="store_true", help="crea puente.env con una clave nueva para BRIDGE_TOKEN")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    handlers = [logging.StreamHandler()]
    try:
        handlers.append(logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2,
                                                             encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", handlers=handlers)
    logging.getLogger("ib_async").setLevel(logging.WARNING)  # sin el ruido informativo de la librería
    if args.crear_clave:
        token = create_env()
        print(f"Listo: {ENV_FILE}\n\nCopia esta clave en Render > radar-semaforo > Environment > BRIDGE_TOKEN:\n\n"
              f"{token}\n\nDespués arranca el puente con iniciar_puente.bat.")
        return 0
    cfg = read_env()
    if not cfg["BRIDGE_TOKEN"]:
        print("Falta la clave. Corre primero:  py puente_ibkr.py --crear-clave")
        return 1
    if IB is None:
        print("Falta la librería de IBKR. Instálala con:  py -m pip install -r requirements.txt")
        return 1
    no_quickedit()
    log.info("Puente IBKR %s -> %s (solo datos, sin órdenes). Ctrl+C para salir.", VERSION, cfg["SEMAFORO_URL"])
    try:
        asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        pass
    log.info("Puente detenido.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
