#!/usr/bin/env bash
# macOS / Linux / WSL 入口（Windows 用户请双击 start.bat）
cd "$(dirname "$0")" || exit 1
PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

case "$1" in
  run)    shift; exec "$PY" main.py --config config.yaml "$@" ;;
  dry)    shift; exec "$PY" main.py --config config.yaml --dry-run "$@" ;;
  quiz)   shift; exec "$PY" main.py --config config.yaml --only quiz "$@" ;;
  video)  shift; exec "$PY" main.py --config config.yaml --only video "$@" ;;
  pick)   shift; exec "$PY" main.py --config config.yaml --pick "$@" ;;
  list)   shift; exec "$PY" main.py --config config.yaml --list-courses "$@" ;;
  key)    shift; exec "$PY" tools/set_key_gui.py "$@" ;;
  tiku)   shift; exec "$PY" tools/tiku_gui.py "$@" ;;
  doctor) shift; exec "$PY" tools/doctor.py "$@" ;;
esac

cat <<'MENU'
============================================================
               学习通自动化助手  v1.0.0
============================================================
   [1] 试运行（只扫描，不学习）
   [2] 正式运行
   [3] 只刷视频/音频
   [4] 只做测验
   [5] 选择课程与课时
   [6] 列出我的课程
   [8] 编辑配置
   [K] 设置大模型密钥
   [T] 配置第三方题库
   [D] 环境自检
   [0] 退出
============================================================
MENU
printf "请输入序号后回车："
read -r opt
case "$opt" in
  1) exec "$PY" main.py --config config.yaml --dry-run ;;
  2) exec "$PY" main.py --config config.yaml ;;
  3) exec "$PY" main.py --config config.yaml --only video ;;
  4) exec "$PY" main.py --config config.yaml --only quiz ;;
  5) exec "$PY" main.py --config config.yaml --pick ;;
  6) exec "$PY" main.py --config config.yaml --list-courses ;;
  8) ${EDITOR:-nano} config.yaml ;;
  k|K) exec "$PY" tools/set_key_gui.py ;;
  t|T) exec "$PY" tools/tiku_gui.py ;;
  d|D) exec "$PY" tools/doctor.py ;;
  *) exit 0 ;;
esac
