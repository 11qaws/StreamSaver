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

# Locate gh CLI
$_ghCmd = Get-Command gh -ErrorAction SilentlyContinue
$GH = if ($_ghCmd) { $_ghCmd.Source } else { "C:\Program Files\GitHub CLI\gh.exe" }
if ($Release -and -not (Test-Path $GH)) { throw "gh CLI not found: $GH" }

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

    # Write release notes as UTF-8 file to avoid PowerShell 5.1 CP949 default encoding
    $noteLines = @(
        "## Changes (v1.0.13 ~ v1.0.14)",
        "",
        "### Tray features (v1.0.13)",
        "- Download folder change without restart (.env saved permanently)",
        "- Unarchived channel management dialog (add/remove, synced with bot)",
        "",
        "### Internal file path reorganization (v1.0.14)",
        "- App data files (archive.txt, cookie.txt, history.json, watch_channels.json, .relay_guild) moved to ``%LOCALAPPDATA%\StreamSaver\data\``",
        "- crash.txt moved to ``logs\``",
        "- **Automatic migration on update** - no manual reconfiguration needed",
        "",
        "## Install",
        "1. Download ``StreamSaver_Setup_v$version.exe`` below",
        "2. Run installer (will auto-close existing StreamSaver)",
        "3. App restarts automatically after install"
    )
    $notes     = [string]::Join([char]10, $noteLines)
    $notesFile = [System.IO.Path]::GetTempFileName()
    [System.IO.File]::WriteAllText($notesFile, $notes, [System.Text.Encoding]::UTF8)

    try {
        $env:GH_TOKEN = $GhToken
        & $GH release create $tag $OutFile.FullName `
            --repo 11qaws/StreamSaver `
            --title $title `
            --notes-file $notesFile
        if ($LASTEXITCODE -ne 0) { throw "gh release create failed" }
        Write-Host "     -> Released: $tag" -ForegroundColor Green
    } finally {
        Remove-Item $notesFile -ErrorAction SilentlyContinue
    }
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
