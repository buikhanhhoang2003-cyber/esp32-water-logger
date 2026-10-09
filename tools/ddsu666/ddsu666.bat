@echo off
rem Build + flash the ESP32 RS485 bridge and start the DDSU666 app.
rem   ddsu666.bat                    build, flash (COM port auto-detected), open the app connected
rem   ddsu666.bat all -Port COM16
rem   ddsu666.bat build ^| flash ^| run ^| monitor ^| clean ^| test ^| ports   [-Port COMx]
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ddsu666.ps1" %*
set "code=%errorlevel%"
if not "%code%"=="0" pause
exit /b %code%
