@echo off
cd /d "%~dp0"
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
  echo Install Python 3.11 or newer from python.org first.
  pause
  exit /b 1
)
py -3 -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements-desktop.txt
if errorlevel 1 goto failed
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0cache\ms-playwright"
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto failed
echo Setup complete. Run the desktop launcher next.
pause
exit /b 0
:failed
echo Setup failed. Review the message above.
pause
exit /b 1
