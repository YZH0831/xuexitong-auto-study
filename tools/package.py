# -*- coding: utf-8 -*-
"""打包可分发版本（zip）。

    python tools/package.py            # 生成 dist/chaoxing-auto-study-v<版本>.zip
    python tools/package.py --list      # 只看会打进去哪些文件

特点：
- 白名单打包：只收代码、示例配置、文档、脚本；`config.yaml`、`state/`、`logs/`、
  `output/`、题库等**私人数据一律不进包**；
- 打包前自动跑密钥扫描（tools/check_secrets.py），有疑似 Key/Cookie 直接终止；
- zip 内是单层目录 `chaoxing-auto-study-v<版本>/`，解压即用。
"""
import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cxlab.version import __version__          # noqa: E402

DIST = ROOT / "dist"
NAME = "chaoxing-auto-study"

# 目录白名单（整目录递归收）
INCLUDE_DIRS = ["cxlab", "tools", "tests", "docs", ".githooks", ".github"]
# 文件白名单
INCLUDE_FILES = [
    "main.py", "requirements.txt", "config.example.yaml",
    "setup.bat", "start.bat", "SetDeepSeekKey.bat", "run.bat", "run_dry.bat", "publish.bat",
    "README.md", "CHANGELOG.md", "LICENSE", "VERSION", ".gitignore",
    "data/font_map_table.json", "data/answers.json",
]
# 永远排除
EXCLUDE_NAMES = {"__pycache__", ".venv", ".git", "dist", "build", "node_modules"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".zip", ".bak", ".err"}


def collect():
    files = []
    for f in INCLUDE_FILES:
        p = ROOT / f
        if p.exists():
            files.append(p)
        else:
            print(f"  ! 缺少 {f}（跳过）")
    for d in INCLUDE_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if any(part in EXCLUDE_NAMES for part in p.parts):
                continue
            if p.suffix.lower() in EXCLUDE_SUFFIX:
                continue
            if p.name.startswith("_"):          # 临时/调试文件
                continue
            files.append(p)
    return sorted(set(files))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="只列出文件，不打包")
    args = ap.parse_args()

    print("① 密钥扫描 …")
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "check_secrets.py")],
                       cwd=ROOT, capture_output=True, text=True)
    print("   " + (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else "")
    if r.returncode != 0:
        print(r.stdout)
        print("✘ 扫描未通过，已终止打包。")
        return 1

    files = collect()
    print(f"② 收集文件：{len(files)} 个")
    if args.list:
        for p in files:
            print("   ", p.relative_to(ROOT).as_posix())
        return 0

    DIST.mkdir(exist_ok=True)
    out = DIST / f"{NAME}-v{__version__}.zip"
    top = f"{NAME}-v{__version__}"
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            rel = p.relative_to(ROOT).as_posix()
            z.write(p, f"{top}/{rel}")
            total += p.stat().st_size
    # 版本文件也塞进去，方便解压后核对
    (ROOT / "VERSION").write_text(f"{__version__}\n", encoding="utf-8")
    print(f"③ 完成：{out}")
    print(f"   原始 {total/1024/1024:.1f} MB → 压缩后 {out.stat().st_size/1024/1024:.1f} MB")
    print(f"   解压后目录：{top}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
