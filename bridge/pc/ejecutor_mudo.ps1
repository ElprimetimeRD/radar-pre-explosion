# Sale con codigo 1 si el semaforo (Render) lleva mas de 4 minutos sin ver hablar a ese ejecutor en horario de mercado
# (dia de semana, 9:36 a 15:50 ET) aunque su ventana siga abierta: un ejecutor colgado. Lo llama ARRANCAR_PAPER.bat para
# cerrarlo y abrirlo de nuevo. Caso real (6-oct): ambos quedaron mudos 80-90 minutos con la ventana abierta.
# Solo mira /health (publico). Con Render inalcanzable, sin dato de visto_s, fuera de horario, o si ya se reinicio hace menos de
# 10 minutos (mudo_reinicio.txt), sale con 0 (no hace nada). Uso: ejecutor_mudo.ps1 -Que paper   o   -Que claude
param([string]$Que = 'paper', [string]$Url = 'https://radar-semaforo.onrender.com/health')
try {
  $now = Get-Date
  $min = $now.Hour * 60 + $now.Minute
  if ($now.DayOfWeek -eq 'Saturday' -or $now.DayOfWeek -eq 'Sunday') { exit 0 }
  if ($min -lt 576 -or $min -gt 950) { exit 0 }
  $m = Join-Path $PSScriptRoot 'mudo_reinicio.txt'
  if ((Test-Path -LiteralPath $m) -and (($now - (Get-Item -LiteralPath $m).LastWriteTime).TotalSeconds -lt 600)) { exit 0 }
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  $r = Invoke-RestMethod -Uri $Url -TimeoutSec 10 -ErrorAction Stop
  $x = $r.$Que
  if (-not $x -or $null -eq $x.visto_s) { exit 0 }
  if (([double]$x.visto_s) -gt 240) {
    Set-Content -LiteralPath $m -Value ($now.ToString('s') + ' ' + $Que) -ErrorAction Stop
    exit 1
  }
  exit 0
} catch { exit 0 }
