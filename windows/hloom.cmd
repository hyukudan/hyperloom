@echo off
setlocal
rem -P: the current directory never shadows the installed package.
rem A versioned install names its active release in current.txt (install.ps1 -Activate).
set "_HYPERLOOM_PY="
if exist "%~dp0current.txt" (
  set /p _HYPERLOOM_RELEASE=<"%~dp0current.txt"
  call set "_HYPERLOOM_PY=%~dp0venvs\%%_HYPERLOOM_RELEASE%%\Scripts\python.exe"
)
if not defined _HYPERLOOM_PY set "_HYPERLOOM_PY=%~dp0venv\Scripts\python.exe"
if not exist "%_HYPERLOOM_PY%" set "_HYPERLOOM_PY=%LOCALAPPDATA%\hyperloom\venv\Scripts\python.exe"
if not exist "%_HYPERLOOM_PY%" (
  echo Hyperloom is not installed. Run windows\install.ps1. 1>&2
  exit /b 1
)
"%_HYPERLOOM_PY%" -P -m hyperloom %*
exit /b %errorlevel%
