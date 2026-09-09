@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title JobPilot - Start All

echo.
echo   =====================================================
echo    JobPilot - starting the full suite
echo      Ollama  +  Dashboard  +  Scheduler  (via watchdog)
echo   =====================================================
echo.

set "PY=venv\Scripts\python.exe"

:: ---------------------------------------------------------------- 1. venv
if not exist "%PY%" (
    echo   [setup] No virtualenv found - creating one ^(first run only^)...
    python -m venv venv
    if errorlevel 1 (
        echo   [ERROR] Could not create the virtualenv. Is Python on PATH?
        pause & exit /b 1
    )
    echo   [setup] Installing dependencies - this takes a few minutes...
    "%PY%" -m pip install --upgrade pip -q
    "%PY%" -m pip install -r requirements.txt
    "%PY%" -m playwright install chromium
) else (
    :: Cheap import check: a missing package here means requirements moved on.
    "%PY%" -c "import fastapi, sqlalchemy, playwright, dns.resolver" >nul 2>&1
    if errorlevel 1 (
        echo   [setup] Dependencies out of date - installing...
        "%PY%" -m pip install -r requirements.txt
    )
)

:: ------------------------------------------------------------- 2. config
for %%F in (applicant_profile base_resume settings) do (
    if not exist "config\%%F.yaml" (
        if exist "config\%%F.example.yaml" (
            echo   [setup] config\%%F.yaml missing - copying the example.
            copy /y "config\%%F.example.yaml" "config\%%F.yaml" >nul
            echo           ^^^> EDIT config\%%F.yaml before applying to anything.
        )
    )
)

:: --------------------------------------------------- 3. clear stale procs
:: A second watchdog would fight the first for port 7777. Only JobPilot's own
:: processes are matched - other Python on this machine is left alone.
echo   [start] Stopping any previous JobPilot processes...
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'watchdog\.py|scheduler\.py|uvicorn' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }" >nul 2>&1

:: ------------------------------------------------------------ 4. watchdog
:: One supervisor: it starts Ollama, the dashboard and the scheduler, then
:: health-checks and restarts whatever dies. Its own window shows the log;
:: closing that window stops supervision, not the services it already started.
echo   [start] Launching the watchdog...
start "JobPilot Watchdog" cmd /k "venv\Scripts\python.exe watchdog.py"

:: ------------------------------------------------- 5. wait for the health
:: Poll rather than sleeping a fixed number of seconds - a cold Ollama start
:: can take much longer than any timeout worth hard-coding.
echo   [wait ] Waiting for the dashboard on port 7777...
set /a tries=0
:waitloop
set /a tries+=1
powershell -NoProfile -Command ^
  "try { (Invoke-WebRequest -Uri 'http://127.0.0.1:7777/api/stats' -UseBasicParsing -TimeoutSec 3) | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 goto ready
if %tries% GEQ 40 (
    echo   [WARN ] Dashboard did not answer after ~2 minutes.
    echo           Check the watchdog window and logs\dashboard.log
    goto done
)
ping -n 4 127.0.0.1 >nul
goto waitloop

:ready
echo   [ok   ] Dashboard is up.
start http://127.0.0.1:7777

:done
echo.
echo   -----------------------------------------------------
echo    Dashboard : http://127.0.0.1:7777
echo    Watchdog  : its own window ^(keeps everything alive^)
echo    Logs      : logs\watchdog.log, dashboard.log, scheduler.log
echo   -----------------------------------------------------
echo.
echo    Auto-apply sends REAL applications when guardrails.enabled
echo    is true and dry_run is false - check the Agent screen.
echo.
echo    Run register_autostart.bat once to start this at every login.
echo.
pause
endlocal
