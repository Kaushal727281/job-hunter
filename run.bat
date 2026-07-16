@echo off
:: ─────────────────────────────────────────────
::  Job Hunter — Start the app
::  Double-click this or run: run.bat
:: ─────────────────────────────────────────────

cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo [X] Virtual environment not found.
    echo     Please run setup.bat first.
    pause & exit /b 1
)

call .venv\Scripts\activate.bat
echo [OK] Starting Job Hunter...
echo [OK] Open browser at: http://localhost:5000
echo      Press Ctrl+C to stop.
echo.
python app.py
pause
