"""Datos de Yahoo para el ejecutor de Claude (bridge/ejecutor_claude.py), sin librerías extra: solo urllib.

Pide a Yahoo (endpoint público de gráficos) las velas de 1 min de hoy de cada acción de la lista y, una vez al día, sus
velas diarias (cierre de ayer, volumen medio de 20 días y ATR de 14 días). Corre en un hilo aparte: el ejecutor nunca
espera a Yahoo. Las velas llegan con 1–2 min de retraso; la última vela de cada acción casi siempre está incompleta y se
descarta.

Prueba rápida (también sirve fuera de horario: Yahoo devuelve la última sesión):  python datos_yahoo.py --probar
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("ejecutor.datos")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
OPEN_M, CLOSE_M = 9 * 60 + 30, 16 * 60
INTERVALO_MIN, INTERVALO_MAX = 60, 240   # s entre barridos: sube si Yahoo limita las peticiones (429) y baja al normalizarse
LIMPIOS_PARA_BAJAR = 8                   # barridos seguidos sin 429 para volver a leer más seguido


class SinDatos(Exception):
    pass


def http_json(url: str, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise SinDatos(f"HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise SinDatos(type(e).__name__) from e


def _resultado(j: dict) -> dict:
    try:
        r = j["chart"]["result"][0]
        r["timestamp"], r["indicators"]["quote"][0]
        return r
    except (KeyError, IndexError, TypeError) as e:
        raise SinDatos("respuesta sin datos") from e


def _local(ts: float, off: float) -> tuple[str, int]:
    """(fecha, minuto del día) en la hora de la bolsa, con el desfase que informa Yahoo (gmtoffset)."""
    t = int(ts + off)
    dia = time.strftime("%Y-%m-%d", time.gmtime(t))
    return dia, (t % 86400) // 60


def parse_1m(j: dict, ahora: float) -> tuple[str | None, list[tuple]]:
    """Velas de 1 min de la última sesión: (fecha, [(m, o, h, l, c, v)]). Descarta la vela en curso y las que no son de
    la sesión regular."""
    r = _resultado(j)
    off = float((r.get("meta") or {}).get("gmtoffset") or -14400)
    q = r["indicators"]["quote"][0]
    out, dia_ult = [], None
    for i, ts in enumerate(r["timestamp"]):
        if ts is None or ts + 60 > ahora - 5:       # la vela de este minuto todavía se está formando
            continue
        try:
            o, h, l, c, v = (q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i])
        except (IndexError, KeyError):
            continue
        if None in (o, h, l, c):
            continue
        dia, m = _local(ts, off)
        if not OPEN_M <= m < CLOSE_M:
            continue
        dia_ult = dia
        out.append((dia, m, float(o), float(h), float(l), float(c), float(v or 0)))
    out = [b[1:] for b in out if b[0] == dia_ult]
    return dia_ult, out


def parse_diario(j: dict, hoy: str) -> dict:
    """Cierre de ayer, volumen medio de 20 días y ATR de 14 días (en % del precio), sin contar la vela de hoy."""
    r = _resultado(j)
    off = float((r.get("meta") or {}).get("gmtoffset") or -14400)
    q = r["indicators"]["quote"][0]
    filas = []
    for i, ts in enumerate(r["timestamp"]):
        try:
            h, l, c, v = q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
        except (IndexError, KeyError):
            continue
        if ts is None or None in (h, l, c) or _local(ts, off)[0] >= hoy:
            continue
        filas.append((float(h), float(l), float(c), float(v or 0)))
    if len(filas) < 15:
        raise SinDatos("pocas velas diarias")
    cierres = [f[2] for f in filas]
    tr = [max(f[0] - f[1], abs(f[0] - cierres[i]), abs(f[1] - cierres[i])) for i, f in enumerate(filas[1:])]
    vols = [f[3] for f in filas[-20:] if f[3] > 0]
    return {"prev": cierres[-1], "adv": sum(vols) / len(vols) if vols else None,
            "atr": sum(tr[-14:]) / len(tr[-14:]) / cierres[-1]}


def _et_por_defecto():
    """Hora de Nueva York (con horario de verano): la función del ejecutor, que ya la resuelve incluso en Windows sin
    base de zonas horarias."""
    from ejecutor_paper import et
    return et


class Feed:
    def __init__(self, simbolos, spy: str = "SPY", fetch=http_json, reloj=time.time, workers: int = 6, et=None):
        self.simbolos = [s for s in dict.fromkeys(simbolos) if s != spy]
        self.spy, self.fetch, self.reloj, self.workers = spy, fetch, reloj, workers
        self.et = et or _et_por_defecto()
        self.datos: dict[str, dict] = {}        # t -> {"bars": [...], "dia": fecha, "ts": cuándo se leyó}
        self.diarios: dict[str, dict] = {}      # t -> {"prev", "adv", "atr", "dia": fecha en que se leyó}
        self.ult_barrido = 0.0
        self.n_ok = self.n_total = 0
        self.error: str | None = None
        self.pausa_hasta = 0.0
        self.intervalo = INTERVALO_MIN          # s entre barridos (se duplica si Yahoo responde 429, hasta INTERVALO_MAX)
        self.limpios = 0                        # barridos seguidos sin 429
        self.limitado = False                   # hubo un 429 en el barrido en curso
        self._hilo: threading.Thread | None = None
        self.lock = threading.Lock()

    # ---------------- lectura ----------------
    def _url(self, t: str, rango: str, intervalo: str, k: int = 0) -> str:
        return (f"https://{HOSTS[k % 2]}/v8/finance/chart/{t}?range={rango}&interval={intervalo}"
                f"&includePrePost=false&events=")

    def _pedir(self, t: str, rango: str, intervalo: str) -> dict:
        ultimo = None
        for k in range(2):                       # un reintento por el otro servidor de Yahoo
            if self.reloj() < self.pausa_hasta:  # Yahoo acaba de decir 429: no se sigue insistiendo (empeora el castigo)
                raise SinDatos("HTTP 429 (en pausa)")
            try:
                return self.fetch(self._url(t, rango, intervalo, k))
            except SinDatos as e:
                ultimo = e
                if "429" in str(e):
                    self.limitado = True
                    self.pausa_hasta = self.reloj() + max(60, self.intervalo)
                    break
        raise ultimo or SinDatos("sin respuesta")

    def _uno(self, t: str, ahora: float, hoy: str):
        try:
            dia, bars = parse_1m(self._pedir(t, "1d", "1m"), ahora)
        except SinDatos as e:
            return t, None, str(e)
        d = self.diarios.get(t)
        if d is None or d.get("dia") != hoy:
            try:
                d = {**parse_diario(self._pedir(t, "3mo", "1d"), hoy), "dia": hoy}
            except SinDatos as e:
                return t, {"bars": bars, "dia": dia, "ts": ahora}, f"diario: {e}"
        return t, {"bars": bars, "dia": dia, "ts": ahora}, d

    def barrido(self, hoy: str | None = None) -> int:
        """Una vuelta por todas las acciones (en paralelo). Devuelve cuántas salieron bien."""
        ahora = self.reloj()
        if ahora < self.pausa_hasta:
            return 0
        hoy = hoy or self.et(ahora).date().isoformat()
        lista = self.simbolos + [self.spy]
        ok, err = 0, None
        self.limitado = False
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            res = list(ex.map(lambda s: self._uno(s, ahora, hoy), lista))
        self._ajustar_ritmo()
        with self.lock:
            for t, datos, extra in res:
                if datos is None:
                    err = f"{t}: {extra}"
                    continue
                self.datos[t] = datos
                if isinstance(extra, dict):
                    self.diarios[t] = extra
                    ok += 1
                elif isinstance(extra, str):
                    err = f"{t}: {extra}"
                    ok += t in self.diarios
                else:
                    ok += 1
            self.ult_barrido, self.n_ok, self.n_total = ahora, ok, len(lista)
            self.error = err if ok < len(lista) else None
        return ok

    def _ajustar_ritmo(self):
        """Si Yahoo limitó las peticiones (429), lee más despacio (se duplica el intervalo, hasta 4 min) en vez de seguir
        chocando; tras LIMPIOS_PARA_BAJAR barridos sin límite vuelve a acercarse al ritmo normal (cada minuto)."""
        if self.limitado:
            nuevo = min(INTERVALO_MAX, self.intervalo * 2)
            log.warning("Yahoo limitó las lecturas (429): %s", f"paso de leer cada {self.intervalo} s a cada {nuevo} s."
                        if nuevo != self.intervalo else f"sigo leyendo cada {nuevo} s (el máximo).")
            self.intervalo, self.limpios = nuevo, 0
            self.pausa_hasta = self.reloj() + self.intervalo
            return
        self.limpios += 1
        if self.intervalo > INTERVALO_MIN and self.limpios >= LIMPIOS_PARA_BAJAR:
            self.intervalo, self.limpios = max(INTERVALO_MIN, self.intervalo // 2), 0
            log.info("Yahoo responde bien otra vez: leo cada %d s.", self.intervalo)

    def espera_barrido(self) -> float:
        """Segundos hasta el próximo barrido: a los 6 s de cada minuto o, si Yahoo limitó, cada `intervalo` s."""
        return max(5.0, self.intervalo - (self.reloj() % 60) + 6)

    # ---------------- uso ----------------
    def ok(self, now: float | None = None) -> bool:
        """Datos recientes de casi todas las acciones (si no, el ejecutor no pone órdenes nuevas)."""
        now = now or self.reloj()
        return now - self.ult_barrido <= self.intervalo + 90 and self.n_ok >= max(3, int(0.6 * self.n_total))

    def snapshot(self, hoy: str | None = None) -> tuple[dict, dict]:
        """({t: datos para la estrategia}, datos de SPY). Copia: el hilo puede estar actualizando. Solo acciones con sus
        datos diarios de hoy (un cierre previo de otro día daría un cambio del día falso)."""
        hoy = hoy or self.et(self.reloj()).date().isoformat()
        with self.lock:
            datos, diarios = dict(self.datos), dict(self.diarios)
        out = {}
        for t, d in datos.items():
            dd = diarios.get(t)
            if t == self.spy or not dd or dd.get("dia") != hoy:
                continue
            out[t] = {"bars": d["bars"], "dia": d["dia"], "ts": d["ts"], "prev": dd["prev"], "adv": dd["adv"],
                      "atr": dd["atr"]}
        spy = dict(datos.get(self.spy) or {})
        dd = diarios.get(self.spy) or {}
        if spy:
            spy["prev"] = dd.get("prev") if dd.get("dia") == hoy else None
        return out, spy

    def estado(self, now: float | None = None) -> dict:
        now = now or self.reloj()
        return {"feed_ok": self.ok(now), "feed_n": self.n_ok, "feed_de": self.n_total,
                "feed_edad_s": round(now - self.ult_barrido) if self.ult_barrido else None, "feed_error": self.error,
                "feed_intervalo_s": self.intervalo}

    # ---------------- hilo ----------------
    def start(self):
        if self._hilo is None:
            self._hilo = threading.Thread(target=self._loop, name="datos-yahoo", daemon=True)
            self._hilo.start()

    def _loop(self):
        while True:
            ahora = self.reloj()
            t = self.et(ahora)
            m = t.hour * 60 + t.minute
            if t.weekday() < 5 and 9 * 60 + 25 <= m < 16 * 60 + 5:
                try:
                    self.barrido()
                except Exception:  # noqa: BLE001 (el hilo no debe morir)
                    log.exception("Error leyendo datos de Yahoo")
                espera = self.espera_barrido()          # la próxima a los 6 s de cada minuto (más espaciada si Yahoo limitó)
            else:
                espera = 30
            time.sleep(max(5, espera))


def probar(simbolos=("SPY", "NVDA", "MU", "AMD", "TSLA", "SNDK")) -> int:
    """Prueba de conexión: pide velas y diarios de unas cuantas acciones y dice qué llegó."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    f = Feed([s for s in simbolos if s != "SPY"])
    t0 = time.time()
    n = f.barrido(hoy="9999-99-99")     # fecha futura: todo cuenta como «ayer» (sirve de noche y fines de semana)
    print(f"Yahoo respondió bien para {n} de {f.n_total} acciones en {time.time() - t0:.1f} s")
    datos, spy = f.snapshot(hoy="9999-99-99")
    for t in [*datos, "SPY"]:
        d = datos.get(t) or spy
        b = d.get("bars") or []
        ult = b[-1] if b else None
        print(f"  {t:6} velas {len(b):3}  última {ult[0] // 60}:{ult[0] % 60:02d} cierre {ult[4]:.2f}" if ult else f"  {t:6} sin velas",
              f" prev {d.get('prev')}  vol.medio {d.get('adv')}  ATR% {d.get('atr')}")
    if f.error:
        print("Último error:", f.error)
    return 0 if n >= 4 else 1


if __name__ == "__main__":
    sys.exit(probar() if "--probar" in sys.argv else 0)
