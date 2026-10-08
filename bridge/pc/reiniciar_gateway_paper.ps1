# Reinicia en frio SOLO el IB Gateway PAPER (el programa que escucha en el puerto 4002) cuando se queda pegado sin conexion
# con IBKR. Caso real (5 y 6-oct): tras el corte nocturno de IBKR (00:13 ET) el Gateway se queda en el aviso "Unrecognized
# Username or Password"; la API sigue aceptando conexiones pero nada llega a IBKR (posiciones, simbolos ni ordenes).
# Lo llama ARRANCAR_PAPER.bat cuando existe reiniciar_gateway.flag:
#   1) borra la ficha de reinicio automatico (autorestart) de ESA sesion, para que el proximo arranque entre con la clave
#      del .ini paper (como el arranque de cada lunes) y no con la ficha que IBKR rechazo;
#   2) cierra la ventana de IBC que lo lanzo (para que IBC no lo reabra por su cuenta) y el propio Gateway;
#   3) espera a que suelte el puerto 4002. ARRANCAR_PAPER.bat lo vuelve a abrir en la misma pasada.
# No toca nada que no sea el Gateway del puerto 4002 (la cuenta real usa el 4001). Las ordenes con stop siguen en IBKR.
$ErrorActionPreference = 'SilentlyContinue'
function Log([string]$m) { Write-Output ("{0} reiniciar_gateway: {1}" -f (Get-Date -Format 'ddd MM/dd/yyyy HH:mm:ss.ff'), $m) }

function Escuchan4002 {
  $p = @()
  try { $p = @(Get-NetTCPConnection -LocalPort 4002 -State Listen -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique) } catch { }
  if (-not $p) {
    $p = @(netstat -ano | Select-String -Pattern ':4002\s+\S+\s+LISTENING\s+(\d+)' |
           ForEach-Object { [int]$_.Matches[0].Groups[1].Value } | Select-Object -Unique)
  }
  return @($p | Where-Object { $_ -gt 0 })
}

$pids = Escuchan4002
if (-not $pids) { Log 'nadie escucha en el puerto 4002: no hay Gateway paper que cerrar'; exit 0 }
foreach ($p in $pids) {
  $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$p"
  $cl = [string]$proc.CommandLine
  if ($cl -notmatch 'ibcalpha\.ibc\.IbcGateway|ibgateway') {
    Log ("el programa del puerto 4002 (PID {0}, {1}) no parece el IB Gateway: no lo toco" -f $p, $proc.Name)
    continue
  }
  if ($cl -match '-Drestart=("?)([^"\s]+)\1') {
    $f = Join-Path (Join-Path "$env:SystemDrive\Jts" $Matches[2]) 'autorestart'
    if (Test-Path -LiteralPath $f) {
      Remove-Item -LiteralPath $f -Force
      if (Test-Path -LiteralPath $f) { Log "no pude borrar la ficha $f" } else { Log "borrada la ficha de reinicio $f (entrara con la clave del .ini)" }
    }
  }
  $padre = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $proc.ParentProcessId)
  if ($padre -and $padre.Name -match '^cmd\.exe$' -and [string]$padre.CommandLine -match 'IBC|DisplayBannerAndLaunch|StartIBC') {
    Stop-Process -Id $padre.ProcessId -Force
    Log ("cerrada la ventana de IBC (PID {0})" -f $padre.ProcessId)
  }
  Stop-Process -Id $p -Force
  Log ("cerrado el IB Gateway paper (PID {0})" -f $p)
}
$libre = $false
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Seconds 1
  if (-not (Escuchan4002)) { $libre = $true; break }
}
Start-Sleep -Seconds 5
$otro = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^javaw?\.exe$' -and [string]$_.CommandLine -match 'ibcalpha\.ibc\.IbcGateway' })
Log ("puerto 4002 libre: {0}; Gateway de IBC todavia abierto: {1}" -f $libre, ($otro.Count -gt 0))
exit 0
