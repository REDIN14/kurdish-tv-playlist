# One-time setup on a fresh machine: Python environment, dependencies, first playlist.
# (Already done on the machine this project was built on.)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$python = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $python) { throw "Python 3.11 or newer is required: https://www.python.org/downloads/" }
$version = [version](& python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))")
if ($version -lt [version]"3.11") { throw "Python $version found; 3.11 or newer is required." }

if (-not (Test-Path ".venv\Scripts\python.exe")) { & python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .\.venv\Scripts\python.exe -m pip install --quiet -e .

if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    Write-Warning "ffprobe not found. Streams are still checked, but their resolution is the playlist's claim, not a measurement. Install ffmpeg (winget install Gyan.FFmpeg) for full verification."
}

Write-Host "Building the first playlist (a few minutes)..."
& .\.venv\Scripts\python.exe -m kurdiptv refresh
Write-Host ""
Write-Host "Done. Start the service with scripts\start.cmd"
