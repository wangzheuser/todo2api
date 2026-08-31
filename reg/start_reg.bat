@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
  echo Python 3.11 or newer is required.
  pause
  exit /b 1
)
start "Todofor.ai Registration" python "%~dp0start_reg.py" --pause
exit /b 0
