# Sale con codigo 1 si el semaforo (Render) vio hablar al ejecutor hace menos de 90 segundos, y con 0 si no lo vio o no pudo
# preguntar. Es una segunda opinion para ARRANCAR_PAPER.bat: si el detector de procesos fallara y dijera "no esta abierto"
# cuando si lo esta, el semaforo (que recibe lo que dice cada ejecutor cada pocos segundos) lo desmiente y no se abre otra copia.
# Solo lee /health (publico, sin clave). Uso: render_vivo.ps1 -Que paper   o   render_vivo.ps1 -Que claude
param([string]$Que = 'paper', [string]$Url = 'https://radar-semaforo.onrender.com/health')
try {
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  $r = Invoke-RestMethod -Uri $Url -TimeoutSec 10 -ErrorAction Stop
  $x = $r.$Que
  if ($x -and $null -ne $x.visto_s -and ([double]$x.visto_s) -lt 90) { exit 1 } else { exit 0 }
} catch {
  exit 0
}
