# -*- coding: utf-8 -*-
"""第三方题库配置界面（零依赖 Tkinter；和密钥界面一样，用带 tkinter 的系统 Python 跑）。

为什么单独做一个：做题准确率提升最大的一步是接题库接口，但让普通用户手改 YAML 的
`providers` 列表太劝退——这里给三个最常见 provider 的填空位，点保存就写回 config.yaml。

    py tools/set_tiku_gui.py            # 图形界面
    py tools/set_tiku_gui.py --show     # 命令行看当前配置
    py tools/set_tiku_gui.py --disable  # 关闭
"""
import json
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"

# provider 定义：键 = 配置里的 type
PROVIDERS = [
    ("icodef", "GO题 / 网课小工具", "https://cx.icodef.com/wyn-nb?v=4", "token",
     "关注公众号领取免费 token"),
    ("enncy", "言溪题库", "https://tk.enncy.cn/query", "token", "tk.enncy.cn 免费领"),
    ("tikuadapter", "tikuAdapter 自建聚合", "http://127.0.0.1:8060", "url",
     "自建服务地址，聚合 10+ 题库"),
]
SAMPLE_STEM = "违反安全保障义务责任属于（）"
SAMPLE_OPTS = [{"text": "公平责任", "letter": "A"},
               {"text": "特殊侵权责任", "letter": "B"},
               {"text": "过错推定责任", "letter": "C"},
               {"text": "连带责任", "letter": "D"}]


def target_config():
    return Path(os.environ.get("KEYGUI_CONFIG") or CONFIG)


# ---------------- config.yaml 读写（只动 quiz.tiku 整块） ----------------
def _find_block(lines, parent="quiz", key="tiku"):
    p_idx = p_ind = None
    for i, ln in enumerate(lines):
        s = ln.rstrip("\r\n")
        if s.strip() == parent + ":" and not s.startswith(" "):
            p_idx, p_ind = i, 0
            break
    if p_idx is None:
        return None, None, None
    end = len(lines)
    for j in range(p_idx + 1, len(lines)):
        s = lines[j].rstrip("\r\n")
        if not s.strip() or s.strip().startswith("#"):
            continue
        if len(s) - len(s.lstrip()) <= p_ind:
            end = j
            break
    for i in range(p_idx + 1, end):
        s = lines[i].rstrip("\r\n")
        if not s.strip() or s.strip().startswith("#"):
            continue
        indent = len(s) - len(s.lstrip())
        if s.strip().startswith(key + ":"):
            # 找该块结束
            stop = end
            for j in range(i + 1, end):
                t = lines[j].rstrip("\r\n")
                if not t.strip() or t.strip().startswith("#"):
                    continue
                if len(t) - len(t.lstrip()) <= indent:
                    stop = j
                    break
            return i, stop, indent
    return None, end, None


def render_tiku_block(enabled, providers, indent=2):
    pad = " " * indent
    out = [f"{pad}tiku:\n", f"{pad}  enabled: {'true' if enabled else 'false'}\n",
           f"{pad}  timeout_s: 12\n", f"{pad}  max_calls: 300\n",
           f"{pad}  min_stem_len: 6\n"]
    if not providers:
        out.append(f"{pad}  providers: []\n")
        return out
    out.append(f"{pad}  providers:\n")
    for p in providers:
        out.append(f"{pad}    - type: {p['type']}\n")
        if p.get("token"):
            out.append(f"{pad}      token: {json.dumps(p['token'], ensure_ascii=False)}\n")
        if p.get("url"):
            out.append(f"{pad}      url: {json.dumps(p['url'], ensure_ascii=False)}\n")
    return out


def patch_tiku(text, enabled, providers):
    """把 quiz.tiku 整块换成新内容（保留其它配置与注释）。"""
    lines = text.splitlines(keepends=True)
    start, stop, indent = _find_block(lines)
    block = render_tiku_block(enabled, providers, indent or 2)
    if start is None:
        # 没有 tiku 段 → 插到 quiz 第一个子项之前
        q_idx, q_stop, _ = _find_block(lines, "quiz", "__none__")
        qi = next((i for i, l in enumerate(lines) if l.rstrip("\r\n") == "quiz:"), None)
        if qi is None:
            raise RuntimeError("找不到 quiz: 段")
        lines[qi + 1:qi + 1] = ["\n"] + block
    else:
        lines[start:stop] = block
    return "".join(lines)


