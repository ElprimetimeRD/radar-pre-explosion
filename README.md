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

`live/` es un servicio web en Render que corre toda la sesión (pre-market cada 3 min desde las 4:00 ET; cada 90 s de 9:30 a 16:00) y responde una sola pregunta: **¿qué compro ahora, si algo?**

**Cada ciclo:** universo dinámico (subidas > 3 %, más activas, small caps en alza, tu watchlist, halts de NASDAQ y el escaneo de la mañana vía `RADAR_SCAN_URL`; en pre-market se ordena por el gap real de las velas de 5 min) → las 35–50 más activas → velas de 1 min → VWAP, rango de apertura (9:30–9:45), RVOL a la misma hora vs. los 4 días previos, velas ballena (≥ 5× la mediana y ≥ US$150 k en un minuto, clasificadas compra/venta), mínimos crecientes (escalera), extensión sobre VWAP. A las 20 más fuertes se les añade noticias clasificadas, dilución en EDGAR (424B/S-1 ≤ 30 días) y calls inusuales. Mercado: SPY y QQQ contra su VWAP.

| Decisión | Cuándo |
|---|---|
| **COMPRA** | En juego (RVOL ≥ 2), sobre VWAP, rompió el máximo de apertura, con impulso (escalera, ballena compradora o volumen acelerando), a ≤ 4 % del VWAP, stop ≤ 2.5 %, fuerza ≥ 60 (70 con mercado amarillo), antes de las 15:30. Trae entrada, stop, +2 % y +5 %. |
| **ESPERA** | En juego pero falta una pieza: pre-market o antes de 9:45, extendido, debajo del máximo de apertura, ballenas vendiendo, mercado en rojo, halt en curso o stop lejos. Dice qué tiene que pasar. |
| **NO COMPRES** | Dilución, spread caro, < US$1, sin volumen, debajo del VWAP, parabólico (> 10 % sobre VWAP), rango diario pequeño, después de 15:30. |

**Telegram:** 🟢 COMPRA (una por ticker y día), ✅ +2 % (sube el stop a la entrada), 🎯 +5 %, 🛑 stop, ⏰ cierre a las 15:50 y resumen del día. Se activa al poner `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` en Render → radar-semaforo → Environment. La página muestra cuántas señales tocaron +2 % antes del stop: esa es la tasa de acierto real que hay que mirar antes de subir el tamaño.

Todos los umbrales están arriba de `live/decide.py`. Pruebas sin red: `python -m tests.test_live`.

**Posiciones reales (avisos de caída y objetivo):** el semáforo también vigila lo que de verdad compraste. Quien ejecuta la orden registra la posición con `POST /api/positions` (cabecera `X-Token: $POSITIONS_TOKEN`, cuerpo `{"t":"RUN","entry":10.41,"qty":30,"tp":3}`) y la quita con `DELETE /api/positions/RUN` al vender. Cada ciclo compara el último precio con tu entrada y avisa por Telegram, una sola vez por nivel: 📉 al caer 2 % y 3 % (`DROP_ALERTS`, coma) y 🎯 al llegar al objetivo `tp` (por defecto `DEFAULT_TP=3`). Solo vigila y avisa; no ejecuta nada. Sin `POSITIONS_TOKEN` en Render el acceso queda cerrado. Las posiciones viven en el disco del servicio: un redeploy las borra, así que hay que volver a registrar las abiertas (`GET /api/positions` muestra cuáles hay). Solo se vigila con el mercado abierto (4:00–16:00 ET) y con el retraso de 1–2 min de Yahoo.

**Render (instalado):** servicio `radar-semaforo`, plan free, Oregon. Variables: `SEC_USER_AGENT`, `CYCLE_S=90`, `UNIVERSE_N=35`, `RADAR_SCAN_URL`, opcional `WATCHLIST` (coma) y las de Telegram. En plan free el servicio se duerme sin tráfico: `live/keepalive.py` se llama a sí mismo de 4:00 a 16:30 ET y `.github/workflows/keepalive.yml` lo despierta cada 15 min. Con Starter (US$7/mes, 5× CPU, no duerme) sube a `CYCLE_S=60` y `UNIVERSE_N=50`. Los cambios en `data/`, `tests/`, `tools/` y este README no redeployan.

**Límites de datos (Yahoo gratis):** 1–2 min de retraso; desde Render Yahoo niega la cotización v7 (401), así que el precio sale de las velas y no hay bid/ask (sin filtro de spread); el volumen pre-market a veces viene en 0.

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
