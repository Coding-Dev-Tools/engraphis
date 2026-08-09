# Engraphis Dashboard Launcher
# Delegates configuration parsing, health validation, browser opening, and process
# lifecycle to the canonical Python entry point instead of maintaining a second launcher.
$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

$dashboard = Get-Command "engraphis-dashboard" -CommandType Application -ErrorAction SilentlyContinue
if ($null -ne $dashboard) {
    & $dashboard.Source
    exit $LASTEXITCODE
}

$python = Get-Command "python" -CommandType Application -ErrorAction SilentlyContinue
if ($null -eq $python) {
    Write-Error "Engraphis Dashboard could not start because Python was not found."
    exit 1
}

Push-Location $ProjectDir
try {
    & $python.Source -m scripts.start_dashboard
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $exitCode
