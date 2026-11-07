# -*- coding: utf-8 -*-
"""本地助手保底修复：先唤起 WorkBuddy / TRAE 判断或维修，失败才转人工。

设计原则：
- 只把最小化的脱敏问题信息写到 `output/local_repair/`（该目录已被 .gitignore 排除）；
- 不读取、不复制 config.yaml / state / 登录态 / Cookie / 日志中的账号信息；
- 外部助手通过 JSON 响应文件明确回报 handled，程序不会把“已启动窗口”误当成“已修好”；
- 没找到 WorkBuddy/TRAE、命令失败或等待超时，才返回人工。
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

DEFAULT_TIMEOUT_S = 180


def _workbuddy_cli():
    """返回 (node.exe, codebuddy CLI) —— 优先用 WorkBuddy 自带的无界面 CLI。"""
    resources = Path(os.environ.get("WORKBUDDY_RESOURCES_PATH")
                     or (Path.home() / "AppData/Local/Programs/WorkBuddy/resources"))
    cli_root = resources / "app.asar.unpacked/cli"
    # WorkBuddy 5.9.x 的 bin/codebuddy 会 require 不存在的 ../dist/codebuddy；
    # 实际可用入口是 dist/codebuddy-lite-wb.mjs（已实测 --help / --version 正常）。
    cli = cli_root / "dist/codebuddy-lite-wb.mjs"
    if not cli.is_file():
        cli = cli_root / "bin/codebuddy"
    if not cli.is_file():
        return None, None
    node_candidates = []
    env_node = os.environ.get("CODEBUDDY_NODE_BIN")
    if env_node:
        node_candidates.append(Path(env_node))
    versions = Path.home() / ".workbuddy/binaries/node/versions"
    if versions.is_dir():
        node_candidates.extend(sorted(versions.glob("*/node.exe"), reverse=True))
    for node in node_candidates:
        try:
            if node.is_file():
                return node, cli
        except OSError:
            continue
    return None, None


def _safe_text(value, limit=600):
    """脱敏：去 URL、Bearer、token/key/cookie/enc 等查询参数值。"""
    text = str(value or "")
    text = re.sub(r"https?://\S+", "[URL_REDACTED]", text)
    text = re.sub(r"(?i)Bearer\s+\S+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)\b(token|key|secret|cookie|password|authorization|enc|jwt)\s*=\s*[^&\s;]+",
        r"\1=[REDACTED]",
        text,
    )
    return text[:limit]


def _app_candidates(cfg):
    """返回 [(名称, 可执行文件)]；优先用户配置，再自动探测本机安装。"""
    lr = cfg or {}
    candidates = []

    def add(name, value):
        if value:
            candidates.append((name, Path(str(value))))

    add("WorkBuddy", lr.get("workbuddy_exe"))
    add("TRAE", lr.get("trae_exe"))
    add("WorkBuddy", os.environ.get("WORKBUDDY_EXE"))
    add("TRAE", os.environ.get("TRAE_EXE"))

    resources = os.environ.get("WORKBUDDY_RESOURCES_PATH")
    if resources:
        add("WorkBuddy", Path(resources).parent / "WorkBuddy.exe")

    home = Path.home()
    add("WorkBuddy", home / "AppData/Local/Programs/WorkBuddy/WorkBuddy.exe")
    add("TRAE", home / "AppData/Local/Programs/Trae/Trae.exe")
    add("TRAE", home / "AppData/Local/Programs/Trae CN/Trae CN.exe")
    add("TRAE", Path("C:/Program Files/Trae/Trae.exe"))
    add("TRAE", Path("C:/Program Files (x86)/Trae/Trae.exe"))

    for name, path in candidates:
        try:
            if path.is_file():
                return name, path
        except OSError:
            continue
    return None, None


def _launch(parts, root, out_dir, log):
    """启动本地助手；不等待 GUI 退出，只把输出留在本地修复目录。"""
    log_path = out_dir / "launcher.log"
    kwargs = {
        "cwd": str(root),
        "stdin": subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    try:
        with log_path.open("ab") as out:
            kwargs["stdout"] = out
            proc = subprocess.Popen(parts, **kwargs)  # noqa: S603
        return proc
    except Exception as exc:  # noqa: BLE001
        log.warning(f"  本地助手启动失败: {str(exc)[:100]}")
        return None


def request_repair(cfg, log, project_root, chapter, reason, note="", timeout_s=None):
    """唤起本地助手判断/维修，返回 (是否处理成功, 说明)。

    响应文件协议（由 WorkBuddy / TRAE 写入 response 路径）：
        {"handled": true,  "note": "已修复..."}
        {"handled": false, "note": "需要人工..."}
    也兼容 {"status": "handled"} / {"status": "manual"}。
    """
    lr = cfg or {}
    if not bool(lr.get("enabled", True)):
        return False, "本地助手保底未启用"

    root = Path(project_root)
    out_dir = root / "output" / "local_repair"
    out_dir.mkdir(parents=True, exist_ok=True)
    key = re.sub(r"[^A-Za-z0-9_-]", "_", str(getattr(chapter, "id", "unknown")))[:32] or "unknown"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    request_path = out_dir / f"request_{stamp}_{key}.json"
    response_path = out_dir / f"response_{stamp}_{key}.json"
    prompt_path = out_dir / f"prompt_{stamp}_{key}.md"

    payload = {
        "purpose": "chaoxing-local-repair",
        "created_at": stamp,
        "project_root": str(root),
        "chapter_id": str(getattr(chapter, "id", "")),
        "chapter_title": _safe_text(getattr(chapter, "title", ""), 120),
        "hint": getattr(chapter, "hint", None),
        "reason": _safe_text(reason, 200),
        "note": _safe_text(note, 600),
        "privacy": "no-account-no-cookie-no-config",
    }
    request_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    prompt_path.write_text(
        "# 学习通自动学习：本地修复请求\n\n"
        "请先判断/尝试修复下面这个问题；能修就修，修好后写响应文件。\n\n"
        "约束：不要上传任何内容；不要读取或提交 config.yaml、state/、登录态、Cookie、日志文件；"
        "不要把账号、手机号、密钥写进任何文件。\n\n"
        f"- 章节：{payload['chapter_title']} (id={payload['chapter_id']})\n"
        f"- 问题：{payload['reason']}\n"
        f"- 说明：{payload['note']}\n"
        f"- 请求 JSON：{request_path}\n"
        f"- 响应 JSON 必须写到：{response_path}\n\n"
        "响应示例（修好）：\n\n"
        "```json\n{\"handled\": true, \"note\": \"已修复，可重试当前章节\"}\n```\n\n"
        "响应示例（需人工）：\n\n"
        "```json\n{\"handled\": false, \"note\": \"平台结构异常，需要人工\"}\n```\n",
        encoding="utf-8",
    )

    command = lr.get("command") or []
    cli_mode = False
    if command and not isinstance(command, (list, tuple)):
        return False, "local_repair.command 必须是列表，例如 [\"python\", \"repair.py\"]"
    if command:
        slots = {"{request}": str(request_path), "{response}": str(response_path),
                 "{prompt}": str(prompt_path)}

        def render(part):
            text = str(part)
            for key, value in slots.items():
                text = text.replace(key, value)
            return text

        parts = [render(part) for part in command]
        app_name = "配置的本地修复命令"
    else:
        node, cli = _workbuddy_cli()
        if node is not None:
            prompt_text = prompt_path.read_text(encoding="utf-8")
            parts = [
                str(node), str(cli), "-p",
                "--output-format", "json",
                "--permission-mode", "acceptEdits",
                "--tools", "Read,Edit,Write",
                "-y", "--max-turns", "8", "--no-session-persistence",
                prompt_text,
            ]
            app_name = "WorkBuddy CLI"
            cli_mode = True
        else:
            app_name, exe = _app_candidates(lr)
            if exe is None:
                return False, "未找到 WorkBuddy/TRAE 可执行文件，也未配置 local_repair.command"
            parts = [str(exe), str(prompt_path)]

    log.info(f"  问题保底：唤起 {app_name} 判断/维修…")
    proc = _launch(parts, root, out_dir, log)
    if proc is None:
        return False, "本地助手启动失败"

    # CLI 模式如果提前退出且没写响应，说明模型/登录/权限失败，不必干等 3 分钟。
    if cli_mode:
        for _ in range(8):
            if response_path.exists() or proc.poll() is not None:
                break
            time.sleep(0.5)
        if proc.poll() is not None and not response_path.exists():
            return False, f"WorkBuddy CLI 已退出（code={proc.returncode}）但未写回结果"

    raw_timeout = timeout_s if timeout_s is not None else lr.get("timeout_s", DEFAULT_TIMEOUT_S)
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError):
        timeout = float(DEFAULT_TIMEOUT_S)
    deadline = time.time() + max(1.0, timeout)
    while time.time() < deadline:
        if response_path.exists():
            try:
                obj = json.loads(response_path.read_text(encoding="utf-8") or "{}")
            except Exception:  # noqa: BLE001
                return False, "本地助手响应文件不是合法 JSON"
            if not isinstance(obj, dict):
                return False, "本地助手响应必须是 JSON 对象"
            status = str(obj.get("status") or "").lower()
            handled = bool(obj.get("handled") or obj.get("retry")) or status in ("handled", "retry", "done", "ok")
            detail = _safe_text(obj.get("note") or obj.get("reason") or status, 200)
            return handled, detail or ("本地助手已处理" if handled else "本地助手判定需人工")
        time.sleep(1.0)

    return False, f"本地助手 {int(timeout)} 秒内未给出处理结果"
