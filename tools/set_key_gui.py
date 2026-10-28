# -*- coding: utf-8 -*-
"""大模型密钥设置界面（DeepSeek / Kimi / 本地 Ollama 通用）。

零依赖：只用标准库 tkinter + urllib，不需要 pip 装任何东西。
⚠ 本脚本要用「带 tkinter 的解释器」运行——项目 .venv 没有 tkinter，
   所以请双击项目里的 SetDeepSeekKey.bat（它会自动找带 tkinter 的 Python）。

它会做的事：
  1. 显示当前 config.yaml 里 quiz.llm 的状态（是否启用 / Key 尾号 / 模型 / 地址）
  2. 让你粘贴 Key，点「测试连接」真的发一次最小请求验证可用（约几个 token）
  3. 点「保存并启用」把值写回 config.yaml（保留全部注释，先备份 config.yaml.bak）

命令行用法（不开窗口，适合脚本/远程）：
    py tools/set_key_gui.py --set-key sk-xxxxxxxx
    py tools/set_key_gui.py --clear
    py tools/set_key_gui.py --show
    py tools/set_key_gui.py --test
    py tools/set_key_gui.py --selftest
"""
import argparse
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"

# (显示名, base_url, model, reasoning_effort)
PRESETS = [
    ("DeepSeek · deepseek-flash（V4.1-Flash，便宜快，推荐）", "https://api.deepseek.com/v1", "deepseek-flash", "low"),
    ("DeepSeek · deepseek-v4-pro（更强，贵一些）", "https://api.deepseek.com/v1", "deepseek-v4-pro", "high"),
    ("Kimi · kimi-k2.6", "https://api.moonshot.cn/v1", "kimi-k2.6", ""),
    ("Kimi · kimi-k3（需 reasoning_effort: low）", "https://api.moonshot.cn/v1", "kimi-k3", "low"),
    ("本地 Ollama · qwen2.5:7b（不花钱，Key 随便填）", "http://localhost:11434/v1", "qwen2.5:7b", ""),
]

BOOL_KEYS = {"enabled"}
RESET_KEYS = ("enabled", "base_url", "api_key", "model", "reasoning_effort")


# ============================ config.yaml 读写 ============================
def _indent_of(line):
    return len(line) - len(line.lstrip())


def _find_llm_blocks(lines):
    """定位所有 quiz: → llm: 段，返回 [(行号, 缩进)]。

    正常配置只有一个；若文件里不小心重复写了 llm 段（YAML 后者生效），
    这里会全部返回，改写时一并处理，避免只改了不生效的那一份。
    """
    blocks, quiz_indent, in_quiz = [], None, False
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)([A-Za-z_][\w]*)\s*:", ln)
        if not m:
            continue
        ind, key = _indent_of(ln), m.group(2)
        if key == "quiz" and ind == 0:
            in_quiz, quiz_indent = True, ind
            continue
        if not in_quiz:
            continue
        if ind <= quiz_indent:
            in_quiz = False
            continue
        if key == "llm":
            blocks.append((i, ind))
    return blocks


def _find_llm_block(lines):
    """兼容旧调用：返回最后一个 llm 段（YAML 语义上生效的那个）。"""
    blocks = _find_llm_blocks(lines)
    return blocks[-1] if blocks else (None, None)


def _strip_value(raw):
    """把 `"sk-xxx"   # 注释` 解析成纯值。"""
    s = raw.strip()
    if s[:1] in ('"', "'"):
        q = s[0]
        end = s.find(q, 1)
        if end > 0:
            return s[1:end]
    return s.split("#", 1)[0].strip()


def _split_comment(rest):
    """把冒号后的内容拆成 (值部分, 行尾注释)，引号内的 # 不算注释。"""
    quote = None
    for i, ch in enumerate(rest):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == "#":
            return rest[:i], rest[i:]
    return rest, ""


