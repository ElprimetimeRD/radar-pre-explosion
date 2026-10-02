"""Puente IBKR -> semáforo. Corre en la PC de Priamo, conectado a TWS o IB Gateway en modo de SOLO LECTURA:

- cada 30 s le pasa al semáforo lo que ven los escáneres de IBKR (los que más suben, volumen inusual y ritmo de
  operaciones), que detectan una acción que arranca antes que las listas de Yahoo;
- se suscribe al precio al instante de las acciones armadas (las que el semáforo le dice) y, si una cruza su
  gatillo, se lo manda en el acto para que llegue el aviso ⚡ por Telegram.

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

VERSION = "1.1"
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
    1100: ("IBKR sin conexión (1100)", "IBKR perdió la conexión con sus servidores; el puente espera a que vuelva."),
    2110: ("IBKR sin conexión (2110)", "TWS/IB Gateway perdió la conexión con los servidores de IBKR."),
    2103: ("precios de IBKR caídos (2103)", "TWS/IB Gateway perdió el servidor de precios de IBKR; suele volver solo."),
    2105: ("escáneres de IBKR caídos (2105)", "TWS/IB Gateway perdió el servidor de datos históricos; suele volver solo."),
}
CLEARS = {1101: (1100, 2110), 1102: (1100, 2110), 2104: (2103,), 2106: (2105,)}  # aviso de OK -> errores que limpia
DATA_ERRORS = {354, 10089, 10090, 10167, 10168, 10197, 2103}  # se limpian solos cuando vuelven a llegar precios

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
        if not self.stopping:
            log.warning("Se cortó la conexión con TWS/IB Gateway; reintento en unos segundos.")

    def drop_lines(self):
        for t in list(self.tickers.values()):
            try:
                self.ib.cancelMktData(t.contract)
            except Exception:  # noqa: BLE001 (desconectado o ya cancelada)
                pass
        self.tickers, self.sent_px = {}, {}

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
        """Manda precios nuevos (y el último escaneo) y recibe qué vigilar. Devuelve 'ok', 'auth' o 'net'."""
        q = self.quotes()
        sent_scan = self.scan if scan else None
        payload = {"v": 1, "quotes": q, "info": {"ib": self.state, "error": self.error, "lines": len(self.tickers),
                                                 "ver": VERSION}}
        if scan:
            payload["scan"] = sent_scan
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
        live = self.phase in ("pre", "open")
        if self.scan_new or woke or self.quotes() or time.monotonic() - self.last_push >= (5 if live else 30):
            res = await self.push(self.scan_new)
            if res == "auth":
                return 30
            if res == "net":
                return 3 if live else 15
            await self.sync_lines()  # lo recién armado se suscribe ya, sin esperar otra vuelta
        if self.phase == "open" and self.armed:
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
