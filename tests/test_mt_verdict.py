# -*- coding: utf-8 -*-
"""顶号判定回归测试：收到多终端提示后必须靠会话探测下结论，不再一见提示就暂停/重登。

不启动真实浏览器（用 __new__ + 替身），可与正在运行的实例并存而不触发真顶号。
运行：.venv\\Scripts\\python.exe tests\\test_mt_verdict.py
"""
import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "config.yaml"
if not CFG.exists():
    CFG = ROOT / "config.example.yaml"
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("mtverdict")

from cxlab.config import Config, load_config  # noqa: E402
from cxlab.runner import Runner  # noqa: E402
import cxlab.login as login_mod  # noqa: E402
import cxlab.course as course_mod  # noqa: E402
from cxlab.browser import BrowserSession  # noqa: E402


def make_runner(state_seq):
    cfg = Config(load_config(str(CFG)))
    cfg["safety"]["confirm_before_run"] = False
    cfg["login"]["auto_relogin"] = True
    cfg["login"]["check_interval_s"] = 0
    cfg["login"]["net_retry_delays_s"] = [0, 0]
    args = argparse.Namespace(select="", pick=False, list_courses=False, dry_run=False,
                              only="", headless=True, course="", config=str(CFG))
    r = Runner(cfg, log, args)
    r.course = type("C", (), {"name": "x", "course_id": "1", "clazzid": "2", "cpi": "3"})()
    r.session = type("S", (), {
        "multi_terminal_alert": True,
        "multi_terminal_detail": "22:00 于 谷歌内核浏览器设备",
        "multi_terminal_kicked": False,
        "probe_session": lambda self, *a, **k: state_seq.pop(0) if state_seq else "ok",
    })()
    return r


def fake_relogin(s, c, l):
    fake_relogin.n += 1
    return True
fake_relogin.n = 0
login_mod.relogin = fake_relogin
course_mod.enter_course = lambda s, c, cf, l: c

# 1) 提示 + 探测 ok → 未被顶号：不重登、标记清除、继续
r1 = make_runner(["ok"])
assert r1._ensure_session("测试") is True
assert r1.session.multi_terminal_alert is False
assert r1.session.multi_terminal_kicked is False
assert fake_relogin.n == 0, "会话正常时不应重新登录"
print("[1] 提示但会话正常 → 判定未被顶号，不重登、不空等  OK")

# 2) 提示 + 探测 logged_out → 确实被顶号：自动重登一次
r2 = make_runner(["logged_out", "ok"])
assert r2._ensure_session("测试") is True
assert r2.session.multi_terminal_kicked is True
assert fake_relogin.n == 1, "真被顶号应触发一次重登"
print("[2] 提示且会话失效 → 判定被顶号，自动重登  OK")

# 3) 提示 + 探测 offline → 网络问题，不重登
r3 = make_runner(["offline", "ok"])
assert r3._ensure_session("测试") is True
assert fake_relogin.n == 1, "网络问题不应触发重登"
print("[3] 提示但网络不通 → 判为网络问题，不重登  OK")

# 4) _on_dialog 解析时间与设备，且不再自称"确认被顶号"
class _Dialog:
    type = "alert"
    message = "您的账号于22:00在一台谷歌内核浏览器设备登入章节页面。多终端登录可能会被判定异常学习。"
    def accept(self):
        return None
b = BrowserSession.__new__(BrowserSession)
b.log = log
b.multi_terminal_alert = False
b.multi_terminal_at = 0.0
b.multi_terminal_detail = ""
b._on_dialog(_Dialog())
assert b.multi_terminal_alert is True
assert "22:00" in b.multi_terminal_detail and "谷歌内核浏览器" in b.multi_terminal_detail, b.multi_terminal_detail
print(f"[4] 弹窗解析出 时间/设备：{b.multi_terminal_detail}  OK")

# 5) keepalive 守护：虚惊(未被顶号)不冷却，真被顶号才冷却
import tempfile  # noqa: E402
import tools.keepalive as ka  # noqa: E402

def check_broken(name, tail_lines, expected):
    with tempfile.TemporaryDirectory() as td:
        ka.LOG_DIR = Path(td)
        (ka.LOG_DIR / "run_test.log").write_text("\n".join(tail_lines), encoding="utf-8")
        got = ka.session_broken(time.time() - 10)
        assert got == expected, f"{name}: 期望 {expected}，实际 {got}"
        print(f"[5.{name}] session_broken={got}  OK")

check_broken("虚惊不冷却", [
    "18:46:42 [页面对话框] alert: 您的账号于22:00在一台谷歌内核浏览器设备登入。多终端登录可能会被判定异常学习。",
    "18:46:43 [顶号判定] 结论=未被顶号；本机会话探测正常 → 继续任务",
], False)
check_broken("真顶号冷却", [
    "18:46:43 [顶号判定] 结论=确实被顶号；本机会话已失效 → 自动重新登录",
], True)

print("\n[OK] 顶号判定回归测试全部通过")
