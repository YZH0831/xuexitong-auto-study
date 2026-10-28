# -*- coding: utf-8 -*-
"""推送到 GitHub（交互式，双击 publish.bat 即可）。

做的事：检查工作区 → 密钥扫描 → 设置 origin → push -u origin main。
Token 只在内存里用一次，不写入任何文件、不留在 git config 里（推送后会还原成不带 token 的地址）。

也可以非交互使用：
    python tools/publish.py --repo https://github.com/me/repo.git
    GITHUB_TOKEN=ghp_xxx python tools/publish.py --repo https://github.com/me/repo.git
"""
import argparse
import getpass
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, text=True, **kw)


def git(*args, check=False, quiet=True):
    r = run(["git", *args], capture_output=quiet)
    if check and r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：\n{(r.stdout or '') + (r.stderr or '')}")
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="", help="仓库地址，如 https://github.com/me/repo.git")
    ap.add_argument("--yes", action="store_true", help="跳过确认")
    ap.add_argument("--dry-run", action="store_true", help="只做前置检查，不配置远端也不推送")
    args = ap.parse_args()

    print("=" * 62)
    print(" 推送到 GitHub")
    print("=" * 62)

    if not (ROOT / ".git").exists():
        print("✘ 当前目录不是 git 仓库")
        return 1

    branch = git("rev-parse", "--abbrev-ref", "HEAD", check=True).stdout.strip()
    commits = git("rev-list", "--count", "HEAD", check=True).stdout.strip()
    print(f"当前分支：{branch}    本地提交数：{commits}")

    dirty = git("status", "--porcelain").stdout.strip()
    if dirty:
        print("⚠ 有未提交的改动（不影响推送已有提交，但建议先提交）：")
        for line in dirty.splitlines()[:10]:
            print("   " + line)

    print("\n① 密钥扫描 …")
    r = run([sys.executable, str(ROOT / "tools" / "check_secrets.py")], capture_output=True)
    out = (r.stdout or "") + (r.stderr or "")
    print("   " + out.strip().splitlines()[-1] if out.strip() else "")
    if r.returncode != 0:
        print(out)
        print("✘ 扫描未通过，已中止。")
        return 1

    repo = args.repo.strip()
    if not repo:
        print("\n先在 https://github.com/new 建一个**空仓库**")
        print("（不要勾 Add README / .gitignore / license，本地已经有了）")
        repo = input("仓库地址（HTTPS 或 SSH）：").strip()
    if not repo:
        print("✘ 没填仓库地址")
        return 1
    if not re.match(r"^(https://|git@)", repo):
        print("✘ 地址应以 https:// 或 git@ 开头")
        return 1

    if args.dry_run:
        print("\n✔ 前置检查通过（--dry-run：未配置远端、未推送）")
        print("   去掉 --dry-run 或直接双击 publish.bat 执行真正的推送")
        return 0

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if repo.startswith("https://") and not token:
        print("\nHTTPS 推送需要 Personal Access Token（GitHub 已不支持密码）")
        print("  创建：https://github.com/settings/tokens → classic → 勾 repo")
        token = getpass.getpass("粘贴 Token（输入不可见，直接回车=跳过用匿名推送）：").strip()

    print("\n② 配置远端 …")
    git("remote", "remove", "origin", quiet=True)
    git("remote", "add", "origin", repo, check=True)
    print("   origin → " + repo)

    push_url = repo
    if token and repo.startswith("https://"):
        push_url = "https://x-access-token:%s@%s" % (token, repo[len("https://"):])
        git("remote", "set-url", "origin", push_url)

    print("\n③ 推送（首次推送 1 MB 左右，慢的话等一会儿）…")
    r = run(["git", "push", "-u", "origin", branch], capture_output=True)
    ok = r.returncode == 0
    out = ((r.stdout or "") + (r.stderr or "")).replace(token or "\0", "***")
    print(out.strip())

    if token:
        git("remote", "set-url", "origin", repo)   # 还原，别把 token 留在 config 里

    if ok:
        web = re.sub(r"\.git$", "", repo).replace("git@github.com:", "https://github.com/")
        print("\n✔ 推送成功 → " + web)
        print("  建议：去仓库 Settings → 确认 Actions 跑通；再打个标签")
        print("        git tag v1.0.0 && git push --tags")
        return 0

    print("\n✘ 推送失败，常见原因：")
    print("   · 远程仓库不是空的（建时勾了 README）→ git pull --rebase origin %s 后再推" % branch)
    print("   · Token 权限不足/已过期 → 重新建一个勾 repo 的 classic token")
    print("   · SSH 没配 key → 改用 HTTPS + Token")
    return 2


if __name__ == "__main__":
    sys.exit(main())
