@echo off
rem Puente IBKR -> semaforo (solo datos, sin ordenes). Ctrl+C para detenerlo.
cd /d "%~dp0"
set PY=python
where py >nul 2>nul && set PY=py -3
%PY% puente_ibkr.py
pause
