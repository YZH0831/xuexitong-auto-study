@echo off
chcp 936 >nul
title Chaoxing Helper - Tiku Setup
cd /d "%~dp0"

set "PYW="
rem 两个候选根目录都试：某些终端/服务账号下 LOCALAPPDATA 可能为空
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%USERPROFILE%\AppData\Local\Programs\Python\Python3*") do (
  if exist "%%~D\python.exe" (
    "%%~D\python.exe" -c "import tkinter" >nul 2>&1
    if not errorlevel 1 set "PYW=%%~D\pythonw.exe"
  )
)
if not defined PYW (
  py -3 -c "import tkinter" >nul 2>&1
  if not errorlevel 1 set "PYW=py -3"
)

if "%~1"=="--dry" (
  if defined PYW (echo interpreter: %PYW%) else (echo interpreter: NOT FOUND)
  exit /b 0
)

set "ERRLOG=state\set_tiku_gui.err"
if not exist "state" set "ERRLOG=set_tiku_gui.err"

if not defined PYW goto nopy

echo.
echo 正在打开题库设置窗口 ...
echo   关掉那个窗口后，这里会自动回到菜单。
echo.
%PYW% "tools\tiku_gui.py" 2>>"%ERRLOG%"
if errorlevel 1 (
  echo.
  echo [!] 题库设置窗口异常退出，详情见 %ERRLOG%
  echo.
  pause
  exit /b 1
)
exit /b 0

:nopy
echo [!] 没有找到带 tkinter 的 Python，请先运行 setup.bat 安装 Python 3.10+。
pause
exit /b 1
