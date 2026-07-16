@echo off
setlocal EnableDelayedExpansion
:: ─────────────────────────────────────────────
::  Job Hunter — Windows Setup
::  Run once after cloning:  setup.bat
:: ─────────────────────────────────────────────

echo.
echo   ╔══════════════════════════════════════╗
echo   ║      Job Hunter — Setup (Windows)    ║
echo   ╚══════════════════════════════════════╝
echo.

:: ── 1. Python check ──────────────────────────
echo [>>] Checking Python version...
set PYTHON=
where python >nul 2>&1 && set PYTHON=python
where py     >nul 2>&1 && set PYTHON=py
if "%PYTHON%"=="" (
    echo [X] Python not found.
    echo     Install Python 3.10+ from https://python.org
    echo     Make sure to check "Add Python to PATH" during install.
    pause & exit /b 1
)
for /f "tokens=2" %%v in ('%PYTHON% --version 2^>^&1') do set PY_VER=%%v
echo [OK] Python %PY_VER%

:: ── 2. Virtual environment ───────────────────
echo.
echo [>>] Setting up virtual environment...
if not exist ".venv" (
    %PYTHON% -m venv .venv
    if not exist ".venv\Scripts\activate.bat" (
        echo [X] Failed to create virtual environment.
        echo     Try running:  %PYTHON% -m pip install virtualenv
        pause & exit /b 1
    )
    echo [OK] Created .venv
) else (
    echo [OK] .venv already exists - skipping
)

:: ── 3. Activate ──────────────────────────────
call .venv\Scripts\activate.bat
echo [OK] Activated .venv

:: ── 4. Install dependencies ──────────────────
echo.
echo [>>] Installing Python dependencies...
python -m pip install --upgrade pip -q
python -m pip install -r requirements.txt -q
echo [OK] All packages installed

:: ── 5. .env file ─────────────────────────────
echo.
echo [>>] Setting up .env...
if not exist ".env" (
    copy .env.example .env >nul
    echo [OK] Created .env from .env.example
    echo [!] Open .env and add your GROQ_API_KEY
) else (
    echo [OK] .env already exists - skipping
)
findstr /C:"your_groq_api_key_here" .env >nul 2>&1
if not errorlevel 1 (
    echo [!] GROQ_API_KEY is not set - get a free key at https://console.groq.com
)

:: ── 6. config.json ───────────────────────────
echo.
echo [>>] Setting up config.json...
if not exist "config.json" (
    copy config.example.json config.json >nul
    echo [OK] Created config.json from config.example.json
    echo [!] Open config.json and update your name + job queries
) else (
    echo [OK] config.json already exists - skipping
)

:: ── 7. Chrome check ──────────────────────────
echo.
echo [>>] Checking for Chrome (needed for PDF generation)...
set CHROME_FOUND=0
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe"      set CHROME_FOUND=1
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set CHROME_FOUND=1
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"         set CHROME_FOUND=1
if "%CHROME_FOUND%"=="1" (
    echo [OK] Google Chrome found
) else (
    where chrome >nul 2>&1 && set CHROME_FOUND=1
    if "%CHROME_FOUND%"=="1" (
        echo [OK] Chrome found in PATH
    ) else (
        echo [!] Chrome not found - PDF downloads won't work
        echo     Install from: https://www.google.com/chrome/
    )
)

:: ── 8. Output dir ─────────────────────────────
if not exist "output" mkdir output
echo [OK] output\ ready

:: ── Done ──────────────────────────────────────
echo.
echo   ══════════════════════════════════════
echo    Setup complete!
echo   ══════════════════════════════════════
echo.
echo   Next steps:
echo.
echo   1. Add your Groq API key to .env
echo      Get free key: https://console.groq.com
echo.
echo   2. Replace base_resume.html with your resume
echo.
echo   3. Update config.json with your name + job queries
echo.
echo   4. Start the app:
echo        .venv\Scripts\activate
echo        python app.py
echo.
echo   5. Open browser: http://localhost:5000
echo.
pause
