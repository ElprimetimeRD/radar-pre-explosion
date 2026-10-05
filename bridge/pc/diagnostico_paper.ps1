# Diagnostico de SOLO LECTURA: deja diagnostico_paper.txt (en esta carpeta) con lo que hay abierto, si la tarea de Windows
# "Semaforo Paper" existe, y como esta configurada la suspension de la PC. No abre ni cierra nada y no cambia ninguna opcion.
# No imprime lineas de comando de los procesos (podrian llevar claves): solo nombre, PID y hora de inicio.
$ErrorActionPreference = 'SilentlyContinue'
$salida = Join-Path $PSScriptRoot 'diagnostico_paper.txt'
$L = New-Object System.Collections.Generic.List[string]
function Add-Linea([string]$t) { [void]$L.Add($t) }
Add-Linea ("Diagnostico del " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + "  (hora de esta PC)")
Add-Linea ""

# 1) Que esta abierto
Add-Linea "1) Procesos abiertos"
$todos = @(Get-CimInstance Win32_Process)
function Que($p) {
  $c = $p.CommandLine
  if (-not $c) { return $null }
  if ($p.Name -match '^(python|pythonw|py)\.exe$' -and $c -match 'ejecutor_paper\.py') { return 'ejecutor PAPER (programa)' }
  if ($p.Name -match '^(python|pythonw|py)\.exe$' -and $c -match 'ejecutor_claude\.py') { return 'ejecutor CLAUDE (programa)' }
  if ($p.Name -eq 'cmd.exe' -and $c -match 'ejecutor\.bat\W{1,3}paper') { return 'ejecutor PAPER (ventana de reinicio)' }
  if ($p.Name -eq 'cmd.exe' -and $c -match 'ejecutor\.bat\W{1,3}claude') { return 'ejecutor CLAUDE (ventana de reinicio)' }
  if ($p.Name -match '^(java|javaw)\.exe$' -and $c -match 'ibcalpha\.ibc\.IbcGateway') { return 'IB Gateway (IBC)' }
  return $null
}
$n = 0
foreach ($p in $todos) {
  $q = Que $p
  if ($q) {
    $n++
    $desde = ''
    if ($p.CreationDate) { $desde = $p.CreationDate.ToString('yyyy-MM-dd HH:mm:ss') }
    Add-Linea ("   {0,-38} PID {1,6}   desde {2}" -f $q, $p.ProcessId, $desde)
  }
}
if ($n -eq 0) { Add-Linea "   (ninguno)" }
Add-Linea "   Lo normal: 1 IB Gateway, 1 ejecutor PAPER (programa + ventana) y 1 ejecutor CLAUDE (programa + ventana). Si hay mas, hay duplicados."
Add-Linea ""

# 2) Puertos de IB Gateway
Add-Linea "2) Puertos de IB Gateway escuchando (4002 = paper, 4001 = cuenta real: no debe estar)"
$pt = @(Get-NetTCPConnection -State Listen -LocalPort 4001,4002 | Select-Object -ExpandProperty LocalPort -Unique)
if ($pt.Count -gt 0) { Add-Linea ("   " + ($pt -join ', ')) } else { Add-Linea "   (ninguno)" }
Add-Linea ""

# 3) Lo que dice ejecutor_activo.ps1 (el mismo control que usa ARRANCAR_PAPER.bat): 1 = lo detecta, 0 = no
Add-Linea "3) Detector de ARRANCAR_PAPER.bat (1 = ve el programa abierto, 0 = no lo ve)"
$act = Join-Path $PSScriptRoot 'ejecutor_activo.ps1'
$pruebas = @(
  @('IB Gateway', 'ibcalpha\.ibc\.IbcGateway'),
  @('ejecutor paper', 'ejecutor_paper\.py|ejecutor\.bat\W{1,3}paper'),
  @('ejecutor claude', 'ejecutor_claude\.py|ejecutor\.bat\W{1,3}claude'))
