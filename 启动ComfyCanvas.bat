@echo off
setlocal
set "BASE=%~dp0"
set "PY=%BASE%.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Python environment is missing. Run .\scripts\bootstrap.ps1 first.
  pause
  exit /b 1
)
cd /d "%BASE%"
"%PY%" -m mobile_server.launcher
if errorlevel 1 pause
