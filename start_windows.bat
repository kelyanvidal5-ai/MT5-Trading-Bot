@echo off
rem Starts the engine and the dashboard (http://127.0.0.1:8050). Close the window or press Ctrl+C to stop;
rem open positions keep their broker-side stop-loss and take-profit.
cd /d "%~dp0"
if not exist .venv (
  echo Run setup_windows.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python main.py run
pause
