@echo off
REM Launch Claude Code GUI using the project venv when it exists.
setlocal
cd /d "%~dp0"

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

if "%PY%"=="python" if not exist ".venv\Scripts\python.exe" (
  echo Tip: run setup.bat first ^(one time^) to create .venv and install dependencies.
  echo Trying your system python ...
)

"%PY%" app.py %*
endlocal
