@echo off
setlocal
set "_TELAR_PY=%~dp0venv\Scripts\python.exe"
if not exist "%_TELAR_PY%" set "_TELAR_PY=%LOCALAPPDATA%\telar\venv\Scripts\python.exe"
if not exist "%_TELAR_PY%" (
  echo Telar is not installed. Run windows\install.ps1. 1>&2
  exit /b 1
)
"%_TELAR_PY%" -m telar %*
exit /b %errorlevel%
