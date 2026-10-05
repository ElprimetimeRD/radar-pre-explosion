@echo off
rem Marcador acumulado: tu ejecutor (semaforo) contra el de Claude, desde sus diarios (diario_sem.csv y diario_cla.csv).
rem Solo lee los diarios. Deja el resultado tambien en radar-puente\comparacion.txt. Con "COMPARAR.bat --dias 5" mira los ultimos 5 dias.
chcp 65001 >nul
cd /d "%USERPROFILE%\Claude\radar-puente"
set PY=python
where py >nul 2>nul && set PY=py -3
%PY% comparar.py %*
echo.
pause
