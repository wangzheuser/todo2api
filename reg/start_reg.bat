@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
  echo Python 3.11 or newer is required.
  pause
  exit /b 1
)
python "%~dp0start_reg.py"
set "exit_code=%errorlevel%"
echo(
if not "%exit_code%"=="0" echo Launcher exit code: %exit_code%
pause
exit /b %exit_code%
