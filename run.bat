@echo off
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Python is not installed. Install Python 3.10+ from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" in the installer, then run this file again.
  pause
  exit /b 1
)

if not exist .venv (
  echo Creating virtual environment...
  py -3 -m venv .venv
)
call .venv\Scripts\activate.bat

echo Installing requirements...
pip install -r requirements.txt
if errorlevel 1 (
  echo Install failed. Check your internet connection and try again.
  pause
  exit /b 1
)

if not exist .env (
  copy .env.example .env >nul
  echo.
  echo Opening .env: paste your bot token after DISCORD_TOKEN= , save, and close Notepad.
  notepad .env
)

echo Starting the bot. Leave this window open; closing it stops the bot.
python bot.py
pause
