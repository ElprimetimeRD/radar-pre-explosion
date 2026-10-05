@echo off
rem Abre un ejecutor de ordenes en la cuenta PAPER de IBKR y lo vuelve a abrir solo si se cae.
rem   ejecutor.bat paper    el tuyo (ejecutor_paper.py, ordenes sem-...)
rem   ejecutor.bat claude   el de Claude (ejecutor_claude.py, ordenes cla-...)
rem Solo IB Gateway paper (puerto 4002, cuentas DU). Normalmente lo abre ARRANCAR_PAPER.bat. Para detenerlo: cierra esta
rem ventana (o Ctrl+C y responde S); con Ctrl+C el ejecutor sale ordenado y cancela las compras que no se llenaron.
cd /d "%~dp0"
set QUE=%~1
if "%QUE%"=="" set QUE=paper
if not exist "ejecutor_%QUE%.py" (
  echo No encuentro ejecutor_%QUE%.py en esta carpeta.
  pause
  exit /b 1
)
set PY=python
where py >nul 2>nul && set PY=py -3
title Ejecutor %QUE%
:otra
%PY% ejecutor_%QUE%.py
set RC=%errorlevel%
rem 0 = lo cerraste tu con Ctrl+C; 2 = falta la clave o la libreria (reintentar no sirve)
if "%RC%"=="0" goto fin
if "%RC%"=="2" goto fin
rem 3 = ya hay otra copia de este ejecutor abierta: esta ventana se cierra sola y la otra sigue como estaba
if "%RC%"=="3" exit
>>"ejecutor_%QUE%.log" echo %time:~0,8% El programa se cerro (codigo %RC%); lo reabro en 15 segundos.
ping -n 16 127.0.0.1 >nul
goto otra
:fin
pause
