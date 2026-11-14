@echo off
chcp 936 >nul
cd /d "%~dp0"

rem 带参数 = 原来的命令行菜单与子命令（menu / doctor / run 等）
if not "%~1"=="" (
  call "%~dp0start.bat" %*
  exit /b
)

if not exist ".venv\Scripts\pythonw.exe" (
  echo [错误] 运行环境缺失，请先双击 setup.bat 完成安装。
  pause
  exit /b 1
)

rem 无窗口拉起控制台：已有的窗口会被唤到前台，否则新开一个
start "" ".venv\Scripts\pythonw.exe" "tools\web_ui.py"
exit /b 0
