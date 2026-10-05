# Sale con codigo 1 si ARRANCAR_PAPER.bat lanzo IB Gateway hace menos de 4 minutos (todavia esta entrando) y con 0 si no.
$f = Join-Path $PSScriptRoot 'gateway_lanzado.txt'
try {
  if ((Test-Path -LiteralPath $f) -and (((Get-Date) - (Get-Item -LiteralPath $f).LastWriteTime).TotalSeconds -lt 240)) { exit 1 }
} catch { }
exit 0
