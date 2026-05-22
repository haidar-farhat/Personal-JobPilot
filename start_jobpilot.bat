@echo off
echo.
echo  ╭─ Starting JobPilot Mission Control ──────────────╮
echo  │                                                   │
echo  │   Scheduler + Dashboard booting...                │
echo  │                                                   │
echo  ╰───────────────────────────────────────────────────╯
echo.

cd /d "%~dp0"

:: Start the main scheduler in a new window
start "JobPilot Scheduler" cmd /k "venv\Scripts\activate && python scheduler.py"

:: Wait for scheduler to initialize
timeout /t 4 /nobreak >nul

:: Start Dashboard on port 7777 in a new window
start "JobPilot Dashboard" cmd /k "venv\Scripts\activate && python -m server.dashboard"

:: Wait for dashboard to come up
timeout /t 3 /nobreak >nul

:: Open dashboard in default browser
start http://localhost:7777

echo.
echo  Mission Control is live:
echo    ↳ Dashboard:  http://localhost:7777
echo    ↳ Scheduler:  running in background
echo.
echo  Press any key to close this launcher window.
echo  (Scheduler and Dashboard will keep running in their own windows.)
pause >nul
