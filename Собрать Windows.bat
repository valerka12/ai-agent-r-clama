@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" build_desktop.py
if errorlevel 1 (
  echo Build failed. Run the Windows setup script first.
  pause
  exit /b 1
)
echo Build complete. See the outputs folder. Distribute the entire AdResearchAgent folder.
pause
