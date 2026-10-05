# Crea la tarea de Windows "Semaforo Paper": de lunes a viernes, desde las 8:45 (hora de tu PC) y cada 5 minutos hasta las
# 17:15, abre vigilante_paper.vbs, que ejecuta ARRANCAR_PAPER.bat /silencioso sin ventana: abre IB Gateway paper y los dos
# ejecutores SOLO si alguno no esta abierto (si todo esta en pie, no hace nada). La ventana llega hasta las 17:15 para que
# cubra el mercado tambien con el horario de invierno de EE. UU. (a partir del 1-nov-2026 abre a las 10:30 de tu hora).
# Corre con tu usuario y solo con tu sesion abierta (no guarda ninguna clave). Si la PC esta dormida a las 8:45, la despierta.
$ErrorActionPreference = 'Stop'
try {
  $carpeta = Join-Path $env:USERPROFILE 'Claude'
  $vbs = Join-Path $carpeta 'vigilante_paper.vbs'
  if (-not (Test-Path -LiteralPath $vbs)) { Write-Host "No encuentro $vbs"; exit 1 }
  $accion  = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('"' + $vbs + '"') -WorkingDirectory $carpeta
  $disparo = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '08:45'
  $rep = (New-ScheduledTaskTrigger -Once -At '08:45' -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Hours 8 -Minutes 30)).Repetition
  $disparo.Repetition = $rep
  $ajustes = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
  $quien   = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName 'Semaforo Paper' -Action $accion -Trigger $disparo -Settings $ajustes -Principal $quien -Description 'Vigila que IB Gateway paper y los dos ejecutores (el tuyo y el de Claude) esten abiertos, lun-vie 8:45-17:15, cada 5 min' -Force | Out-Null
  $sig = (Get-ScheduledTaskInfo -TaskName 'Semaforo Paper').NextRunTime
  Write-Host "Tarea 'Semaforo Paper' creada. Proxima ejecucion: $sig"
  exit 0
} catch {
  Write-Host ("No pude crear la tarea: " + $_.Exception.Message)
  Write-Host "Prueba con clic derecho en INSTALAR_ARRANQUE_DIARIO.bat > Ejecutar como administrador."
  exit 1
}
