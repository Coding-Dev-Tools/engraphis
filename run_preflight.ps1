<#
.SYNOPSIS
    Engraphis local pre-flight verification.
.DESCRIPTION
    Mirrors the offline CI gates (ruff, manifest, CSP drift, pyright, pytest, eval/*).
    Use -Fast for a Python 3.9+ two-file smoke or -SkipEval to omit eval modules.
.PARAMETER Fast
    Run lint, manifest/asset checks, and the two-file smoke suite (skips pyright and eval).
.PARAMETER SkipEval
    Run the full offline pytest suite without the eval/* verification gates.
.PARAMETER Workers
    Optional pytest-xdist worker count (or 'auto') for the offline unit suite.
#>

param(
    [switch]$Fast,
    [switch]$SkipEval,
    [ValidatePattern('^$|^(auto|[1-9][0-9]*)$')]
    [string]$Workers = ""
)

$ErrorActionPreference = "Stop"

$modeLabel = if ($Fast) { 'Fast smoke' } elseif ($SkipEval) { 'Offline unit suite without eval' } else { 'Complete primary gate' }
$isPartialRun = $Fast -or $SkipEval

Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host " ENGRAPHIS LOCAL PRE-FLIGHT VERIFICATION" -ForegroundColor Cyan
Write-Host " Mode: $modeLabel" -ForegroundColor Cyan
Write-Host "================================================================================" -ForegroundColor Cyan

$startTime = Get-Date

# Locate the appropriate python executable (.venv preferred, Windows or POSIX layout)
$repoRoot = $PSScriptRoot
if (-not $repoRoot) { $repoRoot = (Get-Location).Path }
$pythonCandidates = @(
    (Join-Path $repoRoot ".venv\Scripts\python.exe"),
    (Join-Path $repoRoot ".venv/bin/python")
)
$venvPython = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if ($venvPython) {
    $pythonExe = $venvPython
    Write-Host "[Python] Using local venv: $pythonExe" -ForegroundColor Gray
} else {
    $pythonExe = "python"
    Write-Host "[Python] Using system python / uv" -ForegroundColor Gray
}

$ruffCandidates = @(
    (Join-Path $repoRoot ".venv\Scripts\ruff.exe"),
    (Join-Path $repoRoot ".venv/bin/ruff")
)
$venvRuff = $ruffCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

function Test-PythonModule {
    param([string]$ModuleName)
    # A real import, not find_spec: a broken wheel or a missing DLL surfaces here. Seed
    # LASTEXITCODE so an unlaunchable interpreter cannot inherit 0 and return True.
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 1
    try {
        $null = & $pythonExe -c "import importlib, sys; importlib.import_module(sys.argv[1])" $ModuleName 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

# Locate pyright: the venv console script first, then PATH, then python -m pyright.
# [tool.pyright] declares no venvPath/venv, so the gate pins the interpreter it should
# analyse explicitly with --pythonpath; the branches below only choose how pyright
# itself is launched.
$pyrightCandidates = @(
    (Join-Path $repoRoot ".venv\Scripts\pyright.exe"),
    (Join-Path $repoRoot ".venv/bin/pyright")
)
$venvPyright = $pyrightCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
$pyrightMode = $null
$pyrightExe = $null
if ($venvPyright) {
    $pyrightMode = "exe"
    $pyrightExe = $venvPyright
} elseif (Get-Command pyright -ErrorAction SilentlyContinue) {
    $pyrightMode = "exe"
    $pyrightExe = (Get-Command pyright).Source
} elseif (Test-PythonModule "pyright") {
    $pyrightMode = "module"
}
if ($pyrightMode -eq "exe") {
    Write-Host "[Pyright] Using: $pyrightExe" -ForegroundColor Gray
} elseif ($pyrightMode -eq "module") {
    Write-Host "[Pyright] Using: $pythonExe -m pyright" -ForegroundColor Gray
}

# pytest's own CI runs always supply a private runner-owned --basetemp; mirror that here
# so a stale temp dir from an earlier run cannot poison this one.
$pytestBaseTemp = Join-Path ([IO.Path]::GetTempPath()) ("engraphis-preflight-pytest-" + [Guid]::NewGuid().ToString("N"))

$script:lastStepPassed = $false

function Invoke-GateStep {
    param(
        [string]$Name,
        [scriptblock]$Action
    )
    $script:lastStepPassed = $false
    Write-Host "`n[STEP] Starting: $Name" -ForegroundColor Yellow
    $stepStart = Get-Date
    $global:LASTEXITCODE = 0
    try {
        & $Action
        if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne $null) {
            Write-Host "[FAIL] $Name exited with code $LASTEXITCODE" -ForegroundColor Red
            return
        }
        $duration = ((Get-Date) - $stepStart).TotalSeconds
        Write-Host ("[PASS] {0} ({1:N1}s)" -f $Name, $duration) -ForegroundColor Green
        $script:lastStepPassed = $true
    } catch {
        Write-Host "[ERROR] $Name failed: $_" -ForegroundColor Red
    }
}

$locationPushed = $false
try {
    # Run every gate from the repository root so `./run_preflight.ps1` behaves the same
    # when it is invoked from anywhere else.
    Push-Location -Path $repoRoot
    $locationPushed = $true

    # 1. Ruff linting
    Invoke-GateStep "Ruff Linting" {
        if ($venvRuff) {
            & $venvRuff check .
        } elseif (Get-Command uv -ErrorAction SilentlyContinue) {
            uv run --with ruff ruff check .
        } else {
            & $pythonExe -m ruff check .
        }
    }
    if (-not $script:lastStepPassed) { exit 1 }

    # 2. Manifest check
    Invoke-GateStep "Commercial Manifest Boundary" {
        & $pythonExe scripts/check_commercial_manifest.py
    }
    if (-not $script:lastStepPassed) { exit 1 }

    # 3. Asset drift check
    Invoke-GateStep "Strict-CSP Asset Drift Check" {
        & $pythonExe scripts/externalize_dashboard_assets.py
    }
    if (-not $script:lastStepPassed) { exit 1 }

    # 4. Pyright type check (core + backends; skipped in -Fast so Python 3.9 numpy-only smoke works)
    if (-not $Fast) {
        Invoke-GateStep "Pyright Type Check" {
            $pyrightArgs = @()
            if ($venvPython) {
                $pyrightArgs += @("--pythonpath", $pythonExe)
            }
            if ($pyrightMode -eq "exe") {
                & $pyrightExe @pyrightArgs
            } elseif ($pyrightMode -eq "module") {
                & $pythonExe -m pyright @pyrightArgs
            } else {
                throw "pyright not found. Install the dev/test extras or add pyright to PATH."
            }
        }
        if (-not $script:lastStepPassed) { exit 1 }
    }

    # 5. Pytest suite
    Invoke-GateStep "Offline Unit Test Suite" {
        if ($Fast -and -not [string]::IsNullOrWhiteSpace($Workers)) {
            Write-Host "[pytest] -Workers ignored in -Fast mode" -ForegroundColor Gray
        }
        if (-not $Fast) {
            # [test] extras require Python 3.10+; read version without Select-Object so $LASTEXITCODE stays 0.
            $pyVer = (& $pythonExe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null | Out-String).Trim()
            if ($LASTEXITCODE -eq 0 -and $pyVer -match '^\d+\.\d+$' -and ([Version]$pyVer -lt [Version]'3.10')) {
                throw ("Full offline preflight requires Python 3.10+ for [test] extras (found Python $pyVer). Run .\run_preflight.ps1 -Fast on Python 3.9, matching CI's numpy-only job.")
            }
            # A stripped venv would let the extras-gated server/MCP tests skip silently and
            # report green. Fail loudly instead: the full offline suite needs them.
            $requiredModules = @("fastapi", "httpx", "mcp", "multipart", "pydantic", "sqlite_vec", "uvicorn")
            $missingModules = @($requiredModules | Where-Object { -not (Test-PythonModule $_) })
            if ($missingModules.Count -gt 0) {
                throw ('Missing full-stack test dependencies: {0}. Install them with: pip install -e ".[test]" (Python 3.10+)' -f ($missingModules -join ", "))
            }
            $pytestArgs = @("-o", "addopts=", "tests/", "-q", "-rs", "--basetemp", $pytestBaseTemp)
            if (Test-PythonModule "xdist") {
                if (-not [string]::IsNullOrWhiteSpace($Workers)) {
                    Write-Host "[pytest] pytest-xdist detected; running with -n $Workers" -ForegroundColor Gray
                    $pytestArgs += @("-n", $Workers)
                } elseif ($env:OS -eq 'Windows_NT') {
                    Write-Host "[pytest] Windows host detected; running serially to avoid subprocess timeout contention (pass -Workers auto to opt in)" -ForegroundColor Gray
                } else {
                    Write-Host "[pytest] pytest-xdist detected; running with -n auto" -ForegroundColor Gray
                    $pytestArgs += @("-n", "auto")
                }
            } else {
                Write-Host "[pytest] pytest-xdist not installed; running serially" -ForegroundColor Gray
            }
        } else {
            $pytestArgs = @("-o", "addopts=", "tests/test_adaptive_context.py", "tests/test_v2_service_binding.py", "-q", "-rs", "--basetemp", $pytestBaseTemp)
        }
        # CI scopes ENGRAPHIS_INDEX_ROOTS to pytest; include Python's tempfile.gettempdir().
        $previousIndexRoots = $env:ENGRAPHIS_INDEX_ROOTS
        $pyTemp = (& $pythonExe -c "import tempfile; print(tempfile.gettempdir())" 2>$null | Out-String).Trim()
        $indexRoots = @($repoRoot, [IO.Path]::GetTempPath().TrimEnd('\', '/'), $pytestBaseTemp)
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($pyTemp)) { $indexRoots += $pyTemp }
        $env:ENGRAPHIS_INDEX_ROOTS = (($indexRoots | Select-Object -Unique) -join [IO.Path]::PathSeparator)
        try {
            & $pythonExe -m pytest @pytestArgs
        } finally {
            $env:ENGRAPHIS_INDEX_ROOTS = $previousIndexRoots
        }
    }
    if (-not $script:lastStepPassed) { exit 1 }

    if (-not $Fast -and -not $SkipEval) {
        # 6. Eval Gates matching the offline CI suite
        $evalGates = @(
            @{ Name = "Retrieval Eval Gate (sample)"; Dataset = "eval/datasets/sample.jsonl"; Args = @("-m", "eval.harness", "--dataset", "eval/datasets/sample.jsonl", "--k", "5") },
            @{ Name = "Retrieval Eval Gate (codemem)"; Dataset = "eval/datasets/codemem.jsonl"; Args = @("-m", "eval.harness", "--dataset", "eval/datasets/codemem.jsonl", "--k", "5") },
            @{ Name = "Ablation Eval Gate"; Args = @("-m", "eval.ablation") },
            @{ Name = "Reinforcement Eval Gate"; Args = @("-m", "eval.reinforcement") },
            @{ Name = "Adversarial Memory Security Gate"; Args = @("-m", "eval.adversarial_memory_security") },
            @{ Name = "Grounded Recall Eval Gate"; Args = @("-m", "eval.grounded") },
            @{ Name = "Code-Agent Arm Gate"; Args = @("-m", "eval.code_arm") },
            @{ Name = "Spec Crawl Eval Gate"; Args = @("-m", "eval.spec_crawl") },
            @{ Name = "Evidence Contracts Gate"; Args = @("-m", "eval.evidence_contracts") }
        )
        foreach ($gate in $evalGates) {
            if ($gate.ContainsKey("Dataset")) {
                $datasetPath = Join-Path $repoRoot ($gate.Dataset -replace '/', [IO.Path]::DirectorySeparatorChar)
                if (-not (Test-Path -LiteralPath $datasetPath)) {
                    Write-Host "[FAIL] Missing required eval dataset '$($gate.Dataset)' (expected at $datasetPath)." -ForegroundColor Red
                    Write-Host "[FAIL] The pre-flight gate must never silently skip a missing dataset; restore it and re-run." -ForegroundColor Red
                    exit 1
                }
            }
            $evalArgs = $gate.Args
            Invoke-GateStep $gate.Name {
                & $pythonExe @evalArgs
            }
            if (-not $script:lastStepPassed) { exit 1 }
        }
    }

    $totalDuration = ((Get-Date) - $startTime).TotalSeconds
    Write-Host "`n================================================================================" -ForegroundColor Cyan
    if ($isPartialRun) {
        Write-Host ("[PRE-FLIGHT] LIMITED GATES PASSED ({0}) in {1:N1}s." -f $modeLabel, $totalDuration) -ForegroundColor Yellow
        Write-Host "Only the '$modeLabel' gates ran; the full offline CI gate did not." -ForegroundColor Yellow
        Write-Host "Run the complete .\run_preflight.ps1 before push." -ForegroundColor Yellow
    } else {
        Write-Host ("[PRE-FLIGHT] ALL GATES GREEN in {0:N1}s." -f $totalDuration) -ForegroundColor Green
        Write-Host "Safe to commit and push."
    }
    Write-Host "================================================================================" -ForegroundColor Cyan
    exit 0
} finally {
    if ($locationPushed) { Pop-Location }
    if (Test-Path -LiteralPath $pytestBaseTemp) {
        $tempRootPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
        $resolvedBaseTemp = [IO.Path]::GetFullPath($pytestBaseTemp)
        if ($resolvedBaseTemp.StartsWith($tempRootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolvedBaseTemp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
