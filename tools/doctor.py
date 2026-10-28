# -*- coding: utf-8 -*-
"""环境自检：装完/换机器先跑这个，30 秒内知道哪里没配好。

    .venv\\Scripts\\python.exe tools\\doctor.py
"""
import json
import os
import shutil
import socket
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, WARN, BAD = "✔", "⚠", "✘"
results = []


def add(level, title, detail=""):
    results.append((level, title, detail))
    print(f"{level} {title}" + (f"  —— {detail}" if detail else ""))


def check_python():
    v = sys.version_info
    if v >= (3, 10):
        add(OK, f"Python {v.major}.{v.minor}.{v.micro}", "满足 >= 3.10")
    else:
        add(BAD, f"Python {v.major}.{v.minor}", "需要 3.10 及以上")


def check_deps():
    for mod, name in (("playwright", "playwright"), ("yaml", "PyYAML"), ("fontTools", "fonttools")):
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "")
            add(OK, f"依赖 {name}", ver or "已安装")
        except Exception:  # noqa: BLE001
            add(BAD, f"依赖 {name} 缺失", "运行 setup.bat 或 pip install -r requirements.txt")


def check_browser():
    """本项目用本机已安装的 Edge/Chrome（不下载 playwright 自带浏览器）。"""
    cands = []
    for exe in ("msedge", "chrome"):
        w = shutil.which(exe)
        if w:
            cands.append(Path(w))
    # 有些环境（沙箱/服务账号）没有 PROGRAMFILES，直接查标准路径兜底
    cands += [
        Path(r"C://Program Files (x86)//Microsoft//Edge//Application//msedge.exe"),
        Path(r"C://Program Files//Microsoft//Edge//Application//msedge.exe"),
        Path(r"C://Program Files//Google//Chrome//Application//chrome.exe"),
        Path(r"C://Program Files (x86)//Google//Chrome//Application//chrome.exe"),
    ]
    found = [str(p) for p in cands if p.exists()]
    if found:
        add(OK, "浏览器可用", Path(found[0]).name + " @ " + str(Path(found[0]).parent))
    else:
        add(WARN, "未在标准路径找到 Edge/Chrome",
            "以 Playwright 实际启动结果为准（见最后一项）")


def check_config():
    cfg_path = ROOT / "config.yaml"
    if not cfg_path.exists():
        add(WARN, "config.yaml 不存在", "将使用默认配置；建议复制 config.example.yaml 后修改")
        return None
    try:
        from cxlab.config import Config, load_config
        cfg = Config(load_config(str(cfg_path)))
    except Exception as exc:  # noqa: BLE001
        add(BAD, "config.yaml 解析失败", f"{type(exc).__name__}: {exc}")
        return None
    add(OK, "config.yaml 解析通过", "")
    # 关键项摘要
    course = cfg.course.get("name_contains") or "(未指定)"
    add(OK, "目标课程", str(course))
    acct = cfg.account or {}
    if (acct.get("phone") or "").strip():
        add(OK, "账号", f"{str(acct.get('phone'))[:3]}****{str(acct.get('phone'))[-4:]}（密码登录）")
    else:
        add(WARN, "未配置账号", "将使用扫码登录；无人值守建议填 account.phone/password")
    llm = (cfg.quiz.get("llm") or {})
    if llm.get("enabled"):
        add(OK if llm.get("api_key") else WARN, "大模型答题",
            f"{llm.get('model')} " + ("已填 Key" if llm.get("api_key") else "缺少 api_key"))
    else:
        add(WARN, "大模型答题未启用", "答不出的题会走搜题/兜底推理（按 K 可配置）")
    tiku = (cfg.quiz.get("tiku") or {})
    if tiku.get("enabled") and tiku.get("providers"):
        add(OK, "第三方题库", "、".join(str(p.get("type")) for p in tiku["providers"]))
    else:
        add(WARN, "第三方题库未启用", "接入 icodef/言溪 可显著提高答题准确率")
    if (cfg.quiz.get("online_search") or {}).get("enabled"):
        add(OK, "联网搜题", "已启用（会受搜索风控影响）")
    return cfg


def check_dirs():
    for name in ("logs", "output", "state", "data"):
        p = ROOT / name
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            add(OK, f"目录可写 {name}/", "")
        except Exception as exc:  # noqa: BLE001
            add(BAD, f"目录不可写 {name}/", str(exc))


def check_data():
    t = ROOT / "data" / "font_map_table.json"
    if t.exists():
        try:
            n = len(json.loads(t.read_text(encoding="utf-8")))
            add(OK, "加密字体表", f"{n} 条字形")
        except Exception as exc:  # noqa: BLE001
            add(BAD, "加密字体表损坏", str(exc))
    else:
        add(WARN, "缺少 data/font_map_table.json", "题面被字体混淆的测验将无法自动解析")


def check_network(timeout=6):
    for host, port, label in (("mooc1.chaoxing.com", 443, "学习通"),
                              ("api.deepseek.com", 443, "DeepSeek API")):
        try:
            socket.create_connection((host, port), timeout=timeout).close()
            add(OK, f"网络可达 {label}", host)
        except Exception as exc:  # noqa: BLE001
            add(WARN, f"网络不通 {label}", f"{type(exc).__name__}（可能需要代理/VPN）")


def check_playwright_driver():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(channel="msedge", headless=True)
            v = b.version
            b.close()
        add(OK, "Playwright 驱动 Edge 正常", f"Chromium {v}")
    except Exception as exc:  # noqa: BLE001
        add(BAD, "Playwright 启动浏览器失败", f"{type(exc).__name__}: {str(exc)[:120]}")


def main():
    print("=" * 60)
    print(" 学习通自动化助手 · 环境自检")
    print("=" * 60)
    check_python()
    check_deps()
    check_browser()
    check_config()
    check_dirs()
    check_data()
    check_network()
    check_playwright_driver()

    bad = sum(1 for lv, _, _ in results if lv == BAD)
    warn = sum(1 for lv, _, _ in results if lv == WARN)
    print("=" * 60)
    if bad:
        print(f"{BAD} 有 {bad} 项必须修复，{warn} 项建议完善。修好后重跑本命令。")
        return 1
    # 提示下一步：分发包里没有桌面入口 ChaoxingAutoStudy.bat，只认 setup/start
    win = os.name == "nt"
    ready = (ROOT / "config.yaml").exists()
    if win:
        cmd = "start.bat" if ready else "setup.bat（装完再双击 start.bat）"
    else:
        cmd = "./start.sh" if ready else "./setup.sh（装完再运行 ./start.sh）"
    print(f"{OK} 环境可用（{warn} 项建议项可忽略）。下一步：{'双击' if win else '运行'} {cmd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
