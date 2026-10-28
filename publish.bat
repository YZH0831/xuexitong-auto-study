@echo off
chcp 936 >nul
title 推送到 GitHub
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo ============================================================
echo   推送到 GitHub
echo ============================================================
echo   先在 https://github.com/new 建一个空仓库名（比如 chaoxing-auto-study）
echo   不要勾 Add README / .gitignore / license
echo.
"%PY%" tools\publish.py %*
echo.
pause
