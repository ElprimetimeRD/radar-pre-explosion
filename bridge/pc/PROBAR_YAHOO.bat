@echo off
rem Prueba que esta PC puede leer los datos de Yahoo que usa el ejecutor de Claude (sirve tambien de noche y en fin de
rem semana: Yahoo devuelve la ultima sesion). Deja el resultado en probar_yahoo.txt. No toca ninguna orden ni conexion.
cd /d "%USERPROFILE%\Claude\radar-puente"
set PY=python
where py >nul 2>nul && set PY=py -3
echo Probando Yahoo (unos segundos)...
%PY% datos_yahoo.py --probar > "%USERPROFILE%\Claude\probar_yahoo.txt" 2>&1
type "%USERPROFILE%\Claude\probar_yahoo.txt"
if /i "%~1"=="/sinpausa" exit /b 0
echo.
pause