def _rewrite_line(raw, key, val):
    """保留缩进/冒号间距/行尾注释的列位置，只替换值。"""
    m = re.match(r"^(\s+)([A-Za-z_][\w]*)(\s*):(\s*)(.*)$", raw)
    ind, _k, gap1, gap2, rest = m.groups()
    value_part, comment = _split_comment(rest)
    prefix = f"{ind}{key}{gap1}:{gap2 or ' '}{val}"
    if comment:
        # 尽量让行尾注释仍从原来的列开始，保持整段对齐
        target = len(raw) - len(comment)
        return prefix + " " * max(1, target - len(prefix)) + comment
    return prefix + value_part[len(value_part.rstrip()):]


def parse_llm(text):
    """读取 quiz.llm 段的所有键值（纯文本解析，不依赖 PyYAML）。

    多个 llm 段时按 YAML 语义「后者覆盖前者」合并。
    """
    lines = text.splitlines()
    out = {}
    for idx, indent in _find_llm_blocks(lines):
        for ln in lines[idx + 1:]:
            m = re.match(r"^(\s+)([A-Za-z_][\w]*)\s*:\s*(.*)$", ln)
            if not m:
                continue
            if len(m.group(1)) <= indent:
                break
            out[m.group(2)] = _strip_value(m.group(3))
    return out


def _rewrite_block(lines, idx, indent, updates):
    """就地改写一个 llm 段里**已存在**的键。

    返回 (最后一条子行行号, 该段里出现的键集合, 子项缩进)。
    """
    present, last_child, child_indent = set(), idx, None
    i = idx + 1
    while i < len(lines):
        raw = lines[i].rstrip("\r\n")
        eol = lines[i][len(raw):]
        if not raw.strip() or raw.strip().startswith("#"):
            i += 1
            continue
        m = re.match(r"^(\s+)([A-Za-z_][\w]*)\s*:\s*(.*)$", raw)
        if not m or len(m.group(1)) <= indent:
            break
        child_indent = child_indent or len(m.group(1))
        last_child = i
        key = m.group(2)
        present.add(key)
        if key in updates:
            if key in BOOL_KEYS:
                val = "true" if updates[key] else "false"
            else:
                val = _quote(str(updates[key]))
            lines[i] = _rewrite_line(raw, key, val) + eol
        i += 1
    return last_child, present, child_indent


def patch_config_text(text, updates):
    """在保留注释的前提下改写 quiz.llm 段的指定键；缺失的键补到生效的那一段。

    多个 llm 段时全部改写（YAML 语义后者生效，补键只补最后一段）。
    返回 (新文本, 补写过的键集合)。
    """
    lines = text.splitlines(keepends=True)
    blocks = _find_llm_blocks(lines)
    if not blocks:
        raise RuntimeError("config.yaml 里找不到 quiz → llm 段，无法安全改写")

    last_child, present_last, child_indent = None, set(), None
    for idx, indent in blocks:
        last_child, present_last, child_indent = _rewrite_block(lines, idx, indent, updates)

    added = set()
    missing = [k for k in updates if k not in present_last]
    if missing:
        pad = " " * (child_indent or blocks[-1][1] + 2)
        if not lines[last_child].endswith("\n"):
            lines[last_child] += "\n"
        new_lines = []
        for k in missing:
            v = ("true" if updates[k] else "false") if k in BOOL_KEYS else _quote(str(updates[k]))
            new_lines.append(f"{pad}{k}: {v}\n")
            added.add(k)
        lines[last_child + 1:last_child + 1] = new_lines
    return "".join(lines), added


def _quote(s):
    """输出成 YAML 合法的双引号标量（json 的转义与 YAML 兼容）。"""
    return json.dumps(s, ensure_ascii=False)


def target_config():
    """实际要读写的配置文件（可用环境变量 KEYGUI_CONFIG 指定，便于测试）。"""
    return Path(os.environ.get("KEYGUI_CONFIG") or CONFIG)


def save_config(updates, config_path=None):
    path = Path(config_path or target_config())
    text = path.read_text(encoding="utf-8")
    new_text, added = patch_config_text(text, updates)
    backup = path.with_name(path.name + ".bak")
    backup.write_text(text, encoding="utf-8")
    path.write_text(new_text, encoding="utf-8")
    return backup, added