def save_tiku(enabled, providers, path=None):
    path = Path(path or target_config())
    text = path.read_text(encoding="utf-8")
    new = patch_tiku(text, enabled, providers)
    (path.with_name(path.name + ".bak")).write_text(text, encoding="utf-8")
    path.write_text(new, encoding="utf-8")
    return path


def load_tiku(path=None):
    """纯文本读 quiz.tiku（不依赖 PyYAML）。"""
    path = Path(path or target_config())
    if not path.exists():
        return {"enabled": False, "providers": []}
    lines = path.read_text(encoding="utf-8").splitlines()
    start, stop, indent = _find_block([l + "\n" for l in lines])
    info = {"enabled": False, "providers": []}
    if start is None:
        return info
    cur = None
    for ln in lines[start + 1:stop]:
        s = ln.strip()
        if s.startswith("enabled:"):
            info["enabled"] = s.split(":", 1)[1].split("#")[0].strip().lower() == "true"
        elif s.startswith("- type:"):
            cur = {"type": s.split(":", 1)[1].split("#")[0].strip().strip('"')}
            info["providers"].append(cur)
        elif cur is not None and (s.startswith("token:") or s.startswith("url:")):
            k, v = s.split(":", 1)
            v = v.split("#")[0].strip().strip('"').strip("'")
            cur[k] = v
    return info


# ---------------- 题库连通性测试 ----------------
def test_provider(ptype, token, url, log=print):
    """用一条样例题真查一次，看是否命中。"""
    sys.path.insert(0, str(ROOT))
    from cxlab.tiku import Tiku

    cfg = types.SimpleNamespace(quiz={"tiku": {
        "enabled": True, "timeout_s": 12, "max_calls": 5, "min_stem_len": 4,
        "providers": [{"type": ptype, "token": token, "url": url}],
    }})
    logger = types.SimpleNamespace(info=log, warning=log, debug=log)
    t = Tiku(cfg, logger)
    ans, kind, prov, note = t.query(SAMPLE_STEM, SAMPLE_OPTS, "single")
    return ans, kind, prov, note


