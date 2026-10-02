@echo off
REM One-time setup: create .venv from a real Python (never the Store stub)
REM and install the two dependencies. Safe to re-run.
setlocal
cd /d "%~dp0"

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

if not defined PY (
  py -3.13 -m venv .venv 2>nul
  if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
)
if not defined PY (
  python -m venv .venv 2>nul
  if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
)

if not defined PY (
  echo Could not create a virtual environment.
  echo Install Python 3.10+ from python.org, then re-run setup.bat
  exit /b 1
)

echo Using %PY%
"%PY%" -m pip install --upgrade pip --quiet
"%PY%" -m pip install pywebview claude-agent-sdk pillow
if errorlevel 1 (
  echo Install failed. Check your network / proxy, then re-run setup.bat
  exit /b 1
)

"%PY%" -c "import webview, claude_agent_sdk; print('dependencies OK')"
echo.
echo Done. Launch the app with:  run.bat
endlocal
