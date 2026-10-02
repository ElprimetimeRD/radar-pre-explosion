# Radar Pre-Explosión · escáner automático

Escanea el mercado de EE. UU. dos veces por día hábil, evalúa cada candidato con el **mismo modelo** que la página Radar Pre-Explosión y publica el ranking en `data/artifact/`. Una tarea programada de Claude copia ese ranking a la pestaña **Ranking** de la página, y el semáforo en vivo lo usa como parte de su universo.

| Escaneo | Hora (UTC) | Verano (EDT) | Invierno (EST) |
|---|---|---|---|
| Intradía / pre-market (A) | 12:40 | 8:40 | 7:40 |
| Swing 1–20 días (B) | 21:35 | 17:35 | 16:35 |

GitHub puede retrasar los cron unos minutos. También puedes lanzarlo a mano: **Actions → Radar scan → Run workflow**.

## Configuración

- Repo público (la página lee `raw.githubusercontent.com`, que no acepta credenciales). Solo código y rankings de acciones públicas.
- Variable de Actions `SEC_USER_AGENT` (ya puesta): la SEC exige un contacto para EDGAR.
- Opcional: secretos `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` para recibir el top de cada escaneo.
- `watchlist.txt`: tickers que siempre se evalúan (escáner y semáforo).

## Cómo funciona

1. **Universo (~300):** pantallas de Yahoo (subidas > 5 %, short interest alto, volumen, small caps activas; en swing también crecimiento), las 100 acciones más mencionadas en Reddit (ApeWisdom), compras en grupo de insiders (OpenInsider) y tu watchlist.
2. **Etapa 1, datos baratos para todos:** cotización en lote, un año de precios, borrow fee de IBKR (FTP público), menciones. Filtro de liquidez: precio ≥ US$ 0.50 y volumen medio ≥ 100 k.
3. **Etapa 2, datos caros para los 70 mejores + watchlist:** float y short interest, cadena de opciones (calls OTM ≤ 30 días vol/OI, put/call, spread de IV y smirk), RVOL por hora con pre-market (intradía), compras de insiders (swing), earnings, titulares y filings de EDGAR (424B, S-1, S-3, 8-K).
4. **Score:** `scanner/engine.py`, port exacto del motor de la página. Cada ejecución verifica la paridad contra `tools/engine.js` con 1,500 casos aleatorios.
5. **Resultados:** cada escaneo queda en `data/scans/`. El escaneo de la tarde mide qué pasó: intradía = máximo y mínimo de la sesión; swing = las 20 sesiones siguientes. `data/artifact/scanlog-*.json` alimenta la pestaña Validación.

## Qué no mide (y cómo se ve en la página)

- **Utilization:** no hay fuente gratuita. La presión de cortos se calcula sin ella; la cobertura lo refleja.
- **Catalizador:** clasificación por palabras clave en titulares. Es aproximada: revisa el titular antes de actuar.
- **Opciones en pre-market:** Yahoo muestra el volumen de la sesión anterior.
- **Perfil de insiders (oportunista vs. rutinario):** no se infiere; cuenta ×0.7.
- **Warrants, caja corta y promoción pagada:** no se detectan automáticamente.

## Semáforo en vivo: COMPRA / ESPERA / NO COMPRES

**Página:** https://radar-semaforo.onrender.com · **API:** `/api/signals` · **Salud:** `/health`

`live/` es un servicio web en Render que corre toda la sesión (pre-market cada 3 min desde las 4:00 ET; cada 60 s de 9:30 a 16:00, más un **vigía rápido** que cada 15 s mira el precio de las rupturas armadas) y responde una sola pregunta: **¿qué compro ahora, si algo?**

**Cada ciclo:** universo dinámico, refrescado cada 3 min en sesión (subidas > 3 %, más activas, small caps en alza, tu watchlist, halts de NASDAQ y el escaneo de la mañana vía `RADAR_SCAN_URL`; en pre-market se ordena por el gap real de las velas de 5 min) → las 35–50 más activas → velas de 1 min → VWAP, rango de apertura (9:30–9:35; `OR_MINUTES=15` vuelve a 9:45), RVOL a la misma hora vs. los 4 días previos, velas ballena (≥ 5× la mediana y ≥ US$150 k en un minuto, clasificadas compra/venta), mínimos crecientes (escalera), extensión sobre VWAP. A las 20 más fuertes se les añade noticias clasificadas, dilución en EDGAR (424B/S-1 ≤ 30 días) y calls inusuales. Mercado: SPY y QQQ contra su VWAP.

