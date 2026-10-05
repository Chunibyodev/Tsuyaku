@echo off
rem Gets the latest Tsuyaku + yt-dlp and re-syncs packages.
cd /d "%~dp0"
rem The last update bumped yt-dlp in uv.lock; drop that so the pull can't conflict.
where git >nul 2>nul && (git checkout -- uv.lock 2>nul & git pull --ff-only)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" -Update
pause
