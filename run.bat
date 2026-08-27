@echo off
setlocal EnableDelayedExpansion
:: ─────────────────────────────────────────────
::  Job Hunter — Start the app
::  Double-click this or run: run.bat
:: ─────────────────────────────────────────────

cd /d "%~dp0"

set VENV_ACTIVATE=
if exist ".venv\Scripts\activate.bat" set VENV_ACTIVATE=.venv\Scripts\activate.bat
if exist "venv\Scripts\activate.bat"  set VENV_ACTIVATE=venv\Scripts\activate.bat
if "%VENV_ACTIVATE%"=="" (
    echo [X] Virtual environment not found ^(looked for .venv and venv^).
    echo     Please run setup.bat first.
    pause & exit /b 1
)

:: ── If port 5000 is already in use by THIS app, kill it and restart ──
:: Only kills it if the process is actually app.py from this folder — never
:: touches an unrelated program that happens to be using the port.
set PORT=5000
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    set EXISTING_PID=%%p
)
if defined EXISTING_PID (
    wmic process where "ProcessId=%EXISTING_PID%" get CommandLine 2>nul | findstr /i "app.py" >nul
    if !errorlevel! equ 0 (
        echo [!] Port %PORT% is already running this app ^(PID %EXISTING_PID%^) - restarting it...
        taskkill /PID %EXISTING_PID% /F >nul 2>&1
        timeout /t 2 /nobreak >nul
    ) else (
        echo [X] Port %PORT% is in use by a different program ^(PID %EXISTING_PID%^), not job-hunter.
        echo     Not killing it automatically - stop it yourself or change PORT in app.py.
        pause & exit /b 1
    )
)

call %VENV_ACTIVATE%
echo [OK] Starting Job Hunter...
echo [OK] Open browser at: http://localhost:%PORT%
echo      Press Ctrl+C to stop.
echo.
python app.py
pause
