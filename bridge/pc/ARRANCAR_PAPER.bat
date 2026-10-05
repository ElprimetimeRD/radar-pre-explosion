@echo off
setlocal
rem =====================================================================================================================
rem  EJECUCION EN PAPER (dinero simulado). Doble clic: abre IB Gateway paper y los DOS ejecutores (el tuyo, del semaforo,
rem  y el de Claude). Es seguro repetirlo: solo abre lo que falte. La tarea de Windows "Semaforo Paper" lo repite sola cada
rem  5 minutos de lunes a viernes (se instala una vez con INSTALAR_ARRANQUE_DIARIO.bat).
rem    /silencioso  sin ventanas ni preguntas (lo usa la tarea de Windows)
rem    /reiniciar   cierra los ejecutores abiertos y los abre de nuevo (para cargar una version nueva; no toca el Gateway)
rem  - Solo toca la config PAPER (Documents\IBC\config-paper.ini). La cuenta real y su config no se tocan.
rem  - No arranca el puente de datos de la cuenta real (ARRANCAR_RADAR.bat): la paper tiene datos con 15 min de retraso
rem    y no deben llegar al semaforo real. Si el IB Gateway REAL esta abierto, cierralo antes para que no choquen.
rem  - IBC vuelve a marcar "Read-Only API" en cada arranque mientras el .ini diga ReadOnlyApi=yes, y asi IBKR rechaza
rem    las ordenes. Aqui se deja en "no" SOLO en el .ini paper, antes de abrir el Gateway (fijar_ini.ps1).
rem  - Cada ejecutor corre dentro de radar-puente\ejecutor.bat, que lo vuelve a abrir solo si se cae.
rem  - Apunta cada paso en arranque_paper.log, para saber que paso si algo no abre.
rem =====================================================================================================================
rem OJO: SHIFT (mas abajo) tambien mueve %0, y despues %~dp0 ya no es esta carpeta sino C:\ . Por eso la carpeta se guarda
rem en AQUI antes de leer los argumentos y todo lo demas usa %AQUI% (nunca %~dp0).
set "AQUI=%~dp0"
set "LOG=%AQUI%arranque_paper.log"
set "SILENCIO="
set "REINICIAR="
:args
if "%~1"=="" goto args_ok
if /i "%~1"=="/silencioso" set "SILENCIO=1"
if /i "%~1"=="/reiniciar" set "REINICIAR=1"
shift
goto args
:args_ok
rem Reinicio pedido a distancia: si existe reiniciar.flag, esta pasada se comporta como /reiniciar (y borra la marca para no repetirlo).
if exist "%AQUI%reiniciar.flag" set "REINICIAR=1"
if exist "%AQUI%reiniciar.flag" del "%AQUI%reiniciar.flag" >nul 2>&1
set "D=%USERPROFILE%\Documents\IBC"
set "INI=%D%\config-paper.ini"
set "PUENTE=%USERPROFILE%\Claude\radar-puente"
if defined SILENCIO (call :log "---- inicio (silencioso) ----") else (call :log "---- inicio ----")

if not defined REINICIAR goto sin_reinicio
powershell -NoProfile -ExecutionPolicy Bypass -File "%AQUI%parar_ejecutores.ps1" >>"%LOG%" 2>&1
call :log "Ejecutores cerrados para volver a abrirlos"
ping -n 4 127.0.0.1 >nul
:sin_reinicio

if not exist "%D%" mkdir "%D%"
if exist "%INI%" goto ini_ok
if defined SILENCIO (
  call :log "ERROR: falta %INI% (primera vez: abre ARRANCAR_PAPER.bat con doble clic, sin /silencioso)"
  exit /b 1
)
copy "%USERPROFILE%\Claude\IBC\config-plantilla.ini" "%INI%" >nul
echo Primera vez: en el archivo que se abre pon  IbLoginId=paperpriamo  e  IbPassword=tu clave paper,
echo TradingMode=paper  y guarda. La clave solo la escribes tu, aqui en tu PC.
start "" notepad "%INI%"
pause
:ini_ok

