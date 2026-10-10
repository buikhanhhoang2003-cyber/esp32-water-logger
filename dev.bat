@echo off
rem One entry point for the project: logger firmware, DDSU666 tool, local MQTT broker.
rem   dev.bat help     lists every command and option
if "%~1"=="" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev.ps1" help
    pause
    exit /b 0
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev.ps1" %*
set "code=%errorlevel%"
if not "%code%"=="0" pause
exit /b %code%