# ---------------- 图形界面 ----------------
def main_ui():
    import tkinter as tk
    from tkinter import ttk

    cur = load_tiku()
    by_type = {p["type"]: p for p in cur["providers"]}

    root = tk.Tk()
    root.title("学习通助手 · 第三方题库设置")
    root.configure(bg="#f6f7f9")
    try:
        f = max(1.0, root.winfo_fpixels("1i") / 96.0)
    except Exception:  # noqa: BLE001
        f = 1.0
    root.geometry("%dx%d" % (min(int(620 * f), int(root.winfo_screenwidth() * 0.9)),
                            min(int(520 * f), int(root.winfo_screenheight() * 0.85))))
    lab = ("Microsoft YaHei UI", 9)
    small = ("Microsoft YaHei UI", 8)
    mono = ("Consolas", 9)

    tk.Label(root, text="第三方题库（可选，但能显著提高答题准确率）", bg="#f6f7f9",
             font=("Microsoft YaHei UI", 13, "bold"), fg="#1f2328").pack(anchor="w", padx=16, pady=(14, 2))
    tk.Label(root, text="题库返回的是结构化答案，比搜网页准得多。填一个就够用；多个按顺序回退、命中即止。",
             bg="#f6f7f9", font=small, fg="#6b7280", wraplength=580, justify="left").pack(anchor="w", padx=16)

    body = tk.Frame(root, bg="#ffffff", highlightbackground="#e3e6ea", highlightthickness=1)
    body.pack(fill="x", padx=16, pady=10)
    rows = []
    for i, (ptype, name, default_url, field, hint) in enumerate(PROVIDERS):
        curp = by_type.get(ptype, {})
        var_on = tk.BooleanVar(value=bool(curp))
        var_val = tk.StringVar(value=curp.get(field) or (default_url if field == "url" else ""))
        line = tk.Frame(body, bg="#ffffff")
        line.pack(fill="x", padx=10, pady=(8 if i == 0 else 4, 0))
        ttk.Checkbutton(line, text=f"{name}", variable=var_on).pack(side="left")
        ttk.Entry(line, textvariable=var_val, font=mono, width=34).pack(side="left", padx=8)
        tk.Label(line, text=hint, bg="#ffffff", font=small, fg="#9aa1ab").pack(side="left")
        rows.append((ptype, field, var_on, var_val))
    tk.Frame(body, bg="#ffffff", height=8).pack()

    var_enable = tk.BooleanVar(value=cur["enabled"] or not cur["providers"])
    ttk.Checkbutton(root, text="保存后立即启用（quiz.tiku.enabled = true）",
                    variable=var_enable).pack(anchor="w", padx=16)

    btns = tk.Frame(root, bg="#f6f7f9")
    btns.pack(fill="x", padx=16, pady=8)

    log_box = tk.Text(root, height=9, font=mono, bg="#ffffff", fg="#1f2328",
                      relief="flat", wrap="word", state="disabled", padx=8, pady=6,
                      highlightbackground="#e3e6ea", highlightthickness=1)
    log_box.pack(fill="both", expand=True, padx=16, pady=(0, 14))

    def log(msg):
        log_box.configure(state="normal")
        log_box.insert("end", str(msg).rstrip() + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")

    def collect():
        out = []
        for ptype, field, var_on, var_val in rows:
            if not var_on.get():
                continue
            val = var_val.get().strip()
            if not val and field == "token":
                continue
            item = {"type": ptype}
            item[field] = val
            if field == "token":
                # tikuadapter 只需要 url；icodef/enncy 只需要 token
                pass
            out.append(item)
        return out

    def on_test():
        provs = collect()
        if not provs:
            log("✘ 没有勾选任何题库")
            return
        log("→ 用样例题「%s」逐个试（每题消耗 1 次查询）…" % SAMPLE_STEM)
        for p in provs:
            try:
                ans, kind, prov, note = test_provider(
                    p["type"], p.get("token", ""), p.get("url", ""), log=lambda m: log("   " + str(m)))
                if ans:
                    log(f"   ✔ {prov} 命中 → {ans}（{kind}）")
                else:
                    log(f"   ✘ {prov} 未命中（{note or '题库里没有这道样例题'}）")
            except Exception as e:  # noqa: BLE001
                log(f"   ✘ {p['type']} 异常：{type(e).__name__}: {e}")
        log("提示：样例题查不到也正常，只要不报错就说明网络与 token 没问题。")

    def on_save():
        provs = collect()
        try:
            save_tiku(bool(var_enable.get()) and bool(provs), provs)
        except Exception as e:  # noqa: BLE001
            log("✘ 保存失败：%s" % e)
            return
        log("✔ 已写入 config.yaml（备份 config.yaml.bak）")
        log("  providers：%s    enabled=%s" % (
            "、".join(p["type"] for p in provs) or "（空）", bool(var_enable.get()) and bool(provs)))
        log("  下次启动 main.py / start.bat 生效。")

    ttk.Button(btns, text="测试连通性", width=12, command=on_test).pack(side="left")
    ttk.Button(btns, text="保存", width=8, command=on_save).pack(side="left", padx=6)
    ttk.Button(btns, text="打开配置文件", width=12,
               command=lambda: os.startfile(str(target_config()))  # noqa: S606
               if hasattr(os, "startfile") else None).pack(side="right")

    log("当前状态：%s" % ("已启用 " + "、".join(p['type'] for p in cur['providers'])
                          if cur["enabled"] and cur["providers"] else "未启用"))
    log("① 勾选要用的题库 → ② 填 token/地址 → ③ 点「测试连通性」 → ④ 点「保存」")
    root.mainloop()


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if "--show" in argv:
        info = load_tiku()
        print("配置文件：%s" % target_config())
        print("  enabled  = %s" % info["enabled"])
        for p in info["providers"]:
            print("  provider = %s %s" % (p["type"], {k: (v[:6] + "***" if k == "token" and v else v)
                                                     for k, v in p.items() if k != "type"}))
        if not info["providers"]:
            print("  providers = （空）")
        return 0
    if "--disable" in argv:
        save_tiku(False, [])
        print("已关闭第三方题库")
        return 0
    main_ui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
