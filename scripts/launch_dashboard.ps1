# Engraphis Dashboard Launcher
# Starts the memory server (if not already running) and opens the dashboard in the browser.

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Port = if ($env:ENGRAPHIS_PORT) { $env:ENGRAPHIS_PORT } else { 8700 }
$Url = "http://127.0.0.1:$Port"

# Check if server is already running
$running = $false
try {
    $response = Invoke-WebRequest -Uri "$Url/api/health" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
    if ($response.StatusCode -eq 200) { $running = $true }
} catch {
    $running = $false
}

if (-not $running) {
    # Load .env if it exists
    $envFile = Join-Path $ProjectDir ".env"
    if (Test-Path $envFile) {
        Get-Content $envFile | ForEach-Object {
            $line = $_.Trim()
            if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
                $parts = $line -split "=", 2
                $key = $parts[0].Trim()
                $val = $parts[1].Trim()
                Set-Item -Path "Env:$key" -Value $val
            }
        }
    }

    # Start server in a new minimized window
    $pythonExe = (Get-Command python).Source
    Start-Process -FilePath $pythonExe `
        -ArgumentList "-m", "scripts.start_dashboard" `
        -WorkingDirectory $ProjectDir `
        -WindowStyle Minimized `
        -PassThru | Out-Null

    # Wait for server to be ready (max 30 seconds)
    Write-Output "Starting Engraphis server..."
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        try {
            $response = Invoke-WebRequest -Uri "$Url/api/health" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
    }

    if (-not $ready) {
        Write-Output "Server failed to start. Check console window for errors."
        Read-Host "Press Enter to exit"
        exit 1
    }
    Write-Output "Server is ready."
} else {
    Write-Output "Server already running."
}

# Open the dashboard in the default browser
Start-Process $Url
