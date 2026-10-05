@echo off
setlocal
rem UNA SOLA VEZ. Instala el vigilante de lunes a viernes y pone en marcha los dos ejecutores con la version nueva.
echo.
echo  Esto hace tres cosas, todas SOLO con la cuenta PAPER (la real no se toca):
echo.
echo   1) Crea la tarea de Windows "Semaforo Paper": de lunes a viernes, desde las 8:45 y cada 5 minutos hasta las 17:15,
echo      revisa que IB Gateway paper y los dos ejecutores esten abiertos y abre lo que falte (sin ventanas).
echo   2) Cierra tu ejecutor y lo vuelve a abrir con la version nueva, y abre el ejecutor de Claude. Las ordenes que
echo      hubiera puestas en IBKR siguen ahi con sus stops: cada ejecutor las reconoce al volver.
echo   3) Prueba que esta PC puede leer los datos de Yahoo que usa Claude (resultado en probar_yahoo.txt).
echo.
echo  La PC debe estar prendida, con tu sesion de Windows abierta y sin dormirse en horario de mercado (si es laptop,
echo  no cierres la tapa). Para quitar la tarea: schtasks /delete /tn "Semaforo Paper" /f
echo.
echo  Si no lo quieres, cierra esta ventana. Para seguir, pulsa una tecla.
pause >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tarea_paper.ps1"
echo.
call "%~dp0ARRANCAR_PAPER.bat" /reiniciar
echo.
call "%~dp0PROBAR_YAHOO.bat" /sinpausa
echo.
echo  Listo. Escribe /estado y /claude al bot de Telegram en 1 o 2 minutos: los dos deben decir "Ejecutor conectado".
pause
