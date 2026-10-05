# Sale con codigo 1 si hay un proceso cuya linea de comando coincide con -Patron (expresion regular) y con 0 si no.
# Sirve para saber si ya hay un ejecutor corriendo (python con ejecutor_paper.py / ejecutor_claude.py, o su ventana de
# reinicio automatico ejecutor.bat) y si IB Gateway ya esta abierto (java con ibcalpha.ibc.IbcGateway).
# Es rapido: mira los procesos, no las ventanas. Si no puede mirar, sale con 0 (se abre lo que haga falta).
param([string]$Patron = 'ejecutor_paper\.py')
try {
  $yo = $PID
  $hay = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
      $_.ProcessId -ne $yo -and $_.CommandLine -and $_.Name -notmatch '^(powershell|pwsh|wscript)\.exe$' -and ($_.CommandLine -match $Patron) })
  if ($hay.Count -gt 0) { exit 1 } else { exit 0 }
} catch {
  exit 0
}
