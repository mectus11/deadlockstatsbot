@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
set /p SID=Your Steam ID: 
python probe.py %SID% > probe_output.txt 2>&1
type probe_output.txt
echo.
echo Saved to probe_output.txt
pause
