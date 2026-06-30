# StreamSaver 설치 마법사 빌드 스크립트
# 실행: .\build.ps1
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ROOT    = Split-Path $PSScriptRoot -Parent
$VENV    = "$ROOT\venv\Scripts"
$ISCC    = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
$DIST    = "$ROOT\dist\StreamSaver"
$OUTDIR  = "$PSScriptRoot\Output"

Write-Host "=== StreamSaver 빌드 시작 ===" -ForegroundColor Cyan

# ── 1. PyInstaller ────────────────────────────────────────────────────────────
Write-Host "[1/4] PyInstaller 빌드 중..." -ForegroundColor Yellow
Push-Location $ROOT
& "$VENV\pyinstaller.exe" installer\StreamSaver.spec --distpath dist --workpath build --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 실패" }
Pop-Location
Write-Host "     -> $DIST" -ForegroundColor Green

# ── 2. yt-dlp 다운로드 ────────────────────────────────────────────────────────
Write-Host "[2/4] yt-dlp.exe 다운로드 중..." -ForegroundColor Yellow
$ytdlp = "$PSScriptRoot\yt-dlp.exe"
if (-not (Test-Path $ytdlp)) {
    Invoke-WebRequest -Uri "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" `
        -OutFile $ytdlp -UseBasicParsing
    Write-Host "     -> yt-dlp.exe 다운로드 완료" -ForegroundColor Green
} else {
    Write-Host "     -> 이미 존재함: $ytdlp" -ForegroundColor Gray
}

# ── 3. ffmpeg 다운로드 ────────────────────────────────────────────────────────
Write-Host "[3/4] ffmpeg.exe 다운로드 중..." -ForegroundColor Yellow
$ffmpeg = "$PSScriptRoot\ffmpeg.exe"
if (-not (Test-Path $ffmpeg)) {
    $ffzipUrl = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
    $ffzip    = "$env:TEMP\ffmpeg.zip"
    Invoke-WebRequest -Uri $ffzipUrl -OutFile $ffzip -UseBasicParsing
    $tmpDir = "$env:TEMP\ffmpeg_extract"
    Expand-Archive -Path $ffzip -DestinationPath $tmpDir -Force
    $ffexe = Get-ChildItem -Path $tmpDir -Filter "ffmpeg.exe" -Recurse | Select-Object -First 1
    Copy-Item $ffexe.FullName -Destination $ffmpeg
    Remove-Item $ffzip, $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "     -> ffmpeg.exe 다운로드 완료" -ForegroundColor Green
} else {
    Write-Host "     -> 이미 존재함: $ffmpeg" -ForegroundColor Gray
}

# ── 4. Inno Setup 컴파일 ──────────────────────────────────────────────────────
Write-Host "[4/4] 설치 마법사 컴파일 중..." -ForegroundColor Yellow
if (-not (Test-Path $ISCC)) { throw "Inno Setup을 찾을 수 없습니다: $ISCC" }
New-Item -ItemType Directory -Force -Path $OUTDIR | Out-Null
& $ISCC "$PSScriptRoot\setup.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 컴파일 실패" }

$OutFile = Get-ChildItem "$OUTDIR\StreamSaver_Setup_v*.exe" | Select-Object -Last 1
Write-Host ""
Write-Host "=== 빌드 완료 ===" -ForegroundColor Cyan
Write-Host "설치 파일: $($OutFile.FullName)" -ForegroundColor Green
