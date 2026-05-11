@echo off
title SubCast Server
cd /d "%~dp0"
call .venv\Scripts\activate.bat

:loop
echo ========================================
echo        Starting SubCast server...
echo ========================================
python app.py
echo.
echo Server stopped. Restarting in 5 seconds...
echo Press Ctrl+C to cancel.
timeout /t 5 /nobreak
goto loop
