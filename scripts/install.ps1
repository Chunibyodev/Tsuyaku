# Tsuyaku installer / updater for Windows.
#   install.bat  -> first install (Python environment, models, Firefox connection)
#   update.bat   -> latest code, newest yt-dlp, re-sync
param([switch]$Update)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
Write-Host ""
Write-Host "=== Tsuyaku setup ===" -ForegroundColor Cyan

# Firefox runs Tsuyaku from this folder, so it has to stay where it is installed.
if ($Root -like "$env:TEMP*") {
    Write-Host "Tsuyaku is running from inside the zip file. Unzip it first (right-click > Extract All...)," -ForegroundColor Red
    Write-Host "to a folder where it can stay, such as C:\Tsuyaku, and run install.bat from there." -ForegroundColor Red
    exit 1
}
$downloads = ""
try { $downloads = (New-Object -ComObject Shell.Application).NameSpace("shell:Downloads").Self.Path } catch {}
if (-not $Update -and $downloads -and $Root -like "$downloads*") {
    Write-Host "Tsuyaku is in your Downloads folder. Firefox will run it from here, so it must stay here." -ForegroundColor Yellow
    Write-Host "Better: close this window, move the folder somewhere permanent (such as C:\Tsuyaku) and run" -ForegroundColor Yellow
    Write-Host "install.bat there." -ForegroundColor Yellow
    Read-Host "Or press Enter to install it here anyway"
}

# A moved folder: its Python environment still points at the old place. Build it again.
$tool = Join-Path $Root ".venv\Scripts\tsuyaku.exe"
if (Test-Path $tool) {
    $works = $false
    try { & $tool --version *> $null; $works = ($LASTEXITCODE -eq 0) } catch {}
    if (-not $works) {
        Write-Host "The Tsuyaku folder was moved: rebuilding its Python environment..."
        Remove-Item (Join-Path $Root ".venv") -Recurse -Force
    }
}

# 1. uv (manages Python and packages; installs into your user profile)
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv..."
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

# 2. NVIDIA GPU? Then also install the CUDA 12 runtime libraries for speech recognition.
$hasNvidia = $false
try {
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($smi) { $gpus = & nvidia-smi -L; if ($gpus -match "GPU") { $hasNvidia = $true; Write-Host "GPU: $gpus" } }
} catch {}

if ($Update) {
    Write-Host "Updating yt-dlp (YouTube changes often)..."
    uv lock --upgrade-package yt-dlp --upgrade-package yt-dlp-ejs
}

Write-Host "Installing Python packages (first time: a few minutes)..."
if ($hasNvidia) { uv sync --extra cuda --no-dev } else { uv sync --no-dev }
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

# 3. Models and helper programs (speech model, llama.cpp + translation model; Deno for the command line)
Write-Host "Downloading models and helper programs (about 4-5 GB the first time)..."
uv run --no-sync tsuyaku setup
if ($LASTEXITCODE -ne 0) { Write-Host "Some downloads failed - the Tsuyaku button in Firefox can retry them." -ForegroundColor Yellow }

# 4. Let the Firefox extension start Tsuyaku (native messaging host; see docs\FIREFOX.md)
uv run --no-sync tsuyaku firefox
if ($LASTEXITCODE -ne 0) { Write-Host "Could not connect Firefox - run 'uv run tsuyaku firefox' later." -ForegroundColor Yellow }

uv run --no-sync tsuyaku doctor
Write-Host ""
Write-Host "Done! Install the Tsuyaku add-on in Firefox (README, step 3), then click its button and Turn on." -ForegroundColor Green
