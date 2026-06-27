# setup_edge_portable.ps1
# Downloads/extracts Microsoft Edge Stable for portable use

param([string]$OutputDir = "E:\FFMPEG\edge_portable")

$ErrorActionPreference = "Stop"
$tmpDir = "$env:TEMP\edge_portable_setup"

Write-Host "=== Edge Portable Setup ===" -ForegroundColor Cyan

if (Test-Path "$OutputDir\msedge.exe") {
    $ver = & "$OutputDir\msedge.exe" --version 2>$null
    Write-Host "Already exists: $ver" -ForegroundColor Green
    exit 0
}

# Copy from system installation (simplest, most reliable)
$sysPaths = @(
    "C:\Program Files (x86)\Microsoft\Edge\Application",
    "C:\Program Files\Microsoft\Edge\Application"
)
foreach ($p in $sysPaths) {
    if (Test-Path "$p\msedge.exe") {
        Write-Host "Found Edge at $p" -ForegroundColor Yellow
        Write-Host "Copying to $OutputDir ..." -ForegroundColor Yellow
        New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
        Copy-Item -Path "$p\*" -Destination $OutputDir -Recurse -Force
        $ver = & "$OutputDir\msedge.exe" --version 2>$null
        Write-Host "Edge Portable ready: $ver" -ForegroundColor Green
        exit 0
    }
}

Write-Host "Edge not found on system. Download from:" -ForegroundColor Red
Write-Host "  https://www.microsoft.com/edge/business/download" -ForegroundColor Yellow
Write-Host "Then copy the files to: $OutputDir" -ForegroundColor Yellow
exit 1
