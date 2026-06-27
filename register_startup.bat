@echo off
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set TARGET=%~dp0run_bot.bat
set SHORTCUT=%STARTUP_DIR%\StreamSaver.lnk

powershell -Command ^
    $WS = New-Object -ComObject WScript.Shell; ^
    $SC = $WS.CreateShortcut('%SHORTCUT%'); ^
    $SC.TargetPath = '%TARGET%'; ^
    $SC.WorkingDirectory = '%~dp0'; ^
    $SC.Description = 'StreamSaver Discord Bot'; ^
    $SC.Save()

echo StreamSaver registered in startup.
echo Shortcut: %SHORTCUT%
pause
