@echo off
title CineBot Sniper
cd /d "%~dp0"

echo ============================================================
echo  CineBot Sniper launcher
echo ============================================================
echo.

REM --- 1) Free port 8765 if a stale server is still holding it ---
set "KILLED="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
    echo Stopping stale server on port 8765 ^(PID %%a^)...
    taskkill /PID %%a /F >nul 2>&1
    set "KILLED=1"
)
if defined KILLED echo Port cleared. & echo.

REM --- 2) Open the browser a few seconds AFTER the server starts ---
start "" cmd /c "timeout /t 5 >nul & start http://127.0.0.1:8765"

REM --- 3) Start the server in this window (Ctrl+C to stop) ---
echo Starting server at http://127.0.0.1:8765 ...
echo Keep this window open. Press Ctrl+C to stop the app.
echo First page load takes ~10-30s (live guest login) - that is normal.
echo.
python -m cinebot.ui.app_v2

echo.
echo Server stopped. Press any key to close this window.
pause >nul
