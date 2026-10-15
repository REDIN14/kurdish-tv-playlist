@echo off
rem Refreshes the playlist once and prints the report. Add --force to ignore cached pages and re-probe everything.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m kurdiptv refresh %*
