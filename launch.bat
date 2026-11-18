@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File launch.ps1
echo.
pause
