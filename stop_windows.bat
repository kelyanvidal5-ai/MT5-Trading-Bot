@echo off
rem EMERGENCY STOP: blocks new entries and closes every position opened by the platform.
cd /d "%~dp0"
.venv\Scripts\python main.py stop "manual stop (stop_windows.bat)"
pause