def load_llm():
    path = target_config()
    if not path.exists():
        raise RuntimeError(f"找不到配置文件：{path}")
    return parse_llm(path.read_text(encoding="utf-8"))


# ============================ 连通性测试 ============================
def query_balance(base_url, api_key, timeout=15):
    """查账户余额（DeepSeek /user/balance，免费接口，不消耗 token）。

    返回 (is_available, 可读说明, 明细dict)。这是判断"到底能不能用"的唯一权威依据，
    比控制台里可能过期的显示可靠（曾出现控制台看着还有钱、接口返回 -0.02 的情况）。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base or not (api_key or "").strip():
        return None, "缺少接口地址或密钥", {}
    req = urllib.request.Request(base + "/user/balance",
                                 headers={"Authorization": "Bearer " + api_key.strip(),
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, "该服务商没有余额查询接口（非 DeepSeek 官方端点）", {}
        return None, f"余额查询失败 HTTP {e.code}", {}
    except Exception as e:  # noqa: BLE001
        return None, f"余额查询失败：{type(e).__name__}", {}

    infos = data.get("balance_infos") or []
    cny = next((x for x in infos if (x.get("currency") or "").upper() == "CNY"), None)
    if not cny:
        return data.get("is_available"), "无人民币余额信息", {}
    total = cny.get("total_balance")
    granted = cny.get("granted_balance")
    topped = cny.get("topped_up_balance")
    ok = bool(data.get("is_available")) and float(total or 0) > 0
    msg = (f"余额 {total} 元（充值 {topped} / 赠送 {granted}）"
           + ("✔ 可用" if ok else "✘ 不可用 —— 需要充值"))
    return ok, msg, {"total": total, "granted": granted, "topped_up": topped}


def test_connection(base_url, model, api_key, reasoning_effort="", timeout=25):
    """发一次最小请求验证 Key 可用；返回 (ok, 说明)。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return False, "接口地址为空"
    if not (api_key or "").strip():
        return False, "密钥为空"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "只回复两个字：可用"}],
        "max_tokens": 16,
        "stream": False,
    }
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key.strip()},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
        content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
        usage = body.get("usage") or {}
        return True, ("连接成功 ✓  模型=%s  回复=「%s」  消耗=%s tokens"
                      % (body.get("model") or model, str(content).strip()[:20],
                         usage.get("total_tokens", "?")))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300].replace("\n", " ")
        hint = {401: "密钥无效/已失效/有多余空格",
                402: "账户余额不足，请到平台充值",
                403: "无权限（可能未实名或未开通该模型）",
                404: "接口地址或模型名不对（注意 base_url 要带 /v1）",
                429: "请求过于频繁或额度用尽"}.get(e.code, "")
        return False, f"HTTP {e.code}  {hint}\n  服务端返回：{detail}"
    except urllib.error.URLError as e:
        return False, (f"网络不通：{e.reason}\n"
                       "  若用本地 Ollama，请先 `ollama serve`；"
                       " 若开了系统代理，试试关掉代理再测。")
    except Exception as e:  # noqa: BLE001
        return False, f"异常：{type(e).__name__}: {e}"


# ============================ 图形界面 ============================
def mask(key):
    if not key:
        return "（未设置）"
    if len(key) <= 12:
        return key[:3] + "…" + key[-2:] if len(key) > 5 else "…"
    return key[:6] + "…" + key[-4:]


