@echo off
chcp 936 >nul
title 安装 / 修复运行环境
cd /d "%~dp0"

echo ============================================================
echo                学习通自动化助手  v1.0.0  安装
echo ============================================================
echo.

rem ---------- 1) 找 Python（>=3.10，优先 py 启动器） ----------
set "BASE="
where py >nul 2>nul && set "BASE=py -3"
if not defined BASE (
  where python >nul 2>nul && set "BASE=python"
)
if not defined BASE (
  echo [错误] 没有找到 Python。
  echo        请安装 Python 3.10+ ： https://www.python.org/downloads/
  echo        安装时务必勾选 "Add python.exe to PATH"。
  echo.
  pause
  exit /b 1
)
for /f "delims=" %%V in ('%BASE% -c "import sys;print(sys.version.split()[0])" 2^>nul') do set "PYVER=%%V"
echo [1/4] 已找到 Python %PYVER%

rem ---------- 2) 建虚拟环境 ----------
if not exist ".venv\Scripts\python.exe" (
  echo [2/4] 创建虚拟环境 .venv ...
  %BASE% -m venv .venv
  if errorlevel 1 (
    echo [错误] 创建虚拟环境失败。
    pause
    exit /b 1
  )
) else (
  echo [2/4] 虚拟环境已存在，跳过创建
)

rem ---------- 3) 装依赖（清华镜像，失败自动回退官方源） ----------
echo [3/4] 安装依赖（playwright / PyYAML / fonttools）...
.venv\Scripts\python.exe -m pip install -q --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
  echo      清华镜像失败，改用官方源重试...
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
if errorlevel 1 (
  echo [错误] 依赖安装失败，请检查网络后重跑本脚本。
  pause
  exit /b 1
)

rem ---------- 4) 生成配置 + 自检 ----------
if not exist "config.yaml" (
  if exist "config.example.yaml" (
    copy /y "config.example.yaml" "config.yaml" >nul
    echo [4/4] 已根据 config.example.yaml 生成 config.yaml ，请按需修改
  )
) else (
  echo [4/4] config.yaml 已存在，保留你原来的配置
)

echo.
echo 正在自检...
.venv\Scripts\python.exe tools\doctor.py
echo.
echo ============================================================
echo  安装完成。双击 start.bat 开始使用。
echo  提示：本项目不下载 playwright 自带浏览器，直接用系统的 Edge。
echo ============================================================
pause
