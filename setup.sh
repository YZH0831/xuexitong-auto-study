#!/usr/bin/env bash
# macOS / Linux / WSL 安装脚本（Windows 用户请双击 setup.bat）
set -e
cd "$(dirname "$0")"

echo "============================================================"
echo "            学习通自动化助手  v1.0.0  安装"
echo "============================================================"

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    v=$("$c" -c 'import sys;print("%d%d"%sys.version_info[:2])' 2>/dev/null || echo 0)
    if [ "$v" -ge 310 ] 2>/dev/null; then PY="$c"; break; fi
  fi
done
if [ -z "$PY" ]; then
  echo "✘ 未找到 Python 3.10+，请先安装：https://www.python.org/downloads/"
  exit 1
fi
echo "[1/4] 使用 $PY（$("$PY" -V 2>&1)）"

if [ ! -x ".venv/bin/python" ]; then
  echo "[2/4] 创建虚拟环境 .venv ..."
  "$PY" -m venv .venv
else
  echo "[2/4] 虚拟环境已存在，跳过"
fi

echo "[3/4] 安装依赖 ..."
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [ ! -f config.yaml ] && [ -f config.example.yaml ]; then
  cp config.example.yaml config.yaml
  echo "[4/4] 已生成 config.yaml（请按需修改）"
else
  echo "[4/4] config.yaml 已存在，保留原配置"
fi

echo ""
echo "正在自检 ..."
.venv/bin/python tools/doctor.py || true
echo ""
echo "安装完成。启动：./start.sh"
echo "提示：非 Windows 系统没有系统级 Edge 供 Playwright 调用时，请执行"
echo "      .venv/bin/python -m playwright install chromium"
