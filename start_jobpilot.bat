@echo off
setlocal
cd /d "%~dp0"

echo.
echo   =====================================================
echo    Starting JobPilot Mission Control
echo    (watchdog keeps Ollama + Dashboard + Scheduler up)
echo   =====================================================
echo.

:: One supervisor to rule them all. The watchdog starts Ollama, the dashboard,
:: and the scheduler, then health-checks + restarts any that die. It runs in its
:: own window so you can watch the log; closing that window stops supervision
:: (the services it started keep running until they crash).
start "JobPilot Watchdog" cmd /k "venv\Scripts\python.exe watchdog.py"

:: Give the dashboard a few seconds to bind port 7777, then open it.
timeout /t 8 /nobreak >nul
start http://127.0.0.1:7777

echo.
echo   Mission Control is live:
echo     Dashboard:  http://127.0.0.1:7777
echo     Watchdog:   running in its own window
echo.
echo   Tip: run register_autostart.bat once to have this launch
echo        automatically every time you log in.
echo.
endlocal
