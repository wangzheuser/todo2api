@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
  echo 未找到 Python，请先安装 Python 3.11 或更高版本。
  pause
  exit /b 1
)
python "%~dp0start_reg.py"
set "code=%errorlevel%"
echo.
if not "%code%"=="0" echo 启动器退出码：%code%
pause
exit /b %code%
