# Sale con codigo 1 si toca el reinicio en frio DIARIO del IB Gateway paper: dia de semana, entre las 8:30 y las 9:20 (hora de
# la PC = ET) y aun no se hizo hoy. Lo llama ARRANCAR_PAPER.bat. Motivo (5 y 6-oct): tras el corte nocturno de IBKR (00:13 ET)
# el Gateway puede quedar pegado en "Unrecognized Username or Password" con la API abierta pero sin conexion con IBKR; empezar
# cada dia con un Gateway recien abierto (entra con la clave del .ini, como un lunes) evita pasar la sesion con ese estado.
# Con el Gateway cerrado no hace nada (ARRANCAR_PAPER.bat lo abre igual). Si algo falla, sale con 0 (no hace nada).
try {
  $now = Get-Date
  $min = $now.Hour * 60 + $now.Minute
  if ($now.DayOfWeek -eq 'Saturday' -or $now.DayOfWeek -eq 'Sunday') { exit 0 }
  if ($min -lt 510 -or $min -gt 560) { exit 0 }
  $f = Join-Path $PSScriptRoot 'gateway_diario.txt'
  $hoy = $now.ToString('yyyy-MM-dd')
  if ((Test-Path -LiteralPath $f) -and ((Get-Content -LiteralPath $f -ErrorAction Stop | Select-Object -First 1) -eq $hoy)) { exit 0 }
  Set-Content -LiteralPath $f -Value $hoy -ErrorAction Stop
  exit 1
} catch { exit 0 }
