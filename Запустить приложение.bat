@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Install dependencies first. See DESKTOP guide in README.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "desktop.py"
