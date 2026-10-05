@echo off
rem Installs Tsuyaku: Python environment, models, and the connection to Firefox.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1"
pause
