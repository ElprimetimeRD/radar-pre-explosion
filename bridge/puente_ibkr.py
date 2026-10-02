"""Puente IBKR -> semáforo. Corre en la PC de Priamo, conectado a TWS o IB Gateway en modo de SOLO LECTURA:

- cada 30 s le pasa al semáforo lo que ven los escáneres de IBKR (los que más suben, volumen inusual y ritmo de
  operaciones), que detectan una acción que arranca antes que las listas de Yahoo;
- se suscribe al precio al instante de las acciones armadas (las que el semáforo le dice) y, si una cruza su
  gatillo, se lo manda en el acto para que llegue el aviso ⚡ por Telegram;
- si el semáforo lo pide (IBKR_BARS en Render), sigue con velas de 1 min de IBKR a las candidatas principales (el día
  completo y después al día cada ~5 s) para que el semáforo avise la COMPRA sin esperar las velas de Yahoo.

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

VERSION = "1.3"
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
    "MAX_LINES": "40",           # tope de precios al instante a la vez (IBKR da 100 líneas)
    "MAX_BARS": "15",            # tope de acciones con velas de 1 min a la vez (IBKR permite 50 pedidos abiertos)
}
BAR_BUDGET = 2500  # velas por envío (~130 KB): la historia de una acción nunca se parte, la que no cabe va después
SYM = re.compile(r"[A-Z]{1,5}")
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
    1100: ("IBKR sin conexión (1100)", "IBKR perdió la conexión con sus servidores; el puente espera a que vuelva."),
    2110: ("IBKR sin conexión (2110)", "TWS/IB Gateway perdió la conexión con los servidores de IBKR."),
    2103: ("precios de IBKR caídos (2103)", "TWS/IB Gateway perdió el servidor de precios de IBKR; suele volver solo."),
    2105: ("escáneres de IBKR caídos (2105)", "TWS/IB Gateway perdió el servidor de datos históricos; suele volver solo."),
}
CLEARS = {1101: (1100, 2110), 1102: (1100, 2110), 2104: (2103,), 2106: (2105,)}  # aviso de OK -> errores que limpia
DATA_ERRORS = {162, 354, 10089, 10090, 10167, 10168, 10197, 2103}  # se limpian solos cuando vuelven a llegar datos
NO_VELAS = "sin velas de IBKR (162)"

log = logging.getLogger("puente")


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


def bar_row(b) -> list | None:
    """Vela de 1 min de ib_async -> [hora en s, apertura, máximo, mínimo, cierre, volumen], redondeada (menos bytes).
    None si no sirve. La hora llega en UTC (formatDate=2), así que no depende del reloj ni de la zona de la PC."""
    stamp = getattr(getattr(b, "date", None), "timestamp", None)
    o, h, lo, c = (num(getattr(b, k, None)) for k in ("open", "high", "low", "close"))
    try:
        t, v = int(stamp()), float(getattr(b, "volume", None))
    except (TypeError, ValueError, OverflowError):
        return None
    if not (o and h and lo and c) or not math.isfinite(v) or v < 0:
        return None
    return [t, round(o, 4), round(h, 4), round(lo, 4), round(c, 4), round(v)]


class Quiet(logging.Filter):
    """Saca del registro dos avisos normales de la librería: IBKR confirmando cada escaneo que el puente cancela (162,
    'scanner subscription cancelled') y la respuesta a cancelar velas que IBKR ya había cerrado (366)."""

    def filter(self, rec: logging.LogRecord) -> bool:
        if not rec.name.startswith("ib_async"):
            return True
        m = rec.getMessage().lower()
        return "scanner subscription cancelled" not in m and "error 366" not in m


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
        self.bar_want: dict[str, int] = {}     # velas que pide el semáforo: {ticker: hora de la última que ya tiene}
        self.bar_t0 = 0                        # desde qué hora mandar velas (4:00 ET de hoy; lo dice el semáforo)
        self.bar_subs: dict = {}               # ticker -> velas de 1 min que IBKR mantiene al día (BarDataList)
        self.bar_sent: dict[str, list] = {}    # última vela mandada de cada ticker (no se repite si no cambió)
        self.bar_bad: dict[str, float] = {}    # tickers cuyas velas fallaron -> cuándo reintentar
        self.full_try: dict[str, tuple[int, float]] = {}  # envíos del día completo sin acuse -> (veces, próximo)
        self.bars_error: str | None = None     # problema de las velas (162), aparte del de los precios
        self.bars_task: asyncio.Task | None = None
        self.conn_n = 0                        # conexiones hechas (una suscripción de otra conexión ya no sirve)
        self.phase: str | None = None
        self.sent_px: dict[str, float] = {}
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
        text = str(msg or "")
        if code == 162 and "scanner subscription cancelled" in text.lower():
            return  # normal: el puente cancela cada escaneo al terminarlo y IBKR lo confirma así
        if code == 366:
            return  # cancelar velas que IBKR ya había cerrado: sin importancia
        s = next((s for s, b in self.bar_subs.items() if getattr(b, "reqId", None) == req_id), None)
        if s and not (2100 <= code < 2200):
            log.warning("IBKR cortó las velas de %s (%s: %s); las vuelvo a pedir en 2 min.", s, code, text)
            self.drop_bar(s)
            self.bar_bad[s] = time.monotonic() + 120
            if code != 162 and code not in HINTS:
                return  # ya quedó en el registro; los de datos (162, 10089…) siguen y marcan el estado
        if code == 162:  # error del servicio de datos históricos (permisos o acción sin datos)
            # Se guarda aparte: antes el primer precio que llegaba lo borraba y el estado decía OK con las velas caídas
            if self.bars_error != NO_VELAS:
                log.warning("IBKR no dio velas (162): %s. Si habla de permisos, es lo mismo que el 10089: falta la "
                            "suscripción de datos para la API.", text)
            self.bars_error = NO_VELAS
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
        """Cada actualización de precio: si una armada cruza su gatillo, despierta el envío inmediato."""
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

    def on_disconnect(self):
        self.state, self.tickers, self.sent_px = "desconectado", {}, {}
        self.bar_subs, self.bar_sent = {}, {}  # las suscripciones mueren con la conexión: al volver se piden de nuevo
        if not self.stopping:
            log.warning("Se cortó la conexión con TWS/IB Gateway; reintento en unos segundos.")

    def drop_lines(self):
        for t in list(self.tickers.values()):
            try:
                self.ib.cancelMktData(t.contract)
            except Exception:  # noqa: BLE001 (desconectado o ya cancelada)
                pass
        self.tickers, self.sent_px = {}, {}
        for s in list(self.bar_subs):
            self.drop_bar(s)

    def drop_bar(self, s: str):
        b = self.bar_subs.pop(s, None)
        self.bar_sent.pop(s, None)
        if b is not None:
            self._cancel_bars(b)

    def _cancel_bars(self, b):
        try:
            self.ib.cancelHistoricalData(b)
        except Exception:  # noqa: BLE001 (desconectado o ya cerrada)
            pass

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
        self.bar_subs, self.bar_sent = {}, {}
        self.conn_n += 1
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

    async def sync_lines(self):
        """Precio al instante solo para lo que el semáforo tiene armado (y nada más: líneas de datos limitadas)."""
        if not self.ib.isConnected():
            return
        want = list(self.armed)[: int(self.cfg["MAX_LINES"])]
        for s in [s for s in self.tickers if s not in want]:
            t = self.tickers.pop(s)
            self.sent_px.pop(s, None)
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
                self.tickers[s] = self.ib.reqMktData(c, "", False, False)
            except Exception as e:  # noqa: BLE001 (se cortó la conexión en medio)
                log.warning("No pude pedir el precio de %s (%s).", s, type(e).__name__)
                return
        self.crossed &= set(self.armed)

    def sync_bars(self):
        """Velas de 1 min solo para lo que pide el semáforo: suelta lo que ya no pide y pide lo nuevo en segundo plano
        (cada pedido tarda 1–2 s y no debe atrasar el aviso de una ruptura)."""
        if not self.ib.isConnected():
            return
        for s in [s for s in self.bar_subs if s not in self.bar_want]:
            self.drop_bar(s)
        now = time.monotonic()
        need = [s for s in self.bar_want if s not in self.bar_subs and now >= self.bar_bad.get(s, 0)]
        if need and (self.bars_task is None or self.bars_task.done()):
            self.bars_task = asyncio.create_task(self.sub_bars(need))

    async def sub_bars(self, syms: list[str]):
        """Pide, una por una, las velas de 1 min de hoy (con pre-market) que IBKR mantiene al día: la que se está
        formando se actualiza cada ~5 s. Si IBKR no las da (permisos, acción sin datos), reintenta en 5 min."""
        try:
            for s in syms:
                if s not in self.bar_want or s in self.bar_subs or not self.ib.isConnected():
                    continue
                n = self.conn_n
                c = await self.contract_for(s)
                if c is None:
                    log.warning("IBKR no encuentra %s; reintento sus velas en 5 min.", s)
                    self.bar_bad[s] = time.monotonic() + 300
                    continue
                try:
                    bars = await self.ib.reqHistoricalDataAsync(
                        c, endDateTime="", durationStr="1 D", barSizeSetting="1 min", whatToShow="TRADES",
                        useRTH=False, formatDate=2, keepUpToDate=True, timeout=30)
                except Exception as e:  # noqa: BLE001 (se cortó la conexión en medio)
                    log.warning("No pude pedir las velas de %s (%s).", s, type(e).__name__)
                    self.bar_bad[s] = time.monotonic() + 60
                    continue
                if not bars or s not in self.bar_want or not self.ib.isConnected() or n != self.conn_n:
                    self._cancel_bars(bars)  # vacía (error o tiempo agotado), ya no se pide o de una conexión anterior
                    if not bars:
                        log.warning("IBKR no mandó velas de %s; reintento en 5 min.", s)
                        self.bar_bad[s] = time.monotonic() + 300
                    continue
                self.bar_subs[s] = bars
                if self.bars_error:
                    log.info("IBKR vuelve a mandar velas.")
                    self.bars_error = None
                if self.error_code in DATA_ERRORS:
                    log.info("IBKR vuelve a mandar datos.")
                    self.set_error(None, None)
                self.wake.set()  # el día completo sale en el próximo envío
        except Exception:  # noqa: BLE001
            log.exception("Las velas fallaron; reintento en la próxima vuelta.")

    def full_ok(self, s: str) -> bool:
        """¿Toca mandar (otra vez) el día completo de s? Si el semáforo no lo guardó (sigue en 0), espera 15 s, 30 s,
        1 min… hasta 5 min entre intentos: antes lo reenviaba cada segundo sin fin (~130 KB cada vez)."""
        return time.monotonic() >= self.full_try.get(s, (0, 0.0))[1]

    def bars_pending(self) -> bool:
        """¿Hay una acción con velas cuyo día completo el semáforo todavía no tiene (y toca mandarlo)?"""
        return any(len(b) and not self.bar_want.get(s) and self.full_ok(s) for s, b in list(self.bar_subs.items()))

    def bars_payload(self) -> dict:
        """Velas para el semáforo: de cada acción, desde la última que ya tiene (la que se está formando puede haber
        cambiado) o el día completo si no tiene ninguna. Primero lo que es solo una actualización; una historia que no
        cabe en este envío va en el siguiente (nunca se parte)."""
        out, left = {}, BAR_BUDGET
        for s in sorted(self.bar_subs, key=lambda s: not self.bar_want.get(s)):
            have = self.bar_want.get(s, 0)
            if not have and not self.full_ok(s):
                continue
            frm = max(have, self.bar_t0)
            rows = []
            for b in reversed(list(self.bar_subs[s])):
                r = bar_row(b)
                if r is None:
                    continue
                if r[0] < frm:
                    break
                rows.append(r)
                if len(rows) >= 1000:  # un día con pre y after-hours tiene 960
                    break
            rows.reverse()
            if not rows or (have and rows == [self.bar_sent.get(s)]):
                continue  # sin nada nuevo
            if len(rows) > left and out:
                continue
            out[s] = rows
            left -= len(rows)
            if not have:
                n = self.full_try.get(s, (0, 0.0))[0]
                if n >= 2:
                    log.warning("El semáforo no guardó las velas de %s (%d envíos); reintento en %d s.", s, n,
                                min(300, 15 * 2 ** n))
                self.full_try[s] = (n + 1, time.monotonic() + min(300, 15 * 2 ** n))
        return out

    # ---------------- semáforo ----------------
    def quotes(self) -> dict:
        out = {}
        for s, t in list(self.tickers.items()):
            p = last_price(t)
            if p and self.sent_px.get(s) != p:
                out[s] = {"last": p, "high": num(getattr(t, "high", None)), "bid": num(getattr(t, "bid", None)),
                          "ask": num(getattr(t, "ask", None))}
        return out

    async def push(self, scan: bool = False) -> str:
        """Manda precios y velas nuevas (y el último escaneo) y recibe qué vigilar. Devuelve 'ok', 'auth' o 'net'."""
        q = self.quotes()
        bars = self.bars_payload()
        sent_scan = self.scan if scan else None
        payload = {"v": 1, "quotes": q, "info": {"ib": self.state, "error": self.error, "lines": len(self.tickers),
                                                 "bars": len(self.bar_subs), "bars_error": self.bars_error,
                                                 "ver": VERSION}}
        if scan:
            payload["scan"] = sent_scan
        if bars:
            payload["bars"] = bars
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
        self.sent_px.update({s: v["last"] for s, v in q.items()})
        self.bar_sent.update({s: rows[-1] for s, rows in bars.items() if s in self.bar_subs})
        phase = resp.get("phase")
        armed = {str(s): a for s, a in (resp.get("armed") or {}).items() if isinstance(a, dict) and num(a.get("level"))}
        raw = resp.get("bars") if isinstance(resp.get("bars"), dict) else {}
        want = {}
        for s, t in list(raw.items())[: int(self.cfg["MAX_BARS"])]:
            s = str(s).upper()
            if SYM.fullmatch(s):
                want[s] = int(num(t) or 0)
        if phase != self.phase:
            log.info("Mercado: %s", {"pre": "pre-market", "open": "sesión", "late": "última media hora",
                                     "closed": "cerrado"}.get(phase, phase))
        if set(armed) != set(self.armed):
            log.info("Vigilando: %s", ", ".join(f"{s} (gatillo {float(a['level']):.2f})" for s, a in armed.items())
                     or "nada armado")
        if set(want) != set(self.bar_want):
            log.info("Velas de 1 min: %s", ", ".join(want) or "ninguna")
        for s in resp.get("fired") or []:
            log.info("El semáforo avisó la ruptura de %s por Telegram.", s)
        for s in resp.get("early") or []:
            log.info("El semáforo avisó la COMPRA de %s por Telegram con velas de IBKR.", s)
        for s, t in want.items():
            if t:
                self.full_try.pop(s, None)  # el semáforo ya tiene su día: los envíos vuelven a ser solo lo nuevo
        self.phase, self.armed, self.bar_want = phase, armed, want
        self.bar_t0 = int(num(resp.get("bars_t0")) or 0)
        return "ok"

    # ---------------- bucle ----------------
    async def step(self) -> float:
        """Una vuelta: conectar si hace falta, lanzar escaneos, ajustar precios al instante y avisar al semáforo.
        Devuelve cuántos segundos esperar (1 s si hay algo armado en sesión; un cruce despierta antes)."""
        woke = self.wake.is_set()
        self.wake.clear()
        if not self.ib.isConnected():
            self.tickers, self.sent_px = {}, {}
            self.bar_subs, self.bar_sent = {}, {}
            if not await self.connect():
                await self.push()  # el semáforo ve "desconectado" y el motivo
                return 15
        if self.scans_due():
            self.scan_at = time.monotonic()
            self.scan_task = asyncio.create_task(self.run_scans())
        await self.sync_lines()
        self.sync_bars()
        live = self.phase in ("pre", "open")
        if (self.scan_new or woke or self.quotes() or self.bars_pending()
                or time.monotonic() - self.last_push >= (5 if live else 30)):
            res = await self.push(self.scan_new)
            if res == "auth":
                return 30
            if res == "net":
                return 3 if live else 15
            await self.sync_lines()  # lo recién armado se suscribe ya, sin esperar otra vuelta
            self.sync_bars()
        if (self.phase == "open" and self.armed) or self.bars_pending():
            return 1
        return 5 if self.phase in ("pre", "open") else 30

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
    for h in handlers:
        h.addFilter(Quiet())  # en el manejador: los filtros de un logger no se aplican a sus hijos (ib_async.wrapper)
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
