@echo off
setlocal

:: Removes the JobPilot auto-start launcher created by register_autostart.bat.
:: This does NOT stop services that are currently running — it only stops
:: JobPilot from launching automatically at your next login.

set "VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\JobPilot.vbs"

if exist "%VBS%" (
  del "%VBS%"
  echo  Removed auto-start launcher. JobPilot will not start at login anymore.
) else (
  echo  No JobPilot auto-start launcher found ^(already removed^).
)

echo.
pause
endlocal
