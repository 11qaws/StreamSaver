# StreamSaver 설치 마법사 빌드 스크립트
# 실행: .\build.ps1              — 클라이언트 빌드만
# 실행: .\build.ps1 -DeployServer — 클라이언트 빌드 + 릴레이 서버 배포
param(
    [switch]$DeployServer,
    [switch]$Release
)
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

# ── 5. GitHub 릴리즈 업로드 (옵션) ───────────────────────────────────────────
if ($Release) {
    Write-Host ""
    Write-Host "[5/?] GitHub 릴리즈 업로드 중..." -ForegroundColor Yellow

    $version = (Select-String -Path "$PSScriptRoot\setup.iss" -Pattern '#define MyAppVersion "(.+)"').Matches[0].Groups[1].Value
    $tag     = "v$version"
    $title   = "StreamSaver $tag"

    # 릴리즈 노트를 UTF-8 파일로 저장 (PowerShell 5.1 인코딩 우회)
    $notes = @"
## 변경사항

### 내부 경로 정리
- 앱 내부 데이터 파일(archive, cookie, history, watch_channels, .relay_guild)을 ``data/`` 하위로 통합
- crash.txt를 ``logs/`` 하위로 이동
- 업데이트 시 기존 파일 자동 마이그레이션

## 설치 방법
1. 아래 ``StreamSaver_Setup_v$version.exe`` 다운로드
2. 실행 후 안내에 따라 설치
"@
    $notesFile = [System.IO.Path]::GetTempFileName()
    [System.IO.File]::WriteAllText($notesFile, $notes, [System.Text.Encoding]::UTF8)

    try {
        gh release create $tag $OutFile.FullName `
            --repo 11qaws/StreamSaver `
            --title $title `
            --notes-file $notesFile
        if ($LASTEXITCODE -ne 0) { throw "gh release create 실패" }
        Write-Host "     -> GitHub 릴리즈 업로드 완료: $tag" -ForegroundColor Green
    } finally {
        Remove-Item $notesFile -ErrorAction SilentlyContinue
    }
}

# ── 6. 릴레이 서버 배포 (옵션) ────────────────────────────────────────────────
if ($DeployServer) {
    Write-Host ""
    Write-Host "[6/6] 릴레이 서버 배포 중..." -ForegroundColor Yellow
    $SSH_KEY  = "$env:USERPROFILE\.ssh\oracle.key"
    $SSH_HOST = "opc@217.142.229.237"
    $SRV_PY   = "$ROOT\relay_server\server.py"
    $REMOTE   = "/opt/streamsaver-relay"

    & scp -i $SSH_KEY -o StrictHostKeyChecking=no $SRV_PY "${SSH_HOST}:/tmp/server_new.py"
    if ($LASTEXITCODE -ne 0) { throw "scp 실패" }

    $cmd = "sudo cp /tmp/server_new.py $REMOTE/server.py && sudo systemctl restart streamsaver-relay"
    & ssh -i $SSH_KEY -o StrictHostKeyChecking=no $SSH_HOST $cmd
    if ($LASTEXITCODE -ne 0) { throw "서버 재시작 실패" }

    # 배포 기록 (커밋 해시 + 시각) — 버전 불일치 디버깅용
    $hash = git -C $ROOT rev-parse HEAD
    $ts   = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    & ssh -i $SSH_KEY -o StrictHostKeyChecking=no $SSH_HOST `
        "echo '$hash $ts' | sudo tee $REMOTE/deployed.txt > /dev/null"

    Write-Host "     -> 서버 배포 완료 (commit: $($hash.Substring(0,8)))" -ForegroundColor Green
    Write-Host "     -> $REMOTE/deployed.txt 에 배포 기록 저장됨" -ForegroundColor Gray
}
