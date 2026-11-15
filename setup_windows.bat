@echo off
rem One-time setup on Windows: creates the virtual environment, installs the
rem requirements and prepares the .env file. Double-click this file.
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python is not installed. Install Python 3.11 or newer from python.org and tick "Add python.exe to PATH".
  pause
  exit /b 1
)
if not exist .venv py -3 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Installation failed. Read the messages above.
  pause
  exit /b 1
)
if not exist .env copy .env.example .env >nul
echo.
.venv\Scripts\python main.py check
echo.
echo Setup finished. The platform is in PAPER mode. Next: read docs\DEMARRAGE_RAPIDE.md
pause
