@echo off
rem Ejecutor de ordenes en la cuenta PAPER de IBKR (dinero simulado). Solo IB Gateway paper (puerto 4002, cuentas DU).
rem Abrelo despues de ARRANCAR_PAPER.bat y dejalo abierto. Ctrl+C para detenerlo.
cd /d "%~dp0"
set PY=python
where py >nul 2>nul && set PY=py -3
%PY% ejecutor_paper.py
pause
