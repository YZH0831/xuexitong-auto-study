@echo off
chcp 936 >nul
title 无人值守守护
cd /d "%~dp0"

if /i "%~1"=="stop" (
  if not exist "state" md "state"
  echo.> "state\keepalive.stop"
  echo 已发送停止信号，守护会在当前任务收尾后退出。
  echo 详细日志：logs\keepalive.log
  "%SystemRoot%\System32\timeout.exe" /t 3 >nul 2>nul
  exit /b 0
)

if not exist ".venv\Scripts\pythonw.exe" (
  echo [错误] 缺 pythonw.exe，请先双击 setup.bat 安装环境。
  pause
  exit /b 1
)
if not exist "tools\keepalive.py" (
  echo [错误] 缺 tools\keepalive.py
  pause
  exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "tools\keepalive.py"
echo.
echo 守护已在后台启动（无黑框），日志：logs\keepalive.log
echo 停止：在命令行执行 "%~dp0keepalive.bat" stop
echo.
"%SystemRoot%\System32\timeout.exe" /t 5 >nul 2>nul
exit /b 0
