@echo off
cd /d "%~dp0"

REM === 설정: Python 가상환경 경로 ===
set VENV_DIR=C:\Users\Qumin\opencode_venv

:restart
echo [%date% %time%] StreamSaver starting...
"%VENV_DIR%\Scripts\pythonw.exe" main.py
echo [%date% %time%] StreamSaver exited, restarting in 5s...
timeout /t 5 /nobreak >nul
goto restart
