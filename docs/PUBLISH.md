# 发布到 GitHub

本地仓库已经准备好（`main` 分支、首个提交 v1.0.0、密钥扫描钩子已启用）。
剩下只有"建远程仓库 + 推上去"这一步，二选一即可。

## 方式 0：双击 `publish.bat`（最省事）

1. 打开 <https://github.com/new> 建一个**空仓库**（名字如 `chaoxing-auto-study`，
   **不要**勾 Add README / .gitignore / license）
2. 双击项目根目录的 `publish.bat`
3. 按提示粘贴仓库地址；若是 HTTPS 地址，再粘贴一次 Token（输入不回显）

脚本会先跑密钥扫描，通过后才推送；Token 只用一次，推完自动从 `git remote` 里抹掉。

## 方式 A：命令行脚本推送

1. 打开 <https://github.com/new>
   - Repository name：`chaoxing-auto-study`
   - 可见性：Public 或 Private 都行
   - **不要**勾选 "Add a README / .gitignore / license"（本地已经有了，勾了会冲突）
2. 在项目目录执行：

```bash
bash tools/publish.sh git@github.com:<你的用户名>/chaoxing-auto-study.git
```

没配 SSH key 的话用 HTTPS + Token：

```bash
GITHUB_TOKEN=ghp_你的token bash tools/publish.sh https://github.com/<你的用户名>/chaoxing-auto-study.git
```

Token 在 <https://github.com/settings/tokens> 创建（classic token 勾 `repo` 即可；
fine-grained 需要 `Contents: Read and write`）。**用完记得删掉那个 token。**

## 方式 B：手动三条命令

```bash
git remote add origin git@github.com:<你的用户名>/chaoxing-auto-study.git
git push -u origin main
git remote -v
```

## 推送前自检（脚本也会做）

```bash
.venv/Scripts/python.exe tools/check_secrets.py     # 必须 ✔ 通过
git status --short                                  # 不应出现 config.yaml / state/ / logs/
```

## 常见问题

| 现象 | 处理 |
|---|---|
| `Permission denied (publickey)` | 没配 SSH key，改用 HTTPS + Token，或先 `ssh-keygen` 并把公钥加到 GitHub |
| `remote: Support for password authentication was removed` | GitHub 已不支持密码推送，必须用 Token |
| `Updates were rejected` | 远程仓库初始化时勾了 README，先 `git pull --rebase origin main` 再推 |
| `x-access-token:...` 出现在 `git remote -v` | 推完把 URL 换回不带 token 的形式：`git remote set-url origin https://github.com/...` |
| 想改提交作者 | `git config user.name "你的名字"; git config user.email "你的邮箱"; git commit --amend --reset-author --no-edit` |

## 之后怎么维护版本

```bash
.venv/Scripts/python.exe tools/package.py     # 重新生成 dist/*.zip
git add -A && git commit -m "feat: xxx"
git push
git tag v1.0.0 && git push --tags             # 打标签（可选）
```
