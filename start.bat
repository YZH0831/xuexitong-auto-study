@echo off
chcp 936 >nul
title 学习通自动化助手
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [!] 还没安装运行环境，请先双击 setup.bat
  pause
  exit /b 1
)

if /i "%~1"=="run"   goto run
if /i "%~1"=="dry"   goto dry
if /i "%~1"=="quiz"  goto quiz
if /i "%~1"=="video" goto vid
if /i "%~1"=="pick"  goto pick
if /i "%~1"=="list"  goto list
if /i "%~1"=="key"   goto setkey
if /i "%~1"=="tiku"  goto settiku
if /i "%~1"=="doctor" goto doctor

:menu
set "LLMSTAT="
set "LLMTIP="
for /f "delims=" %%S in ('"%PY%" tools\set_key_gui.py --status-line 2^>nul') do (
  if not defined LLMSTAT (set "LLMSTAT=%%S") else (set "LLMTIP=%%S")
)
if not defined LLMSTAT set "LLMSTAT=[未配置] 按 K 填写密钥"
cls
echo ============================================================
echo                学习通自动化助手  v1.0.0
echo ============================================================
echo    大模型答题：%LLMSTAT%
if defined LLMTIP echo                %LLMTIP%
echo ------------------------------------------------------------
echo    [1] 试运行      ：只扫描章节和任务点，不真正学习
echo    [2] 正式运行    ：自动学习（已完成的任务点自动跳过）
echo    [3] 只刷视频/音频
echo    [4] 只做测验
echo    [5] 选择课程与课时：字母多选课程 → 选课时范围 → 确认
echo    [6] 列出我的课程（含课程编号）
echo    [7] 打开报告目录
echo    [8] 编辑配置    ：课程、课时、拟人化、安全限制等
echo    [9] 清除登录态  ：换账号或登录异常时用
echo    [K] 设置大模型密钥（DeepSeek / Kimi / 本地 Ollama）
echo    [T] 配置第三方题库（icodef / 言溪 / tikuAdapter，提高答题准确率）
echo    [D] 环境自检    ：依赖/浏览器/配置/网络一次查清
echo    [0] 退出
echo ============================================================
echo    提示：运行时会弹出浏览器窗口，需要时用学习通 APP 扫码登录；
echo          运行期间不要关闭或最小化该窗口。
echo ============================================================
set /p opt=请输入序号后回车：
if not defined opt goto end

if "%opt%"=="1" goto dry
if "%opt%"=="2" goto run
if "%opt%"=="3" goto vid
if "%opt%"=="4" goto quiz
if "%opt%"=="5" goto pick
if "%opt%"=="6" goto list
if "%opt%"=="7" goto rep
if "%opt%"=="8" goto cfg
if "%opt%"=="9" goto relogin
if /i "%opt%"=="k" goto setkey
if /i "%opt%"=="t" goto settiku
if /i "%opt%"=="d" goto doctor
if "%opt%"=="0" goto end
goto menu

:dry
echo.
echo === 试运行：扫描结构（可能需要扫码登录） ===
"%PY%" main.py --config config.yaml --dry-run
echo.
pause
goto menu

:run
echo.
"%PY%" tools\set_key_gui.py --has-key >nul 2>&1
if errorlevel 1 echo    [提示] 大模型未启用 —— 答不出的题走题库/搜题/兜底推理
echo === 正式运行 ===
"%PY%" main.py --config config.yaml
echo.
pause
goto menu

:vid
echo.
echo === 只刷视频/音频 ===
"%PY%" main.py --config config.yaml --only video
echo.
pause
goto menu

:quiz
echo.
echo === 只做章节测验 ===
"%PY%" main.py --config config.yaml --only quiz
echo.
pause
goto menu

:pick
echo.
echo === 选择课程与课时 ===
echo   步骤1：输入课程字母（可多选：A C F / A-C / 1,3 / all）
echo   步骤2：选择课时范围（1=全部 2=统一指定 3=逐门指定）
echo   步骤3：确认执行计划
"%PY%" main.py --config config.yaml --pick
echo.
pause
goto menu

:list
echo.
echo === 我的课程 ===
"%PY%" main.py --config config.yaml --list-courses
echo.
pause
goto menu

:rep
if not exist output mkdir output
start "" "%CD%\output"
goto menu

:cfg
notepad "%CD%\config.yaml"
goto menu

:relogin
if exist "state\storage_state.json" (
  del /f /q "state\storage_state.json"
  echo 已清除登录态，下次运行需重新扫码。
) else (
  echo 当前没有已保存的登录态。
)
echo.
pause
goto menu

:setkey
echo.
echo === 设置大模型答题密钥 ===
echo    （关掉设置窗口后会自动回到菜单）
call "%CD%\SetDeepSeekKey.bat"
if errorlevel 1 (
  echo.
  echo [!] 没能打开图形界面（缺少带 tkinter 的 Python）。
  echo     可改用命令行：
  echo     .venv\Scripts\python.exe tools\set_key_gui.py --set-key sk-你的密钥
  echo.
  pause
)
goto menu

:settiku
echo.
echo === 配置第三方题库 ===
echo    （关掉设置窗口后会自动回到菜单）
call "%~dp0SetTiku.bat"
if errorlevel 1 (
  echo.
  echo [!] 没能打开图形界面，可改用命令行：
  echo     .venv\Scripts\python.exe tools\tiku_gui.py --show
  echo.
  pause
)
goto menu

:doctor
echo.
echo === 环境自检 ===
"%PY%" tools\doctor.py
echo.
pause
goto menu

:end
exit /b 0
