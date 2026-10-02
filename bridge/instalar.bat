@echo off
rem Instala lo que necesita el puente IBKR y crea la clave (puente.env).
cd /d "%~dp0"
set PY=python
where py >nul 2>nul && set PY=py -3
%PY% -m pip install -r requirements.txt
%PY% puente_ibkr.py --crear-clave
pause
