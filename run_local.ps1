# ── IOL Trade Bot — Local Runner ─────────────────────────────────────────────
# Corre el bot en esta máquina durante horario BYMA (11:00-17:00 ART).
# Coloca credenciales en .env (no se commitea) o setea las vars antes de correr.
#
# Uso:
#   .\run_local.ps1              # real (DRY_RUN=false)
#   .\run_local.ps1 -DryRun      # simulación
# ─────────────────────────────────────────────────────────────────────────────

param(
    [switch]$DryRun
)

$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | Where-Object { $_ -match "^\s*[^#].*=.*" } | ForEach-Object {
        $k, $v = $_ -split "=", 2
        [System.Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process")
    }
    Write-Host "[env] Loaded credentials from .env"
}

$required = @("IOL_USERNAME", "IOL_PASSWORD")
foreach ($r in $required) {
    if (-not [System.Environment]::GetEnvironmentVariable($r)) {
        Write-Error "Missing $r — define it in .env or set the env var manually."
        exit 1
    }
}

$env:DRY_RUN        = if ($DryRun) { "true" } else { "false" }
$env:LOOP_MINUTES   = "5"
$env:SCAN_BUDGET_PCT  = "30"
$env:SCAN_ASSET_TYPES = "ACCION,CEDEAR"

Write-Host "[bot] DRY_RUN=$($env:DRY_RUN) | Started $(Get-Date -Format 'HH:mm') ART"
Write-Host "[bot] Ctrl+C to stop"
Write-Host ""

python "$PSScriptRoot\scripts\trade_executor.py"