class App:
    BG = "#f6f7f9"
    CARD = "#ffffff"
    TXT = "#1f2328"
    SUB = "#6b7280"
    OK = "#0f7b3f"
    BAD = "#b3261e"
    ACCENT = "#1a5fd0"

    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root = root
        self.busy = False

        root.title("学习通助手 · 大模型密钥设置")
        root.configure(bg=self.BG)
        # 按屏幕 DPI 缩放初始尺寸（154% 缩放屏上不放大就会显得很挤）
        try:
            factor = max(1.0, root.winfo_fpixels("1i") / 96.0)
        except Exception:  # noqa: BLE001
            factor = 1.0
        w = min(int(640 * factor), int(root.winfo_screenwidth() * 0.9))
        h = min(int(540 * factor), int(root.winfo_screenheight() * 0.82))
        root.geometry(f"{w}x{h}")
        root.minsize(min(int(560 * factor), w), min(int(470 * factor), h))
        try:
            root.iconbitmap(default="")
        except Exception:  # noqa: BLE001
            pass

        f_title = ("Microsoft YaHei UI", 13, "bold")
        f_lab = ("Microsoft YaHei UI", 9)
        f_small = ("Microsoft YaHei UI", 8)
        f_mono = ("Consolas", 9)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:  # noqa: BLE001
            pass
        style.configure("TCombobox", font=f_lab)

        # ---- 标题 ----
        head = tk.Frame(root, bg=self.BG)
        head.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(head, text="大模型答题 · 密钥设置", font=f_title,
                 bg=self.BG, fg=self.TXT).pack(anchor="w")
        tk.Label(head,
                 text="填好 Key 后，答不出来的题会由大模型读题推理作答（不再只靠搜索）。"
                      "Key 只存在本机 config.yaml，不上传任何地方。",
                 font=f_small, bg=self.BG, fg=self.SUB, justify="left",
                 wraplength=580).pack(anchor="w", pady=(3, 0))

        # ---- 当前状态卡 ----
        card = tk.Frame(root, bg=self.CARD, highlightbackground="#e3e6ea",
                        highlightthickness=1)
        card.pack(fill="x", padx=16, pady=8)
        self.lbl_state = tk.Label(card, text="", font=f_lab, bg=self.CARD,
                                  fg=self.TXT, justify="left", anchor="w")
        self.lbl_state.pack(fill="x", padx=12, pady=10)

        # ---- 填写区 ----
        body = tk.Frame(root, bg=self.BG)
        body.pack(fill="x", padx=16)   # 只横向铺开，多余高度留给下方日志区
        body.columnconfigure(1, weight=1)

        tk.Label(body, text="服务 / 模型", font=f_lab, bg=self.BG,
                 fg=self.TXT).grid(row=0, column=0, sticky="w", pady=4)
        self.var_preset = tk.StringVar(value=PRESETS[0][0])
        self.cb = ttk.Combobox(body, textvariable=self.var_preset, state="readonly",
                               values=[p[0] for p in PRESETS], font=f_lab)
        self.cb.grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)
        self.cb.bind("<<ComboboxSelected>>", self._on_preset)

        tk.Label(body, text="API 密钥", font=f_lab, bg=self.BG,
                 fg=self.TXT).grid(row=1, column=0, sticky="w", pady=4)
        self.var_key = tk.StringVar()
        self.ent_key = ttk.Entry(body, textvariable=self.var_key, show="•", font=f_mono)
        self.ent_key.grid(row=1, column=1, sticky="ew", pady=4, padx=(0, 6))
        self.var_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text="显示", variable=self.var_show,
                        command=self._toggle_show).grid(row=1, column=2, sticky="w")

        tk.Label(body, text="接口地址", font=f_lab, bg=self.BG,
                 fg=self.TXT).grid(row=2, column=0, sticky="w", pady=4)
        self.var_base = tk.StringVar()
        ttk.Entry(body, textvariable=self.var_base, font=f_mono).grid(
            row=2, column=1, columnspan=2, sticky="ew", pady=4)

        tk.Label(body, text="模型名称", font=f_lab, bg=self.BG,
                 fg=self.TXT).grid(row=3, column=0, sticky="w", pady=4)
        self.var_model = tk.StringVar()
        ttk.Entry(body, textvariable=self.var_model, font=f_mono).grid(
            row=3, column=1, columnspan=2, sticky="ew", pady=4)

        self.var_enable = tk.BooleanVar(value=True)
        ttk.Checkbutton(body, text="保存后立即启用（把 quiz.llm.enabled 设为 true）",
                        variable=self.var_enable).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(8, 2))

        tk.Label(body, text="提示：DeepSeek 的 Key 形如 sk-xxxxxxxx（32 位）。"
                            "在 platform.deepseek.com → API keys 里创建，只显示一次。",
                 font=f_small, bg=self.BG, fg=self.SUB, justify="left",
                 wraplength=560).grid(row=5, column=0, columnspan=3,
                                      sticky="w", pady=(0, 6))

        # ---- 按钮 ----
        btns = tk.Frame(root, bg=self.BG)
        btns.pack(fill="x", padx=16, pady=(2, 6))
        self.b_test = ttk.Button(btns, text="测试连接", width=12, command=self.on_test)
        self.b_test.pack(side="left")
        self.b_save = ttk.Button(btns, text="保存并启用", width=12, command=self.on_save)
        self.b_save.pack(side="left", padx=6)
        self.b_clear = ttk.Button(btns, text="清除密钥", width=10, command=self.on_clear)
        self.b_clear.pack(side="left")
        self.b_open = ttk.Button(btns, text="打开配置文件", width=12, command=self.on_open_cfg)
        self.b_open.pack(side="right")

        # ---- 日志 ----
        logbox = tk.Frame(root, bg=self.CARD, highlightbackground="#e3e6ea",
                          highlightthickness=1)
        logbox.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        self.log_text = tk.Text(logbox, height=7, font=f_mono, bg=self.CARD,
                                fg=self.TXT, relief="flat", wrap="word",
                                state="disabled", padx=8, pady=6)
        sb = ttk.Scrollbar(logbox, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

        self.reload()
        self.log("三步走：① 粘贴密钥 → ② 点「测试连接」确认真能用 → ③ 点「保存并启用」", self.SUB)
        self.log("Key 在 platform.deepseek.com → API keys 创建（只显示一次）；保存后下次启动主程序即生效。", self.SUB)

    # ---- 内部 ----
    def log(self, msg, color=None):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg.rstrip() + "\n")
        if color:
            start = self.log_text.index("end-2l linestart")
            tag = f"c{color}"
            self.log_text.tag_configure(tag, foreground=color)
            self.log_text.tag_add(tag, start, "end-1c")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _toggle_show(self):
        self.ent_key.configure(show="" if self.var_show.get() else "•")

    def _on_preset(self, _evt=None):
        label = self.var_preset.get()
        for name, base, model, effort in PRESETS:
            if name == label:
                self.var_base.set(base)
                self.var_model.set(model)
                self._effort = effort
                self.log(f"已切换到预设：{model}（{base}）")
                return

    def reload(self):
        cur = load_llm()
        enabled = str(cur.get("enabled", "")).lower() in ("true", "yes", "1", "on")
        key = cur.get("api_key", "")
        self.var_base.set(cur.get("base_url") or "https://api.deepseek.com/v1")
        self.var_model.set(cur.get("model") or "deepseek-flash")
        self._effort = cur.get("reasoning_effort", "") or ""
        self.var_key.set(key)
        # 反查预设
        for name, base, model, _eff in PRESETS:
            if base == self.var_base.get() and model == self.var_model.get():
                self.var_preset.set(name)
                break

        if enabled and key:
            state = "✔ 已启用（注意账户余额：为 0 时调用会报 402，先点「测试连接」确认）"
            color = self.OK
        elif enabled and not key:
            state = "⚠ 已启用但密钥为空，实际不会生效"
            color = self.BAD
        elif key and not enabled:
            state = "○ 已填 Key，但未启用（保存时勾上「立即启用」即可）"
            color = self.SUB
        else:
            state = "○ 未启用 —— 目前只有「本地题库 + 联网搜题」两条路"
            color = self.SUB
        self.lbl_state.configure(
            text=f"{state}\n当前密钥：{mask(key)}      模型：{self.var_model.get()}\n"
                 f"接口地址：{self.var_base.get()}",
            fg=color)

    def _set_busy(self, busy):
        self.busy = busy
        st = "disabled" if busy else "normal"
        for b in (self.b_test, self.b_save, self.b_clear, self.b_open):
            b.configure(state=st)
        self.root.configure(cursor="watch" if busy else "")

    # ---- 按钮行为 ----
    def on_test(self):
        if self.busy:
            return
        key = self.var_key.get().strip()
        base = self.var_base.get().strip()
        model = self.var_model.get().strip()
        if not key:
            self.log("✘ 请先粘贴 API 密钥", self.BAD)
            return
        if not model:
            self.log("✘ 模型名称不能为空", self.BAD)
            return
        self._set_busy(True)
        self.log(f"→ 正在查询账户余额（免费接口）…", self.SUB)

        def work():
            ok_bal, bal_msg, _detail = query_balance(base, key)
            if ok_bal is False:
                self.root.after(0, lambda: self._after_test(
                    False, bal_msg + "\n  → 先到 platform.deepseek.com 充值，余额 > 0 才能调用",
                    show_key_hint=True))
                return
            if ok_bal:
                self.log("  " + bal_msg, self.OK)
            else:                       # 非 DeepSeek 端点（如本地 Ollama）查不到余额，跳过
                self.log("  " + bal_msg, self.SUB)
            self.root.after(0, lambda: self.log(f"→ 正在测试 {model} @ {base} …（约 1-10 秒）", self.SUB))
            ok, msg = test_connection(base, model, key, getattr(self, "_effort", ""))
            self.root.after(0, lambda: self._after_test(ok, msg))
        threading.Thread(target=work, daemon=True).start()

    def _after_test(self, ok, msg, show_key_hint=False):
        self._set_busy(False)
        self.log(("✔ " if ok else "✘ ") + msg, self.OK if ok else self.BAD)
        if show_key_hint:
            self.log("  （Key 本身没问题，只是余额不足；充完直接再点「测试连接」即可，不用重填）",
                     self.SUB)

    def on_save(self):
        if self.busy:
            return
        key = self.var_key.get().strip()
        base = self.var_base.get().strip()
        model = self.var_model.get().strip()
        if not key:
            self.log("✘ 密钥为空，没什么可保存的（要关闭大模型请点「清除密钥」）", self.BAD)
            return
        if re.search(r"\s", key):
            self.log("✘ 密钥里含有空格/换行，请重新复制（常见于多选了一行）", self.BAD)
            return
        if not key.startswith("sk-") and "localhost" not in base and "127.0.0.1" not in base:
            self.log("⚠ 这个 Key 不是 sk- 开头，DeepSeek/月之暗面的 Key 通常以 sk- 开头，已照原样保存",
                     self.SUB)
        updates = {
            "api_key": key,
            "base_url": base,
            "model": model,
            "reasoning_effort": getattr(self, "_effort", "") or "",
            "enabled": bool(self.var_enable.get()),
        }
        try:
            backup, added = save_config(updates)
        except Exception as e:  # noqa: BLE001
            self.log(f"✘ 写入配置文件失败：{e}", self.BAD)
            return
        self.log(f"✔ 已写入 {backup.name.replace('.bak', '')}（备份：{backup.name}）", self.OK)
        if added:
            self.log(f"  顺便补上了配置里缺的键：{', '.join(sorted(added))}", self.SUB)
        self.log("  下次启动 main.py / BAT 即生效。", self.SUB)
        self.reload()

    def on_clear(self):
        if self.busy:
            return
        from tkinter import messagebox
        if not messagebox.askyesno("确认", "清除已保存的密钥，并关闭大模型答题？"):
            return
        try:
            backup, _ = save_config({"api_key": "", "enabled": False})
        except Exception as e:  # noqa: BLE001
            self.log(f"✘ 清除失败：{e}", self.BAD)
            return
        self.var_key.set("")
        self.log(f"✔ 已清空密钥并关闭大模型（备份：{backup.name}）", self.OK)
        self.reload()

    def on_open_cfg(self):
        import os
        try:
            os.startfile(str(CONFIG))  # noqa: S606
        except Exception as e:  # noqa: BLE001
            self.log(f"✘ 打开失败：{e}", self.BAD)