rem 1) IB Gateway paper. Si ya escucha en el puerto 4002, o ya esta abierto entrando, o se lanzo hace menos de 4 minutos,
rem    no abro otro (dos Gateways con la misma cuenta se pelean).
set "REAL="
for /f %%P in ('netstat -ano ^| findstr /r /c:":4001 .*LISTENING"') do set "REAL=1"
if defined REAL call :log "AVISO: el IB Gateway REAL esta abierto (puerto 4001)"
if defined REAL if not defined SILENCIO echo AVISO: el IB Gateway REAL esta abierto. Si el paper no conecta, cierra el real y repite esto.
set "PAPER="
for /f %%P in ('netstat -ano ^| findstr /r /c:":4002 .*LISTENING"') do set "PAPER=1"
if defined PAPER goto gw_escucha
powershell -NoProfile -ExecutionPolicy Bypass -File "%AQUI%ejecutor_activo.ps1" -Patron "ibcalpha\.ibc\.IbcGateway"
if "%errorlevel%"=="1" goto gw_entrando
powershell -NoProfile -ExecutionPolicy Bypass -File "%AQUI%gateway_reciente.ps1"
if "%errorlevel%"=="1" goto gw_entrando
powershell -NoProfile -ExecutionPolicy Bypass -File "%AQUI%fijar_ini.ps1" -Archivo "%INI%" -Clave ReadOnlyApi -Valor no >>"%LOG%" 2>&1
if errorlevel 1 goto ini_mal
call :log "Read-Only API en no: ok"
start "IBC PAPER" /min "%USERPROFILE%\Claude\IBC\StartGatewayPaper.bat"
>"%AQUI%gateway_lanzado.txt" echo %date% %time%
call :log "Gateway paper lanzado"
if not defined SILENCIO echo IB Gateway paper abierto: entra solo en 1 o 2 minutos.
goto gw_fin
:gw_escucha
call :log "Gateway paper ya escuchaba en 4002: no abro otro"
if not defined SILENCIO echo IB Gateway paper ya estaba abierto: no abro otro.
goto gw_fin
:gw_entrando
call :log "Gateway paper abierto pero aun sin escuchar (entrando): no abro otro"
if not defined SILENCIO echo IB Gateway paper esta abierto y entrando: no abro otro.
goto gw_fin
:ini_mal
call :log "ERROR: no pude quitar el Read-Only del .ini paper; no abro nada"
if defined SILENCIO exit /b 1
echo.
echo NO pude quitar el Read-Only del .ini paper: IBKR rechazaria las ordenes. No abro nada. Avisame con esta ventana a la vista.
timeout /t 60
exit /b 1
:gw_fin

rem 2) Los dos ejecutores (el que ya este corriendo no se vuelve a abrir). Reintentan solos hasta que el Gateway este listo.
call :ejecutor paper "Ejecutor paper (tuyo)"
call :ejecutor claude "Ejecutor de Claude"

call :log "---- fin ----"
if defined SILENCIO exit /b 0
echo.
echo Listo. En 1 o 2 minutos escribe /estado y /claude al bot de Telegram: los dos deben decir "Ejecutor conectado".
timeout /t 20 >nul
exit /b 0

:ejecutor
rem Tres seguros contra abrir una copia de mas: 1) el detector de procesos, 2) la segunda opinion del semaforo (si ve al
rem ejecutor hablando hace menos de 90 s, esta abierto aunque el detector no lo vea; no se consulta en /reiniciar, porque
rem ahi se acaba de cerrar a proposito) y 3) el propio programa, que se cierra si ya hay otra copia (codigo 3).
powershell -NoProfile -ExecutionPolicy Bypass -File "%AQUI%ejecutor_activo.ps1" -Patron "ejecutor_%~1\.py|ejecutor\.bat\W{1,3}%~1"
if "%errorlevel%"=="1" goto ej_ya
if defined REINICIAR goto ej_abrir
powershell -NoProfile -ExecutionPolicy Bypass -File "%AQUI%render_vivo.ps1" -Que %~1
if "%errorlevel%"=="1" goto ej_visto
:ej_abrir
start "%~2" /min "%PUENTE%\ejecutor.bat" %~1
call :log "Ejecutor %~1 lanzado"
if not defined SILENCIO echo Ejecutor %~1 abierto.
exit /b 0
:ej_visto
call :log "Ejecutor %~1: el detector no lo ve, pero el semaforo lo ve hablando: no abro otro"
if not defined SILENCIO echo Ejecutor %~1 ya estaba abierto ^(lo ve el semaforo^): no abro otro.
exit /b 0
:ej_ya
call :log "Ejecutor %~1 ya estaba abierto"
if not defined SILENCIO echo Ejecutor %~1 ya estaba abierto.
exit /b 0

:log
>>"%LOG%" echo %date% %time% %~1
exit /b 0
