@echo off
setlocal
set "_HYPERLOOM_PY=%~dp0venv\Scripts\python.exe"
if not exist "%_HYPERLOOM_PY%" set "_HYPERLOOM_PY=%LOCALAPPDATA%\hyperloom\venv\Scripts\python.exe"
if not exist "%_HYPERLOOM_PY%" (
  echo Hyperloom is not installed. Run windows\install.ps1. 1>&2
  exit /b 1
)
"%_HYPERLOOM_PY%" -m hyperloom %*
exit /b %errorlevel%
