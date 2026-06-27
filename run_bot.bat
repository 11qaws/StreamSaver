@echo off
cd /d "%~dp0"
set VENV_DIR=%~dp0venv
set RESTART_COUNT=0

:restart
echo [%date% %time%] StreamSaver starting...
"%VENV_DIR%\Scripts\pythonw.exe" main.py
set EXIT_CODE=%ERRORLEVEL%
echo [%date% %time%] StreamSaver exited with code %EXIT_CODE%

if %EXIT_CODE%==0 goto :eof
if %EXIT_CODE%==42 goto :eof

set /a RESTART_COUNT+=1
if %RESTART_COUNT% geq 5 (
    echo [%date% %time%] Too many restarts, waiting 60s...
    timeout /t 60 /nobreak >nul
    set RESTART_COUNT=0
) else (
    echo [%date% %time%] Restarting in 5s... (attempt %RESTART_COUNT%)
    timeout /t 5 /nobreak >nul
)
goto restart