| Decisión | Cuándo |
|---|---|
| **COMPRA** | En juego (RVOL ≥ 2), sobre VWAP, rompió el máximo de apertura **y el máximo previo** (nunca compra debajo de un techo que ya falló), con impulso (escalera, ballena compradora o volumen acelerando), a ≤ 4 % del VWAP, stop ≤ 2.5 %, fuerza ≥ 60 (65 con mercado amarillo), antes de las 15:30. Trae entrada, stop, +2 % y +5 %. |
| **ESPERA** | En juego pero falta una pieza: pre-market o antes de 9:35, extendido, debajo del máximo de apertura o del máximo del día, ballenas vendiendo, mercado en rojo, halt en curso o stop lejos. Dice qué tiene que pasar y, si es una ruptura, el nivel (`level`) y la orden lista para armar. |
| **NO COMPRES** | Dilución, spread caro, < US$1, sin volumen, debajo del VWAP, parabólico (> 10 % sobre VWAP), rango diario pequeño, después de 15:30. |

**Telegram:** 📋 lista de apertura (9:15 ET, gaps con su máximo pre-market), 🟡 ARMA (ruptura a ≤ 1 % del gatillo con fuerza ≥ 55, 60 con mercado amarillo: orden de compra stop con su stop y +2 %, una por ticker y día, máx. 8), ⚡ rompe (el vigía rápido vio cruzar el gatillo), 🟢 COMPRA (una por ticker y día), ✅ +2 % (sube el stop a la entrada), 🎯 +5 %, 🛑 stop, ⏰ cierre a las 15:50 y resumen del día. Se activa al poner `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` en Render → radar-semaforo → Environment. La página muestra cuántas señales tocaron +2 % antes del stop: esa es la tasa de acierto real que hay que mirar antes de subir el tamaño.

Todos los umbrales están arriba de `live/decide.py`. Pruebas sin red: `python -m tests.test_live` y `python tests/test_bridge.py`.

**Velocidad (oct-2026):** con datos de Yahoo (1–2 min de retraso) el ciclo de 1 min llegaba tarde a las rupturas: de las 6 entradas del 1-oct, 4 nunca subieron más de +0.35 % a favor. Ahora: (1) el rango de apertura es de 5 min, así que las rupturas cuentan desde las 9:35; (2) cada ruptura en ESPERA cercana se **arma**: el aviso 🟡 trae la orden de compra stop para dejarla puesta en el bróker, que se ejecuta en el instante del cruce sin depender del retraso de Yahoo; (3) el vigía rápido revisa esas acciones cada 15 s y, al cruzar, avisa ⚡ y despierta el ciclo completo para confirmar la COMPRA sin esperar su turno; (4) el universo se refresca cada 3 min (antes 10). Costo honesto: entrar antes es entrar con menos confirmación; una orden armada también se llena en rupturas falsas. Variables: `OR_MINUTES` (5), `FAST_S` (15), `ARM_MIN` (55), `ARM_NEAR` (1.0 %), `ARM_MAX` (8), `UNIVERSE_TTL` (180 s).

**Fuerza (oct-2026):** RVOL, noticia y calls inusuales se topan juntos en 35 puntos (son el mismo evento visto tres veces), y la subida del día se mide en rangos diarios (ATR): hasta 2 rangos cuenta completa, desde 4 ya no suma y cada rango extra resta 2.5 (máx. 10). ACN el 1-oct (+23 % = 5.9 rangos diarios) pasó de 99 a 55. Una noticia solo cuenta si el titular menciona el ticker o el nombre de la empresa (los resúmenes tipo *top analyst calls* ya no puntúan). La página solo muestra "Mejor opción" cuando hay COMPRA; si no, muestra qué está vigilando y avisa si los datos tienen más de 3 min.

**Fiabilidad y medición (1-oct-2026, noche):**
- **Memoria.** El 1-oct a las 10:18 Render reinició el servicio por quedarse sin memoria (512 MB del plan Starter): venía al ~95 % desde el día anterior. Causa: `yf.download` usa `multitasking`, que guarda en una lista global cada hilo terminado (uno por ticker y descarga: decenas de miles por día). Ahora `scanner.sources.yahoo.prune_tasks()` los suelta después de cada descarga, y tras cada ciclo se recolecta basura y se devuelve memoria al sistema (`malloc_trim`), con menos arenas de malloc (`MALLOC_ARENAS`, 2). `/health` y el pie de la página muestran la memoria; Telegram avisa una vez por día si pasa de `MEM_ALERT_PCT` (85 %).
- **Señales abiertas fuera del universo.** Una COMPRA (o una armada ya activada) se sigue hasta su stop u objetivo aunque la acción salga de las pantallas. Antes, SITC (COMPRA a las 11:17 del 1-oct) dejó de seguirse a los 6 minutos y nunca llegaron sus avisos.
- **Rupturas armadas con aviso de cancelar, y medidas.** Cada aviso 🟡 ARMA crea una compra stop virtual (entrada, límite +0.3 %, stop, +2 %, +5 %): 📥 si se activa; ❌, ⚠ o ⌛ para cancelarla si pierde el stop sin romper, el semáforo la pasa a NO COMPRES, salta por encima del límite o se acaba la hora de entradas (así una orden puesta no se llena tarde, con la jugada ya dañada). Sus resultados van aparte (`armadas` en `/api/trades`, en `data/live/AAAA-MM-DD.json` y en `summary.json`) para comparar entrar en la ruptura contra esperar la COMPRA confirmada.
- **Retraso medido.** Cada COMPRA guarda `lag_min` (minutos desde que rompió el nivel, según las velas) y `slip` (% de la entrada sobre el nivel); el aviso 🟢 lo dice y la página muestra el promedio del día y del acumulado.
- **Avisos por día.** Al cambiar de día se vacían siempre; antes, sin COMPRA el día anterior, un ARMA de ayer bloqueaba el de hoy para la misma acción.

