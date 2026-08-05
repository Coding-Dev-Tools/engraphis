# Engraphis Directory Submission Helper
# Run this in an interactive PowerShell terminal with browser access

$ErrorActionPreference = "Stop"
$repoUrl = "https://github.com/Coding-Dev-Tools/engraphis"

Write-Host "=== Engraphis Directory Submissions ===" -ForegroundColor Cyan
Write-Host ""

# 1. MCP Registry (feeds MCP Toplist automatically)
Write-Host "[1/3] MCP Registry" -ForegroundColor Yellow
$publisher = "C:\tmp\mcp-publisher.exe"
if (-not (Test-Path $publisher)) {
    Write-Host "  Downloading mcp-publisher..." -ForegroundColor Gray
    $arch = if ([System.Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture -eq "Arm64") { "arm64" } else { "amd64" }
    Invoke-WebRequest -Uri "https://github.com/modelcontextprotocol/registry/releases/latest/download/mcp-publisher_windows_$arch.tar.gz" -OutFile "$env:TEMP\mcp-publisher.tar.gz"
    tar xf "$env:TEMP\mcp-publisher.tar.gz" mcp-publisher.exe
    $publisher = ".\mcp-publisher.exe"
}
Write-Host "  Running: $publisher login github" -ForegroundColor Gray
& $publisher login github
Write-Host "  Running: $publisher publish" -ForegroundColor Gray
& $publisher publish
Write-Host "  ✓ MCP Registry published (MCP Toplist syncs 2x daily)" -ForegroundColor Green
Write-Host ""

# 2. Glama
Write-Host "[2/3] Glama" -ForegroundColor Yellow
Write-Host "  Opening Glama Add Server page..." -ForegroundColor Gray
Start-Process "https://glama.ai/mcp/servers"
Write-Host "  → Click 'Add Server', sign in with GitHub, paste: $repoUrl" -ForegroundColor White
Write-Host "  → glama.json is already in the repo for maintainer claim" -ForegroundColor Gray
Read-Host "  Press Enter when done"
Write-Host "  ✓ Glama submitted" -ForegroundColor Green
Write-Host ""

# 3. LobeHub
Write-Host "[3/3] LobeHub" -ForegroundColor Yellow
Write-Host "  Running LobeHub CLI login..." -ForegroundColor Gray
npx -y @lobehub/market-cli login
npx -y @lobehub/market-cli github connect
npx -y @lobehub/market-cli plugin submit $repoUrl
Write-Host "  ✓ LobeHub submitted" -ForegroundColor Green
Write-Host ""

Write-Host "=== All submissions complete ===" -ForegroundColor Cyan
