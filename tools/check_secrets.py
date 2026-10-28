# -*- coding: utf-8 -*-
"""提交前密钥/隐私扫描：防止把 API Key、Cookie、手机号带进公开仓库。

用法：
    python tools/check_secrets.py            # 扫描所有将被提交的文件
    python tools/check_secrets.py --all      # 连 .gitignore 里的文件也扫（自测用）
    python tools/check_secrets.py --staged   # 只扫 git 暂存区（pre-commit 钩子用）

退出码：0 = 干净；1 = 发现疑似泄露（会列出 文件:行号 与脱敏后的片段）。

配合 `.githooks/pre-commit` 使用（`bash tools/install_hooks.sh` 一键安装）。
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------- 规则 ----------
# 密钥：真实 DeepSeek/OpenAI Key 形如 sk-xxxxxxxxxxxxxxxx（长度较长）
KEY_RE = re.compile(r"sk-[A-Za-z0-9]{20,}")
# 允许出现在代码/文档里的测试占位符
KEY_PLACEHOLDERS = re.compile(r"sk-(x{3,}|test|abc|your|fake|new|placeholder|1234)", re.I)

TOKEN_RES = [
    ("疑似 API 密钥", KEY_RE),
    ("疑似 Cookie/会话", re.compile(r"\"(?:STUDENTCOOKIE|fid|_uid|uf|k8s-ed)\"\s*:\s*\"[^\"]{8,}\"")),
    ("疑似手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("疑似密码字段", re.compile(r"^\s*password\s*:\s*[\"']?[^\s\"'#]{4,}", re.M)),
]

# 这些文件/目录永远不需要扫描（要么被 gitignore，要么是二进制/第三方）
SKIP_DIRS = {".git", ".venv", "__pycache__", "logs", "output", "state", "dist", "build", "node_modules"}
SKIP_FILES = {"config.yaml", "config.yaml.bak", "question_pool.json", "answers.json"}
SKIP_SUFFIX = {".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".zip", ".whl", ".exe", ".dll",
               ".ttf", ".otf", ".woff", ".woff2", ".mp4", ".pdf"}

# 扫描到也不拦的文件（自检代码里本来就有假 Key）
ALLOW_LIST = {
    "tools/check_secrets.py",
    "data/font_map_table.json",   # 开源字形哈希表，键都是数字哈希，会误报成手机号
}


def mask(s):
    s = s.strip()
    return s[:4] + "***" + s[-2:] if len(s) > 8 else "***"


def iter_files(all_files=False, staged=False):
    if staged:
        out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
                             cwd=ROOT, capture_output=True, text=True)
        for line in out.stdout.splitlines():
            p = ROOT / line.strip()
            if p.is_file():
                yield p
        return
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIX:
            continue
        if not all_files and p.name in SKIP_FILES:
            continue
        yield p


def scan_file(path):
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return []
    hits = []
    rel = path.relative_to(ROOT).as_posix()
    if rel in ALLOW_LIST:
        return []
    for lineno, line in enumerate(text.splitlines(), 1):
        for label, rx in TOKEN_RES:
            for m in rx.finditer(line):
                got = m.group(0)
                if label == "疑似 API 密钥" and KEY_PLACEHOLDERS.search(got):
                    continue
                # 文档里明确写的占位/示例（如 sk-你的key / sk-xxxxxxxx）不算
                if label == "疑似 API 密钥" and ("你的" in line or "xxx" in got.lower()):
                    continue
                hits.append((rel, lineno, label, mask(got)))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="连被 gitignore 的文件也扫（自测）")
    ap.add_argument("--staged", action="store_true", help="只扫 git 暂存区（pre-commit 用）")
    args = ap.parse_args()

    hits, checked = [], 0
    for f in iter_files(all_files=args.all, staged=args.staged):
        checked += 1
        hits.extend(scan_file(f))

    if not hits:
        print(f"✔ 密钥扫描通过（检查了 {checked} 个文件，未发现密钥/Cookie/手机号）")
        return 0

    print("✘ 发现疑似敏感信息，已阻止提交：\n")
    for rel, lineno, label, masked in hits:
        print(f"  {rel}:{lineno}  [{label}]  {masked}")
    print("\n处理办法：")
    print("  1) 密钥/密码请放到 config.yaml（已被 .gitignore 排除），不要写进代码或文档；")
    print("  2) 如果确实要写示例，用占位符 sk-你的key；")
    print("  3) 误报可加到 tools/check_secrets.py 的 ALLOW_LIST。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
