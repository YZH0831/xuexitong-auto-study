@echo off
chcp 936 >nul
title Chaoxing Helper - LLM Key Setup
cd /d "%~dp0"

rem ---------- find a Python that has tkinter (the .venv has none) ----------
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

rem 错误日志路径（必须在下面的括号块之前设好：括号块内的 %ERRLOG% 是解析时展开的）
set "ERRLOG=state\set_key_gui.err"
if not exist "state" set "ERRLOG=set_key_gui.err"

if not defined PYW goto nopy

echo.
echo 正在打开密钥设置窗口 ...
echo   窗口会主动跳到最前面并闪烁任务栏；
echo   若仍没看到，请看任务栏里名为"学习通助手 · 大模型密钥设置"的窗口。
echo   关掉那个窗口后，这里会自动回到菜单。
echo.
rem 前台阻塞式运行：窗口不关，本窗口就一直等（不会出现"下一步没反应"）
%PYW% "tools\set_key_gui.py" 2>>"%ERRLOG%"
if errorlevel 1 (
  echo.
  echo [!] 设置窗口异常退出。
  echo     详情见 %ERRLOG% ，最后几行：
  if exist "%ERRLOG%" powershell -NoProfile -Command "Get-Content -Tail 8 -Encoding UTF8 '%ERRLOG%'" 2>nul
  echo.
  pause
  exit /b 1
)
exit /b 0

:nopy
echo.
echo [!] No Python with tkinter found on this machine.
echo.
echo     Option A: install Python from https://www.python.org/downloads/
echo               and keep the "tcl/tk and IDLE" component checked.
echo     Option B: set the key without GUI (venv python is enough):
echo               .venv\Scripts\python.exe tools\set_key_gui.py --set-key sk-xxxxxxxx
echo.
pause
exit /b 1
