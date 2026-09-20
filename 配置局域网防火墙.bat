@echo off
setlocal
set "BASE=%~dp0"
set "PY=%BASE%.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Python environment is missing. Run .\scripts\bootstrap.ps1 first.
  pause
  exit /b 1
)
net session >nul 2>&1
if not %errorlevel%==0 (
  echo Please right-click this file and choose Run as administrator.
  pause
  exit /b 1
)
cd /d "%BASE%"
"%PY%" -m mobile_server.firewall
if errorlevel 1 pause
