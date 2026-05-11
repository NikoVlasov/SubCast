@echo off
title SubFlow Watch
cd /d "%~dp0"

echo ========================================
echo         SubFlow Watch Window
echo ========================================
echo.
echo Make sure run_server.bat is already running.
echo.

call .venv\Scripts\activate.bat
python watch_window.py

echo.
pause
