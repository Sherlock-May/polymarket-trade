@echo off
cd /d "%~dp0.."
set PYTHONUTF8=1
".venv\Scripts\python.exe" -u main.py run --record
pause
