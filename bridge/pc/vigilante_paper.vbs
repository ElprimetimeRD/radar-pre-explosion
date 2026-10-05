' Abre ARRANCAR_PAPER.bat /silencioso sin ventana. Lo usa la tarea de Windows "Semaforo Paper" (INSTALAR_ARRANQUE_DIARIO.bat).
Set sh = CreateObject("WScript.Shell")
carpeta = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\Claude"
sh.Run """" & carpeta & "\ARRANCAR_PAPER.bat"" /silencioso", 0, False
