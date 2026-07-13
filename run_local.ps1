# IOL Trade Bot -- Local Runner
# Corre el bot en esta maquina durante horario BYMA (11:00-17:00 ART).
# Coloca credenciales en .env (no se commitea) o setea las vars antes de correr.
#
# Uso:
#   .\run_local.ps1              # simulacion (default seguro)
#   .\run_local.ps1 -Live        # real (DRY_RUN=false)
#   .\run_local.ps1 -DryRun      # simulacion explicita
#   .\run_local.ps1 -DryRun -LoopMinutes 1 -MaxIterations 2
#

param(
    [switch]$DryRun,
    [switch]$Live,
    [int]$LoopMinutes = 5,
    [int]$MaxIterations = 0
)

if ($DryRun -and $Live) {
    Write-Error "No puedes usar -DryRun y -Live al mismo tiempo."
    exit 1
}

$existingBot = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*iol-portfolio-fix*trade_executor.py*" }
if ($existingBot) {
    Write-Error "Ya hay una instancia activa de trade_executor.py. Detenla antes de lanzar otra."
    $existingBot | Select-Object ProcessId, CommandLine | Format-List
    exit 1
}

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
        Write-Error "Missing $r -- define it in .env o setea la var manualmente."
        exit 1
    }
}

$env:DRY_RUN          = if ($Live) { "false" } else { "true" }
$env:LOOP_MINUTES     = "$LoopMinutes"
$env:MAX_ITERATIONS   = "$MaxIterations"
$env:SCAN_BUDGET_PCT  = "30"
$env:SCAN_ASSET_TYPES = "ACCION,CEDEAR"

Write-Host "[preflight] Running local state audit..."
python "$PSScriptRoot\scripts\preflight_check.py"
Write-Host ""
Write-Host "[bot] DRY_RUN=$($env:DRY_RUN) | LOOP_MINUTES=$($env:LOOP_MINUTES) | MAX_ITERATIONS=$($env:MAX_ITERATIONS) | Started $(Get-Date -Format 'HH:mm') ART"
Write-Host "[bot] Ctrl+C to stop"
Write-Host ""

python "$PSScriptRoot\scripts\trade_executor.py"
