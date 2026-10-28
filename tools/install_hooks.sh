#!/bin/sh
# 安装 git 钩子（只需跑一次）
cd "$(dirname "$0")/.." || exit 1
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit 2>/dev/null
echo "✔ 已启用提交前密钥扫描（core.hooksPath=.githooks）"
echo "  试试：git add -A && git commit -m test   —— 若含密钥会被拦下"