**Auditoría (2-oct-2026): correcciones.** (1) Si Yahoo no entrega velas (límite de la IP), la acción conserva el precio de la cotización y el NO "por falta de datos" ya no cancela las compras stop armadas; tampoco las cancela un NO si el precio ya está sobre la entrada (pudo activarse y las velas aún no lo muestran). Una descarga que vuelve vacía se pide una vez más. (2) Las últimas 2 velas de Yahoo llegan a medio llenar: se revisan otra vez completas en el ciclo siguiente (antes un stop o un +2 % tocado en la segunda mitad de ese minuto no se avisaba) y el seguimiento de cada señal empieza en la última vela que vio, no en la hora del reloj. En la vela en que se llena una límite no cuenta el +2 % (su máximo suele ser de antes de llenarse). (3) El rango de apertura se cierra cuando llega la vela de las 9:35, no por el reloj; si la acción abre tarde (halt), el rango empieza en su primera vela (antes el nivel salía NaN). (4) La curva de volumen o el cierre previo que fallaron se vuelven a pedir cada 5 min (antes la acción quedaba en NO todo el día). (5) Halts, refresco del universo, noticias, SEC y opciones corren en un hilo aparte: el ciclo de 1 min ya no los espera (`cycle_s` en `/health` mide cuánto tarda). (6) Si las cotizaciones v7 fallan, descansan 1, 2, 5 y luego 20 min (antes 20 al primer fallo). (7) RVOL sin la vela a medio llenar y RVOL de los últimos 15 min (`rvol15`), que solo decide con `RVOL15_IN_PLAY` > 0 (apagado hasta medir si ayuda). Un parabólico con noticia fresca y RVOL ≥ 5 queda en ESPERA del retroceso en vez de NO. La ballena cuenta desde la vela 6 de la sesión. (8) Seguridad: `/api/news/{t}` pide `X-Token` (`POSITIONS_TOKEN`), `/api/replay?fresh=1` recalcula como mucho cada 10 min, el puente rechaza cuerpos grandes por la cabecera y el aviso ⚡ se manda fuera del candado (el puente no espera a Telegram); el registro de Render no guarda precios de IBKR.

**Puente IBKR (opcional, 1-oct-2026): escáneres y precio al instante.** `bridge/puente_ibkr.py` corre en la PC (Windows) conectado a TWS o IB Gateway en **solo lectura** (no tiene código para operar) y le manda al semáforo, por `POST /api/bridge` con la cabecera `X-Token: $BRIDGE_TOKEN`:
- **Escáneres de IBKR cada 30 s** (`TOP_PERC_GAIN`, `HOT_BY_VOLUME`, `TOP_TRADE_RATE`; precio ≥ US$1 y volumen ≥ 100 k). En sesión, los primeros 12 (`IBKR_TOP`) entran al universo en el siguiente ciclo, sin esperar el refresco de 3 min ni que Yahoo los liste; en pre-market solo compiten en el ranking. La página les pone la etiqueta IBKR.
- **Precio al instante de las acciones armadas** (streaming; no usa consultas sueltas, que cuestan US$0.01 cada una). Al cruzar el gatillo, el aviso ⚡ sale en ~1 s y dice "IBKR" (con Yahoo eran hasta 15 s más su retraso de 1–2 min). La armada guarda `break_t` y `break_src` (hora y fuente de la ruptura, sin el precio) para medir cuánto adelanta.
- Sin puente todo sigue igual con Yahoo. `/health` (`bridge`) y el pie de la página dicen si está conectado y el último error de IBKR. Los precios de IBKR no se publican en la página ni en la API (son para uso personal).

