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
