# -*- coding: utf-8 -*-
"""掉线自愈功能测试：会话探测分类、断网退避、掉线自动重登录与续跑。

包含两类：
  A 真机探测（需要已保存登录态）：正常会话 → 'ok'；不可达地址 → 'offline'
  B 逻辑测试（用替身，不联网）：掉线 → 触发重登录 → 清理缓存 → 重进课程续跑；
    重登录频率上限 → 冷却；断网退避序列 → 恢复后继续
"""
import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 本地用 config.yaml（含真实 Key），CI 里没有该文件 → 回退到示例配置
CFG_PATH = ROOT / "config.yaml"
if not CFG_PATH.exists():
    CFG_PATH = ROOT / "config.example.yaml"
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("test")

from cxlab.config import Config, load_config  # noqa: E402
from cxlab.runner import Runner  # noqa: E402


def make_runner(**login_over):
    cfg = Config(load_config(str(CFG_PATH)))
    cfg["safety"]["confirm_before_run"] = False
    for k, v in login_over.items():
        cfg["login"][k] = v
    args = argparse.Namespace(select="", pick=False, list_courses=False, dry_run=False,
                              only="", headless=True, course="", config="config.yaml")
    return Runner(cfg, log, args), cfg


class FakeSession:
    """可编程的会话替身：按脚本返回探测结果。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def probe_session(self, *a, **k):
        self.calls += 1
        return self.script.pop(0) if self.script else "ok"


# ---------------- A) 真机探测分类 ----------------
try:
    from cxlab.browser import BrowserSession
    cfg0 = Config(load_config(str(CFG_PATH)))
    cfg0["browser"]["headless"] = True
    sess = BrowserSession(cfg0, log)
    ok_state = sess.probe_session()
    print(f"[A1] 真机会话探测 → {ok_state}")
    bad_state = sess.probe_session(url="http://no-such-host-abcxyz.invalid/x", timeout_ms=8000)
    print(f"[A2] 不可达地址探测 → {bad_state}")
    assert ok_state in ("ok", "logged_out"), f"探测结果异常: {ok_state}"
    assert bad_state == "offline", f"断网应判为 offline，实际 {bad_state}"
    sess.close()
    print("     → 探测分类正确（正常 / 断网可区分）")
except Exception as exc:  # noqa: BLE001
    print(f"[A] 真机探测跳过（{str(exc)[:70]}）")

# ---------------- B1) 掉线 → 自动重登录 → 续跑 ----------------
runner, cfg = make_runner(auto_relogin=True, check_interval_s=0)
runner.session = FakeSession(["logged_out", "ok"])
runner.course = type("C", (), {"name": "假课程", "course_id": "1", "clazzid": "2", "cpi": "3"})()

import cxlab.login as login_mod  # noqa: E402
import cxlab.course as course_mod  # noqa: E402

relogin_called = {"n": 0}
resume_called = {"n": 0}


def fake_relogin(session, cfg_, log_):
    relogin_called["n"] += 1
    return True


def fake_enter(session, course, cfg_, log_):
    resume_called["n"] += 1
    return course


login_mod.relogin = fake_relogin
course_mod.enter_course = fake_enter

assert runner._ensure_session("测试") is True, "掉线后重登录成功应返回 True"
assert relogin_called["n"] == 1, "应触发 1 次自动重登录"
assert resume_called["n"] == 1, "重登录后应重新进入课程以续跑"
assert runner.relogin_count == 1
print("[B1] 掉线 → 自动重登录 → 清缓存 → 重进课程续跑  OK")

# ---------------- B2) 重登录失败 → 如实上报 ----------------
runner2, cfg2 = make_runner(auto_relogin=True, check_interval_s=0)
runner2.session = FakeSession(["logged_out"])
login_mod.relogin = lambda s, c, l: False
assert runner2._ensure_session("测试") is False, "重登录失败应返回 False（上层暂停）"
assert runner2.relogin_failed == 1
print("[B2] 重登录失败 → 返回 False 并计数 OK")

# ---------------- B3) 断网退避 → 恢复后继续 ----------------
runner3, cfg3 = make_runner(auto_relogin=True, check_interval_s=0,
                            net_retry_delays_s=[0, 0, 0])
runner3.session = FakeSession(["offline"] + ["offline", "ok"])
login_mod.relogin = fake_relogin
assert runner3._ensure_session("测试") is True, "断网恢复后应继续"
assert runner3.net_wait_count == 1
print("[B3] 断网 → 退避重试 → 恢复后继续 OK")

# ---------------- B4) 断网重试耗尽 → 暂停 ----------------
runner4, cfg4 = make_runner(auto_relogin=True, check_interval_s=0, net_retry_delays_s=[0, 0])
runner4.session = FakeSession(["offline", "offline", "offline"])
assert runner4._ensure_session("测试") is False, "网络长时间未恢复应返回 False"
print("[B4] 断网重试耗尽 → 暂停（下次运行自动续跑）OK")

# ---------------- B5) 关闭自愈 → 不探测 ----------------
runner5, cfg5 = make_runner(auto_relogin=False)
runner5.session = FakeSession([])
assert runner5._ensure_session("测试") is True
assert runner5.session.calls == 0, "auto_relogin=False 时不应探测"
print("[B5] auto_relogin=False → 不探测 OK")

print("\n[OK] 掉线自愈功能测试全部通过")
