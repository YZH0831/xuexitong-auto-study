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
    "setup.bat", "start.bat", "SetDeepSeekKey.bat", "SetTiku.bat",
    "run.bat", "run_dry.bat", "publish.bat", "setup.sh", "start.sh",
    "README.md", "CHANGELOG.md", "LICENSE", "VERSION", ".gitignore",
    "data/font_map_table.json", "data/answers.json",
]
# 永远排除
EXCLUDE_NAMES = {"__pycache__", ".venv", ".git", "dist", "build", "node_modules"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".zip", ".bak", ".err"}


def _git_tracked():
    """git 跟踪的文件集合；不在 git 仓库里（如解压后的分发包）返回 None。"""
    if not (ROOT / ".git").exists():
        return None
    try:
        r = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        return {x for x in r.stdout.split("\0") if x}
    except (OSError, subprocess.SubprocessError):
        return None


def collect():
    tracked = _git_tracked()
    if tracked is None:
        print("  ! 未检测到 git 仓库，改用扩展名/命名规则排除私人文件")
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
            # 临时/调试文件（_foo.py、_脚本.sh）跳过，但 __init__.py / __main__.py 必须保留
            if p.name.startswith("_") and not p.name.startswith("__"):
                continue
            # 私人笔记（.gitignore 里的 *.local.md 等）：不进包，双保险
            if ".local." in p.name or p.suffix in {".local", ".private"}:
                continue
            # 有 git 时以「被跟踪」为准，.gitignore 里的私人文件自动排除
            if tracked is not None:
                if p.relative_to(ROOT).as_posix() not in tracked:
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

    # 自检：分发包必须完整可运行（曾因 "_" 排除规则漏掉全部 __init__.py）
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        missing = []
        for text in ("cxlab/__init__.py", "main.py", "requirements.txt", "config.example.yaml"):
            if not any(n.endswith("/" + text) for n in names):
                missing.append(text)
        # 私人笔记绝不能进包（曾因 rglob 绕过 .gitignore 而泄露开发日志）
        leaked = [n for n in names if ".local." in n or "DEVLOG" in n]
    if missing:
        print(f"✘ 自检失败：分发包缺文件 {missing}")
        return 2
    if leaked:
        print(f"✘ 自检失败：分发包含私人笔记 {leaked}")
        return 3

    print(f"③ 完成：{out}")
    print(f"   原始 {total/1024/1024:.1f} MB → 压缩后 {out.stat().st_size/1024/1024:.1f} MB")
    print(f"   解压后目录：{top}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
