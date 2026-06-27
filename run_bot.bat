@echo off
cd /d "%~dp0"
set VENV_DIR=%~dp0venv

:restart
echo [%date% %time%] StreamSaver starting...
"%VENV_DIR%\Scripts\pythonw.exe" main.py
echo [%date% %time%] StreamSaver exited, restarting in 5s...
timeout /t 5 /nobreak >nul
goto restart
