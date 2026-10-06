@echo off
rem Diagnostico de SOLO LECTURA: deja el resultado en diagnostico_paper.txt y lo muestra. No abre ni cierra nada ni cambia nada.
rem (Que ejecutores e IB Gateway hay abiertos, si la tarea de Windows existe y como esta la suspension de la PC.)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0diagnostico_paper.ps1"
echo.
type "%~dp0diagnostico_paper.txt"
echo.
echo (Esta ventana se cierra sola en 30 segundos.)
timeout /t 30 >nul
