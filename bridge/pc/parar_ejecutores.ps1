# Cierra los ejecutores que estan abiertos (el de Priamo y el de Claude): primero sus ventanas de reinicio automatico
# (ejecutor.bat, y la ventana vieja ejecutor_paper.bat) y luego el programa. Las ordenes que ya estan puestas en IBKR
# siguen ahi con sus stops; al volver a abrirlos, cada ejecutor las reconoce por su referencia (sem-... / cla-...).
# No toca el Gateway ni el puente de datos. Solo se usa con ARRANCAR_PAPER.bat /reiniciar.
$ErrorActionPreference = 'SilentlyContinue'
$yo = $PID
$todos = @(Get-CimInstance Win32_Process)
$ventanas = @($todos | Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -match 'ejecutor\.bat\W{1,3}(paper|claude)|ejecutor_(paper|claude)\.bat' })
foreach ($p in $ventanas) { Stop-Process -Id $p.ProcessId -Force }
$progs = @($todos | Where-Object { $_.ProcessId -ne $yo -and $_.Name -match '^(python|pythonw|py)\.exe$' -and $_.CommandLine -match 'ejecutor_(paper|claude)\.py' })
foreach ($p in $progs) { Stop-Process -Id $p.ProcessId -Force }
Write-Host ("Cerrados: {0} ventanas y {1} programas de ejecutor" -f $ventanas.Count, $progs.Count)
exit 0