foreach ($t in $pruebas) {
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $act -Patron $t[1] | Out-Null
  Add-Linea ("   {0,-16} -> {1}" -f $t[0], $LASTEXITCODE)
}
Add-Linea "   Segunda opinion (render_vivo.ps1): el semaforo en Render ve al ejecutor hablando hace menos de 90 s (1 = si, 0 = no)"
$rv = Join-Path $PSScriptRoot 'render_vivo.ps1'
foreach ($q in @('paper', 'claude')) {
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $rv -Que $q | Out-Null
  Add-Linea ("   {0,-16} -> {1}" -f ('ejecutor ' + $q), $LASTEXITCODE)
}
$lk = @(Get-NetTCPConnection -State Listen -LocalPort 45041,45042 | Select-Object -ExpandProperty LocalPort -Unique)
$lkTxt = 'ninguno'
if ($lk.Count -gt 0) { $lkTxt = ($lk -join ', ') }
Add-Linea ("   Cerrojo de una sola copia (45041 = paper, 45042 = claude; solo lo tiene la version nueva del programa): " + $lkTxt)
Add-Linea ""

# 4) Tarea programada
Add-Linea "4) Tarea de Windows 'Semaforo Paper'"
$tarea = Get-ScheduledTask -TaskName 'Semaforo Paper'
if ($tarea) {
  $info = Get-ScheduledTaskInfo -TaskName 'Semaforo Paper'
  Add-Linea ("   Estado: {0} | ultima vez: {1} (resultado {2}) | proxima: {3}" -f $tarea.State, $info.LastRunTime, $info.LastTaskResult, $info.NextRunTime)
  foreach ($a in $tarea.Actions) { Add-Linea ("   Ejecuta: {0} {1}" -f $a.Execute, $a.Arguments) }
  foreach ($g in $tarea.Triggers) { Add-Linea ("   Disparador: empieza {0}, repite cada {1} durante {2}" -f $g.StartBoundary, $g.Repetition.Interval, $g.Repetition.Duration) }
  Add-Linea ("   Despierta la PC: {0} | corre con bateria: {1}" -f $tarea.Settings.WakeToRun, (-not $tarea.Settings.DisallowStartIfOnBatteries))
} else {
  Add-Linea "   NO existe (no se instalo, o fallo: vuelve a abrir INSTALAR_ARRANQUE_DIARIO.bat)"
}
Add-Linea ""

# 5) Suspension de la PC (solo se lee)
Add-Linea "5) Equipo y suspension (solo lectura)"
$cs = Get-CimInstance Win32_ComputerSystem
if ($cs) { Add-Linea ("   Equipo: {0} {1} | tipo {2} (2 = portatil)" -f $cs.Manufacturer, $cs.Model, $cs.PCSystemType) }
$bat = Get-CimInstance Win32_Battery
if ($bat) { Add-Linea ("   Bateria: presente (estado {0}, carga {1} %)" -f $bat.BatteryStatus, $bat.EstimatedChargeRemaining) } else { Add-Linea "   Bateria: no hay (PC de escritorio)" }
$esquema = (& powercfg.exe /getactivescheme) 2>$null
if ($esquema) { Add-Linea ("   " + $esquema) }
function Valor([string]$sub, [string]$set) {
  $o = (& powercfg.exe /query SCHEME_CURRENT $sub $set) 2>$null
  $ac = ($o | Select-String 'Current AC Power Setting Index|ndice de configuraci.n de energ.a de CA actual' | Select-Object -First 1)
  $dc = ($o | Select-String 'Current DC Power Setting Index|ndice de configuraci.n de energ.a de CC actual' | Select-Object -First 1)
  $f = { param($x) if ($x) { $v = ($x.ToString() -split ':')[-1].Trim(); try { [Convert]::ToInt32($v, 16) } catch { $v } } else { '?' } }
  return ("enchufada={0} bateria={1}" -f (& $f $ac), (& $f $dc))
}
Add-Linea ("   Suspender tras (segundos, 0 = nunca): " + (Valor 'SUB_SLEEP' 'STANDBYIDLE'))
Add-Linea ("   Al cerrar la tapa (0 nada, 1 suspender, 2 hibernar, 3 apagar): " + (Valor 'SUB_BUTTONS' 'LIDACTION'))
Add-Linea "   (Mientras un ejecutor esta abierto en horario de mercado, el programa pide a Windows que no se duerma; cerrar la tapa si puede suspenderla.)"
Add-Linea ""

# 6) Ultimas lineas de arranque_paper.log
Add-Linea "6) Ultimas lineas de arranque_paper.log"
$lg = Join-Path $PSScriptRoot 'arranque_paper.log'
if (Test-Path -LiteralPath $lg) { Get-Content -LiteralPath $lg -Tail 12 | ForEach-Object { Add-Linea ("   " + $_) } } else { Add-Linea "   (no existe)" }

[System.IO.File]::WriteAllLines($salida, $L, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ("Listo: " + $salida)
exit 0
