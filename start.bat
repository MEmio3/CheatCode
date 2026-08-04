@echo off
title CineBot Sniper
cd /d "%~dp0"

echo ============================================================
echo  CineBot Sniper launcher
echo ============================================================
echo.

REM --- 0) Ensure Python is present ---
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found on PATH.
    echo Install Python 3.11+ from python.org - tick Add Python to PATH - and re-run.
    echo.
    pause
    exit /b 1
)

REM --- 1) Install dependencies if any are missing - first run or after a pull ---
python -c "import fastapi, uvicorn, playwright, curl_cffi, playwright_stealth, keyring, anyio, pydantic, dotenv, telegram" >nul 2>&1
if errorlevel 1 (
    echo Installing Python dependencies - one-time, please wait...
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    if errorlevel 1 goto install_failed
    echo Dependencies installed.
    echo.
)

REM --- 2) Ensure a Chromium browser binary exists for Playwright - fallback browser ---
python -m playwright install chromium

REM --- 3) Free port 8765 if a stale server is still holding it ---
set "KILLED="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
    echo Stopping stale server on port 8765 - PID %%a
    taskkill /PID %%a /F >nul 2>&1
    set "KILLED=1"
)
if defined KILLED echo Port cleared. & echo.

REM --- 4) Open the browser a few seconds AFTER the server starts ---
start "" cmd /c "timeout /t 5 >nul & start http://127.0.0.1:8765"

REM --- 5) Start the server in this window - Ctrl+C to stop ---
echo.
echo Starting server at http://127.0.0.1:8765 ...
echo Keep this window open. Press Ctrl+C to stop the app.
echo First page load takes about 10-30s - live guest login - that is normal.
echo.
python -m cinebot.ui.app_v2

echo.
echo Server stopped. Press any key to close this window.
pause >nul
exit /b

:install_failed
echo.
echo [ERROR] Dependency install failed. See messages above.
pause
exit /b 1
