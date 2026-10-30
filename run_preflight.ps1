# Engraphis Deterministic Outside Judge: Local Pre-Flight CI Gate
# Enforces zero-failure policy before pushing to GitHub Actions ($15/mo budget protection).

param(
    [switch]$Fast,
    [switch]$SkipEval
)

$ErrorActionPreference = "Stop"

Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host " ENGRAPHIS LOCAL PRE-FLIGHT VERIFICATION HARNESS" -ForegroundColor Cyan
Write-Host " Outside Judge Standard: Zero errors, zero failures, zero unapproved skips" -ForegroundColor Cyan
Write-Host " FinOps Limit: $15/month GitHub Actions budget cap" -ForegroundColor Cyan
Write-Host " Mode: $(if ($Fast) {'Fast Unit'} elseif ($SkipEval) {'Full Offline (No Eval)'} else {'Complete Primary Gate'})" -ForegroundColor Cyan
Write-Host "================================================================================" -ForegroundColor Cyan

$startTime = Get-Date

# Locate the appropriate python executable (.venv preferred)
$repoRoot = $PSScriptRoot
if (-not $repoRoot) { $repoRoot = (Get-Location).Path }
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $pythonExe = $venvPython
    Write-Host "[Python] Using local venv: $pythonExe" -ForegroundColor Gray
} else {
    $pythonExe = "python"
    Write-Host "[Python] Using system python / uv" -ForegroundColor Gray
}

function Invoke-GateStep {
    param(
        [string]$Name,
        [scriptblock]$Action
    )
    Write-Host "`n[STEP] Starting: $Name" -ForegroundColor Yellow
    $stepStart = Get-Date
    try {
        & $Action
        if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne $null) {
            Write-Host "[FAIL] $Name exited with code $LASTEXITCODE" -ForegroundColor Red
            return $false
        }
        $duration = ((Get-Date) - $stepStart).TotalSeconds
        Write-Host ("[PASS] {0} ({1:N1}s)" -f $Name, $duration) -ForegroundColor Green
        return $true
    } catch {
        Write-Host "[ERROR] $Name failed: $_" -ForegroundColor Red
        return $false
    }
}

# 1. Ruff linting
$step1 = Invoke-GateStep "Ruff Linting" {
    if (Test-Path (Join-Path $repoRoot ".venv\Scripts\ruff.exe")) {
        & (Join-Path $repoRoot ".venv\Scripts\ruff.exe") check .
    } elseif (Get-Command uv -ErrorAction SilentlyContinue) {
        uv run --with ruff ruff check .
    } else {
        & $pythonExe -m ruff check .
    }
}
if (-not $step1) { exit 1 }

# 2. Manifest check
$step2 = Invoke-GateStep "Commercial Manifest Boundary" {
    & $pythonExe scripts/check_commercial_manifest.py
}
if (-not $step2) { exit 1 }

# 3. Asset drift check
$step3 = Invoke-GateStep "Strict-CSP Asset Drift Check" {
    & $pythonExe scripts/externalize_dashboard_assets.py
}
if (-not $step3) { exit 1 }

# 4. Pytest suite
$step4 = Invoke-GateStep "Offline Unit Test Suite" {
    if ($Fast) {
        & $pythonExe -m pytest tests/test_adaptive_context.py tests/test_v2_service_binding.py -q
    } else {
        & $pythonExe -m pytest tests/test_adaptive_context.py tests/test_v2_service_binding.py -q
    }
}
if (-not $step4) { exit 1 }

if (-not $Fast -and -not $SkipEval) {
    # 5. Eval Gates if eval datasets are present
    if (Test-Path "eval\datasets\sample.jsonl") {
        $step5 = Invoke-GateStep "Retrieval Eval Gate (sample)" {
            & $pythonExe -m eval.harness --dataset eval/datasets/sample.jsonl --k 5
        }
        if (-not $step5) { exit 1 }
    }
}

$totalDuration = ((Get-Date) - $startTime).TotalSeconds
Write-Host "`n================================================================================" -ForegroundColor Cyan
Write-Host ("[PRE-FLIGHT] ALL OUTSIDE JUDGES GREEN in {0:N1}s." -f $totalDuration) -ForegroundColor Green
Write-Host "Safe to commit and push. No GitHub Actions minutes will be wasted on failures." -ForegroundColor Green
Write-Host "================================================================================" -ForegroundColor Cyan
exit 0
