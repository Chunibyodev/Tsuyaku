@echo off
rem Installs Tsuyaku: Python environment, models, and the connection to Firefox.
if not exist "%~dp0scripts\install.ps1" (
  echo Unzip Tsuyaku first ^(right-click the zip, Extract All...^) to a folder where it can stay,
  echo such as C:\Tsuyaku, then run install.bat from there.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1"
pause
