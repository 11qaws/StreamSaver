# StreamSaver build script
# Usage: .\build.ps1                        - client build only
#        .\build.ps1 -Release               - build + GitHub release upload
#        .\build.ps1 -Release -DeployServer - build + release + server deploy
param(
    [switch]$DeployServer,
    [switch]$Release,
    [string]$GhToken = $env:GH_TOKEN
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ROOT   = Split-Path $PSScriptRoot -Parent
$VENV   = "$ROOT\venv\Scripts"
$ISCC   = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
$DIST   = "$ROOT\dist\StreamSaver"
$OUTDIR = "$PSScriptRoot\Output"

Write-Host "=== StreamSaver build ===" -ForegroundColor Cyan

# -- 1. PyInstaller -----------------------------------------------------------
Write-Host "[1/4] PyInstaller..." -ForegroundColor Yellow
Push-Location $ROOT
& "$VENV\pyinstaller.exe" installer\StreamSaver.spec --distpath dist --workpath build --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Pop-Location
Write-Host "     -> $DIST" -ForegroundColor Green

# -- 2. yt-dlp ----------------------------------------------------------------
Write-Host "[2/4] yt-dlp.exe..." -ForegroundColor Yellow
$ytdlp = "$PSScriptRoot\yt-dlp.exe"
if (-not (Test-Path $ytdlp)) {
    Invoke-WebRequest -Uri "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" `
        -OutFile $ytdlp -UseBasicParsing
    Write-Host "     -> downloaded" -ForegroundColor Green
} else {
    Write-Host "     -> already exists: $ytdlp" -ForegroundColor Gray
}

# -- 3. ffmpeg ----------------------------------------------------------------
Write-Host "[3/4] ffmpeg.exe..." -ForegroundColor Yellow
$ffmpeg = "$PSScriptRoot\ffmpeg.exe"
if (-not (Test-Path $ffmpeg)) {
    $ffzipUrl = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
    $ffzip    = "$env:TEMP\ffmpeg.zip"
    Invoke-WebRequest -Uri $ffzipUrl -OutFile $ffzip -UseBasicParsing
    $tmpDir   = "$env:TEMP\ffmpeg_extract"
    Expand-Archive -Path $ffzip -DestinationPath $tmpDir -Force
    $ffexe    = Get-ChildItem -Path $tmpDir -Filter "ffmpeg.exe" -Recurse | Select-Object -First 1
    Copy-Item $ffexe.FullName -Destination $ffmpeg
    Remove-Item $ffzip, $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "     -> downloaded" -ForegroundColor Green
} else {
    Write-Host "     -> already exists: $ffmpeg" -ForegroundColor Gray
}

# -- 4. Inno Setup ------------------------------------------------------------
Write-Host "[4/4] Inno Setup..." -ForegroundColor Yellow
if (-not (Test-Path $ISCC)) { throw "Inno Setup not found: $ISCC" }
New-Item -ItemType Directory -Force -Path $OUTDIR | Out-Null
& $ISCC "$PSScriptRoot\setup.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

$OutFile = Get-ChildItem "$OUTDIR\StreamSaver_Setup_v*.exe" | Sort-Object LastWriteTime | Select-Object -Last 1
Write-Host ""
Write-Host "=== Build complete ===" -ForegroundColor Cyan
Write-Host "Installer: $($OutFile.FullName)" -ForegroundColor Green

# -- 5. GitHub release upload (optional) -------------------------------------
if ($Release) {
    Write-Host ""
    Write-Host "[5/6] GitHub release upload..." -ForegroundColor Yellow

    $version = (Select-String -Path "$PSScriptRoot\setup.iss" -Pattern '#define MyAppVersion "(.+)"').Matches[0].Groups[1].Value
    $tag   = "v$version"
    $title = "StreamSaver $tag"
    $notes = "## 변경사항`n`n### 버그 수정 (v1.0.15)`n- Unarchived 관리 창이 두 번째 열기부터 뜨지 않던 문제 수정 (tkinter root 생명주기 오류)`n- 서버 연결 다이얼로그 동일 패턴 예방적 수정`n- 다이얼로그 오류 시 로그에 기록되도록 예외 처리 추가`n`n## 설치 방법`n1. 아래 ``StreamSaver_Setup_v$version.exe`` 다운로드`n2. 실행 중인 StreamSaver가 있으면 자동 종료 후 업데이트`n3. 설치 완료 후 자동 재시작"

    $headers = @{
        Authorization = "token $GhToken"
        Accept        = "application/vnd.github+json"
    }
    $body = @{ tag_name = $tag; name = $title; body = $notes } | ConvertTo-Json

    # 1) 릴리즈 생성
    $release = Invoke-RestMethod `
        -Uri "https://api.github.com/repos/11qaws/StreamSaver/releases" `
        -Method Post -Headers $headers `
        -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) `
        -ContentType "application/json; charset=utf-8"

    # 2) 인스톨러 업로드
    $uploadUrl = "https://uploads.github.com/repos/11qaws/StreamSaver/releases/$($release.id)/assets?name=$($OutFile.Name)"
    $fileBytes  = [System.IO.File]::ReadAllBytes($OutFile.FullName)
    Invoke-RestMethod `
        -Uri $uploadUrl -Method Post -Headers $headers `
        -Body $fileBytes -ContentType "application/octet-stream" | Out-Null

    Write-Host "     -> Released: $($release.html_url)" -ForegroundColor Green
}

# -- 6. Relay server deploy (optional) ----------------------------------------
if ($DeployServer) {
    Write-Host ""
    Write-Host "[6/6] Deploy relay server..." -ForegroundColor Yellow
    $SSH_KEY  = "$env:USERPROFILE\.ssh\oracle.key"
    $SSH_HOST = "opc@217.142.229.237"
    $SRV_PY   = "$ROOT\relay_server\server.py"
    $REMOTE   = "/opt/streamsaver-relay"

    & scp -i $SSH_KEY -o StrictHostKeyChecking=no $SRV_PY "${SSH_HOST}:/tmp/server_new.py"
    if ($LASTEXITCODE -ne 0) { throw "scp failed" }

    $cmd = "sudo cp /tmp/server_new.py $REMOTE/server.py && sudo systemctl restart streamsaver-relay"
    & ssh -i $SSH_KEY -o StrictHostKeyChecking=no $SSH_HOST $cmd
    if ($LASTEXITCODE -ne 0) { throw "server restart failed" }

    $hash = git -C $ROOT rev-parse HEAD
    $ts   = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    & ssh -i $SSH_KEY -o StrictHostKeyChecking=no $SSH_HOST "echo '$hash $ts' | sudo tee $REMOTE/deployed.txt > /dev/null"

    Write-Host "     -> Deployed (commit: $($hash.Substring(0,8)))" -ForegroundColor Green
    Write-Host "     -> $REMOTE/deployed.txt updated" -ForegroundColor Gray
}
