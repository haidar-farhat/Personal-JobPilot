@echo off
setlocal
cd /d "%~dp0"
title JobPilot - Stop All

echo.
echo   =====================================================
echo    JobPilot - stopping the full suite
echo   =====================================================
echo.

:: Order matters: the watchdog's whole job is restarting things that die, so
:: it has to go FIRST. Kill the dashboard while the watchdog is alive and it
:: is simply started again a few seconds later.
echo   [stop ] Watchdog ^(supervisor^)...
powershell -NoProfile -Command ^
  "$n=0; Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'watchdog\.py' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $n++ } catch {} }; Write-Host ('          stopped ' + $n)"

echo   [stop ] Dashboard + scheduler...
powershell -NoProfile -Command ^
  "$n=0; Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'scheduler\.py|uvicorn|jobpilot' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $n++ } catch {} }; Write-Host ('          stopped ' + $n)"

:: Ollama is left running on purpose - it is a shared local service and other
:: things on this machine may be using it. Pass /ollama to stop it too.
if /i "%~1"=="/ollama" (
    echo   [stop ] Ollama...
    powershell -NoProfile -Command ^
      "$n=0; Get-Process -Name 'ollama','ollama app' -ErrorAction SilentlyContinue | ForEach-Object { try { Stop-Process -Id $_.Id -Force -ErrorAction Stop; $n++ } catch {} }; Write-Host ('          stopped ' + $n)"
) else (
    echo   [keep ] Ollama left running ^(run "stop_all.bat /ollama" to stop it too^).
)

:: Any leftover watchdog console windows.
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process -Filter \"Name='cmd.exe'\" | Where-Object { $_.CommandLine -match 'watchdog\.py' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }" >nul 2>&1

:: ------------------------------------------------------------- verify
ping -n 3 127.0.0.1 >nul
powershell -NoProfile -Command ^
  "try { (Invoke-WebRequest -Uri 'http://127.0.0.1:7777/api/stats' -UseBasicParsing -TimeoutSec 3) | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [ok   ] Port 7777 is closed - JobPilot is stopped.
) else (
    echo.
    echo   [WARN ] Something is still answering on port 7777.
    echo           It may be a JobPilot started outside this folder.
)

echo.
echo   Nothing was deleted - the database, resumes and logs are untouched.
echo   Start again with start_all.bat
echo.
pause
endlocal
