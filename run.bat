@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === running (QR login required) ===
if exist ".venv\Scripts\python.exe" (set PY=.venv\Scripts\python.exe) else (set PY=python)
%PY% main.py --config config.yaml
pause
