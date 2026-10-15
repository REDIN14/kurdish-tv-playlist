@echo off
rem Runs the Kurdish TV playlist service in this window (Ctrl+C stops it).
rem Reachable from this PC only. For phones/TVs on your network: scripts\start.cmd --host 0.0.0.0
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo The Python environment is missing. Run: powershell -File scripts\setup.ps1
  exit /b 1
)
".venv\Scripts\python.exe" -m kurdiptv serve %*
