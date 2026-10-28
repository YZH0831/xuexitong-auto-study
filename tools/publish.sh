#!/bin/sh
# 一键推送到 GitHub（本地已 commit，只差 remote + push）
#
# 用法一（已建好仓库，推荐 SSH）：
#   bash tools/publish.sh git@github.com:<你的用户名>/chaoxing-auto-study.git
#
# 用法二（用 Personal Access Token 走 HTTPS）：
#   GITHUB_TOKEN=ghp_xxxxxxxx bash tools/publish.sh https://github.com/<你的用户名>/chaoxing-auto-study.git
#
# 脚本做的事：检查工作区 → 密钥扫描 → 设置/更新 origin → push -u origin main
set -e
cd "$(dirname "$0")/.." || exit 1

URL="$1"
if [ -z "$URL" ]; then
  echo "用法：bash tools/publish.sh <仓库地址>"
  echo "  例：bash tools/publish.sh git@github.com:me/chaoxing-auto-study.git"
  exit 1
fi

echo "① 检查工作区 …"
if [ -n "$(git status --porcelain)" ]; then
  echo "  ! 有未提交的改动，先提交："
  git status --short | head -20
  echo "  （若只是想推送已有提交，可忽略此提示继续）"
fi

echo "② 密钥扫描 …"
PY=".venv/Scripts/python.exe"
[ -x "$PY" ] || PY="python"
"$PY" tools/check_secrets.py || { echo "✘ 扫描未通过，已中止推送"; exit 1; }

echo "③ 配置远端 …"
if [ -n "$GITHUB_TOKEN" ]; then
  case "$URL" in
    https://*) URL="https://x-access-token:${GITHUB_TOKEN}@${URL#https://}" ;;
  esac
fi
if git remote | grep -q '^origin$'; then
  git remote set-url origin "$URL"
else
  git remote add origin "$URL"
fi
git remote -v | sed 's/x-access-token:[^@]*@/x-access-token:***@/'

echo "④ 推送 …"
git push -u origin main

echo "✔ 完成：$(git remote get-url origin | sed 's/x-access-token:[^@]*@/x-access-token:***@/')"