# ============================ 入口 ============================
def _selftest():
    print("[1] tkinter 探测 …")
    import tkinter as tk
    print(f"    OK  tk {tk.TkVersion}")
    print("[2] 改写 config.example.yaml 的 quiz.llm（保留注释）…")
    sample = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    new, added = patch_config_text(sample, {"api_key": "sk-test123456", "enabled": True,
                                            "model": "deepseek-flash"})
    got = parse_llm(new)
    assert got.get("api_key") == "sk-test123456", got
    assert got.get("enabled") == "true", got
    assert got.get("model") == "deepseek-flash", got
    # 注释必须还在
    assert "# 或设环境变量" in new
    assert new.count("api_key") == sample.count("api_key"), "键被重复插入"
    print("    OK  值已改写、注释与其余键完好")
    print("[3] 缺键自动补齐 …")
    partial = "quiz:\n  llm:\n    enabled: true\n    model: m\n"
    new2, added2 = patch_config_text(partial, {"api_key": "sk-x", "base_url": "http://a/v1"})
    got2 = parse_llm(new2)
    assert got2.get("api_key") == "sk-x" and got2.get("base_url") == "http://a/v1", got2
    assert got2.get("model") == "m" and got2.get("enabled") == "true", got2
    print("    OK  缺的键补在正确缩进下")
    print("[4] 重复 llm 段一起改写（避免改了不生效的那份）…")
    dup = ('quiz:\n  llm:\n    api_key: "old"\n    model: a\n'
           '  llm:\n    api_key: "old"\n    model: b\n')
    new3, _ = patch_config_text(dup, {"api_key": "sk-new"})
    assert new3.count('"sk-new"') == 2, new3
    assert parse_llm(new3)["api_key"] == "sk-new"
    assert parse_llm(new3)["model"] == "b"
    print("    OK  两段都改了，且读取按后者生效")
    print("[5] 掩码 …")
    assert mask("sk-1234567890abcdef") == "sk-123…cdef", mask("sk-1234567890abcdef")
    print("    OK")
    print("[6] 建窗口（不进入事件循环）…")
    root = tk.Tk()
    root.withdraw()
    App(root)
    root.update()
    root.destroy()
    print("    OK  界面可正常构建")
    print("\n全部通过。")
    return 0