Instalación (una vez):
1. **IBKR (Client Portal → Settings → Market Data Subscriptions):** estado no profesional, *US Securities Snapshot and Futures Value Bundle* (US$10/mes, gratis el mes que las comisiones llegan a US$30) + *US Equity and Options Add-On Streaming Bundle* (US$4.50/mes) y el *Market Data API Acknowledgement* firmado.
2. **TWS o IB Gateway abierto con tu usuario.** TWS: Global Configuration → API → Settings → *Enable ActiveX and Socket Clients*, *Read-Only API* marcado, puerto 7496. IB Gateway: Configure → Settings → API → Settings, *Read-Only API* marcado, puerto 4001. En *Lock and Exit*, *Auto restart* (una vez por semana pide entrar de nuevo con 2FA). Un usuario solo puede estar abierto en una plataforma: si abres IBKR en el celular con el mismo usuario, el puente se queda sin datos (error 10197). Para operar desde el celular con el puente encendido hace falta un segundo usuario, que paga sus propios datos.
3. **Python 3.10+ en Windows.** En la carpeta `bridge/`, doble clic en `instalar.bat`: instala `ib_async` y crea `puente.env` con una clave nueva. Copia esa clave en Render → radar-semaforo → Environment → `BRIDGE_TOKEN`. Con TWS, cambia `IB_PORT=7496` en `puente.env`.
4. **Cada día de mercado:** doble clic en `iniciar_puente.bat` antes de las 9:30 ET y déjalo abierto. Pruebas sin TWS: `python tests/test_bridge.py` (IBKR falso y el servidor real en memoria).

**Posiciones reales (avisos de caída y objetivo):** el semáforo también vigila lo que de verdad compraste. Quien ejecuta la orden registra la posición con `POST /api/positions` (cabecera `X-Token: $POSITIONS_TOKEN`, cuerpo `{"t":"RUN","entry":10.41,"qty":30,"tp":3}`) y la quita con `DELETE /api/positions/RUN` al vender. Cada ciclo compara el último precio con tu entrada y avisa por Telegram, una sola vez por nivel: 📉 al caer 2 % y 3 % (`DROP_ALERTS`, coma) y 🎯 al llegar al objetivo `tp` (por defecto `DEFAULT_TP=3`). Solo vigila y avisa; no ejecuta nada. Sin `POSITIONS_TOKEN` en Render el acceso queda cerrado. Las posiciones viven en el disco del servicio: un redeploy las borra, así que hay que volver a registrar las abiertas (`GET /api/positions` muestra cuáles hay). Solo se vigila con el mercado abierto (4:00–16:00 ET) y con el retraso de 1–2 min de Yahoo.

**Render (instalado):** servicio `radar-semaforo`, plan Starter (no duerme, 512 MB), Oregón (la configuración que manda es la del panel de Render; `render.yaml` la documenta). Variables: `SEC_USER_AGENT`, `CYCLE_S=60`, `UNIVERSE_N=50`, `RADAR_SCAN_URL`, opcional `WATCHLIST` (coma), las de Telegram y, para el puente IBKR, `BRIDGE_TOKEN` (y opcional `IBKR_TOP`, 12). En plan free el servicio se duerme sin tráfico: `live/keepalive.py` se llama a sí mismo de 4:00 a 16:30 ET y `.github/workflows/keepalive.yml` lo despierta cada 15 min. Con Starter (US$7/mes, 5× CPU, no duerme) sube a `CYCLE_S=60` y `UNIVERSE_N=50`. Los cambios en `data/`, `tests/`, `tools/` y este README no redeployan.

**Límites de datos (Yahoo gratis):** 1–2 min de retraso (con el puente IBKR conectado, las rupturas armadas se ven al instante y los escáneres de IBKR alimentan el universo; el resto del análisis sigue con las velas de Yahoo); a ratos Yahoo niega la cotización v7 (401) y entonces el precio sale de las velas, no hay bid/ask y el vigía rápido espera; cuando sí responde, a veces trae bid/ask viejos (MRVL o AMD con 10 % de spread en plena sesión), así que un spread que no cuadra con el último precio o es mucho más ancho que el rango típico de 1 min se descarta en vez de vetar la acción; el volumen pre-market a veces viene en 0.

Local: `pip install -r requirements-live.txt && uvicorn live.app:app --port 8000`.

## Local

```bash
pip install -r requirements.txt
python -m scanner.scan --horizon A              # real (necesita internet)
python -m scanner.scan --horizon A --fixtures x --no-notify   # datos sintéticos, sin red
python tools/parity.py                          # motor Python = motor JS
```

`scanner/engine.py` sirve tal cual para tu screener en FastAPI: `evaluate(st)` recibe el mismo diccionario de entradas que la página.

Herramienta de análisis personal. No es asesoría financiera.
