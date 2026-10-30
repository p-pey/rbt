@echo off
REM One-click Windows build: creates a virtual environment, installs everything, builds and self-checks.
REM Usage: packaging\build_windows.bat [--onefile] [--test]
setlocal
cd /d "%~dp0\.."

where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher "py" not found. Install Python 3.12 from https://www.python.org/downloads/ ^(tick "Add to PATH"^).
  exit /b 1
)

if not exist .venv (
  py -3.12 -m venv .venv || py -3 -m venv .venv || exit /b 1
)
call .venv\Scripts\activate.bat || exit /b 1
python -m pip install --upgrade pip || exit /b 1
pip install -e ".[gui,mt5,secrets,build,dev]" || exit /b 1

python packaging\build.py %*
if errorlevel 1 (
  echo.
  echo BUILD FAILED - see the messages above.
  exit /b 1
)
echo.
echo Done. Your build is in the dist folder.
endlocal