def _gbk_mask(key):
    """批处理里用的掩码：只用 ASCII，避免控制台编码问题。"""
    key = (key or "").strip()
    if not key:
        return "(未设置)"
    if len(key) <= 10:
        return key[:2] + "***"
    return key[:6] + "***" + key[-4:]


def status_lines():
    """返回 (状态行, 提示行)，给桌面 BAT 的菜单用（必须 GBK 能编码）。"""
    try:
        cur = load_llm()
    except Exception as e:  # noqa: BLE001
        return "[读取失败] " + str(e), ""
    enabled = str(cur.get("enabled", "")).strip().lower() in ("true", "yes", "1", "on")
    key = (cur.get("api_key") or "").strip()
    model = (cur.get("model") or "?").strip()
    if enabled and key:
        return f"[已启用] {model}  {_gbk_mask(key)}", ""
    if key:
        return (f"[未启用] 已填密钥 {_gbk_mask(key)}，但开关没打开",
                "按 K 打开后勾选「保存后立即启用」，再点保存")
    return ("[未配置] 目前只靠题库+搜题",
            "按 K 填 DeepSeek Key：答不出的题会由 AI 读题推理作答")


def _emit(lines):
    """按 GBK 输出（cmd 里 chcp 936）；编码不了就替换，绝不抛异常。"""
    try:
        sys.stdout.reconfigure(encoding="gbk", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    for ln in lines:
        if ln:
            print(ln)
    sys.stdout.flush()


def main(argv=None):
    ap = argparse.ArgumentParser(add_help=True, description="大模型密钥设置")
    ap.add_argument("--set-key", metavar="KEY", help="直接写入密钥并启用（不开窗口）")
    ap.add_argument("--model", default="deepseek-flash", help="配合 --set-key 使用的模型名")
    ap.add_argument("--base-url", default="https://api.deepseek.com/v1", help="接口地址")
    ap.add_argument("--clear", action="store_true", help="清空密钥并关闭大模型")
    ap.add_argument("--show", action="store_true", help="打印当前 quiz.llm 配置")
    ap.add_argument("--test", action="store_true", help="用当前配置发一次最小请求")
    ap.add_argument("--balance", action="store_true", help="只查余额（免费，不消耗 token）")
    ap.add_argument("--status-line", action="store_true",
                    help="给批处理用：输出状态行+提示行（GBK 安全）")
    ap.add_argument("--has-key", action="store_true",
                    help="已启用且已填 Key 时退出码 0，否则 1（供批处理判断）")
    ap.add_argument("--selftest", action="store_true", help="自检（不写真实配置）")
    args = ap.parse_args(argv)

    if args.status_line:
        _emit(status_lines())
        return 0
    if args.has_key:
        try:
            cur = load_llm()
        except Exception:  # noqa: BLE001
            return 1
        ok = (str(cur.get("enabled", "")).strip().lower() in ("true", "yes", "1", "on")
              and bool((cur.get("api_key") or "").strip()))
        return 0 if ok else 1
    if args.selftest:
        return _selftest()
    if args.show:
        cur = load_llm()
        print(f"配置文件：{target_config()}")
        for k in RESET_KEYS:
            v = cur.get(k, "")
            if k == "api_key":
                v = mask(v) if v else "（未设置）"
            print(f"  {k:18s} = {v!r}")
        return 0
    if args.clear:
        backup, _ = save_config({"api_key": "", "enabled": False})
        print(f"已清空密钥并关闭大模型；备份：{backup}")
        return 0
    if args.set_key:
        key = args.set_key.strip()
        backup, _ = save_config({"api_key": key, "base_url": args.base_url,
                                 "model": args.model, "enabled": True})
        print(f"已保存（{mask(key)}，模型 {args.model}）；备份：{backup}")
        ok, msg = test_connection(args.base_url, args.model, key)
        print(("✔ " if ok else "✘ ") + msg)
        return 0 if ok else 2
    if args.balance:
        cur = load_llm()
        ok, msg, detail = query_balance(cur.get("base_url", ""), cur.get("api_key", ""))
        print(("✔ " if ok else "✘ ") + msg)
        return 0 if ok else 2
    if args.test:
        cur = load_llm()
        ok, msg = test_connection(cur.get("base_url", ""), cur.get("model", ""),
                                  cur.get("api_key", ""), cur.get("reasoning_effort", ""))
        print(("✔ " if ok else "✘ ") + msg)
        return 0 if ok else 2

    # 默认：开窗口
    import tkinter as tk
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    try:
        root = tk.Tk()
        App(root)
        _bring_to_front(root)
        root.mainloop()
        return 0
    except Exception:  # noqa: BLE001
        # 双击 bat 时控制台在后台，异常必须落盘，否则窗口一闪就没了还查不到原因
        import traceback
        tb = traceback.format_exc()
        try:
            (ROOT / "state").mkdir(exist_ok=True)
            with open(ROOT / "state" / "set_key_gui.err", "a", encoding="utf-8") as f:
                f.write(tb + "\n")
        except Exception:  # noqa: BLE001
            pass
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, "设置窗口启动失败：\n\n" + tb[-800:], "学习通助手 · 密钥设置", 0x10)
        except Exception:  # noqa: BLE001
            pass
        return 3


def _bring_to_front(root):
    """把窗口顶到最前面并闪任务栏（从 bat 启动时容易藏在别的窗口后面）。"""
    def kick():
        try:
            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)
        except Exception:  # noqa: BLE001
            pass
        try:
            root.focus_force()
        except Exception:  # noqa: BLE001
            pass
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
            ctypes.windll.user32.FlashWindow(hwnd, True)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            pass
        # 400ms 后取消 topmost，避免一直压着别的窗口
        root.after(900, lambda: _un_top(root))

    root.after(120, kick)
    root.after(1200, kick)   # 有些机器第一次抢前台会被系统拒绝，再试一次


def _un_top(root):
    try:
        root.attributes("-topmost", False)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    sys.exit(main())
