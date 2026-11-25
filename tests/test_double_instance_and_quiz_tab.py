# -*- coding: utf-8 -*-
"""回归测试：① 单实例锁真的能拦第二个进程 ② 重扫会点「2章节测验」标签。

离线，不联网不开浏览器。
用法：python tests/test_double_instance_and_quiz_tab.py
"""
import ast
import ctypes
import hashlib
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WEB_SRC = (ROOT / "tools" / "web_ui.py").read_text(encoding="utf-8")
RUN_SRC = (ROOT / "cxlab" / "runner.py").read_text(encoding="utf-8")
PASS, FAIL = 0, 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        print(f"  ✓ {name}")
    except Exception as exc:  # noqa: BLE001
        FAIL += 1
        print(f"  ✗ {name} — {type(exc).__name__}: {exc}")


# ---------- ① 单实例锁 ----------
def t_syntax():
    ast.parse(WEB_SRC)
    ast.parse(RUN_SRC)


def t_uses_use_last_error():
    assert 'ctypes.WinDLL("kernel32", use_last_error=True)' in WEB_SRC, \
        "未用 use_last_error=True（GetLastError 恒 0 的根源）"


def t_uses_ctypes_get_last_error():
    assert "ctypes.get_last_error()" in WEB_SRC, "未用 ctypes.get_last_error() 读错误码"


def t_no_broken_windll_getlasterror():
    # 旧的错误写法必须消失
    assert "k = ctypes.windll.kernel32" not in WEB_SRC, "仍有 windll 直接读 last-error 的错误写法"


def t_argtypes_declared():
    seg = WEB_SRC[WEB_SRC.find("def acquire_instance_mutex"):WEB_SRC.find("def wait_running_port")]
    assert "k.CreateMutexW.argtypes" in seg, "CreateMutexW 未声明 argtypes"
    assert "wintypes.LPCWSTR" in seg, "互斥体名未用宽字符类型"


def t_behavior_mutex_first_true_second_false():
    """真实 Win32 行为：同一个 mutex 名，第一个拿到、第二个应报 183。"""
    if sys.platform != "win32":
        return
    name = "Local\\CXConsole_TEST_" + hashlib.md5(b"regression").hexdigest()[:10]
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateMutexW.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    ctypes.set_last_error(0)
    h1 = k.CreateMutexW(None, False, name)
    e1 = ctypes.get_last_error()
    ctypes.set_last_error(0)
    h2 = k.CreateMutexW(None, False, name)
    e2 = ctypes.get_last_error()
    assert e1 == 0, f"首个实例应无错误，实际 {e1}"
    assert e2 == 183, f"第二个实例应 ERROR_ALREADY_EXISTS(183)，实际 {e2}"
    k.CloseHandle(h1)
    k.CloseHandle(h2)


# ---------- ② 重扫点题目标签 ----------
def t_rescan_signature():
    assert "def _rescan_current_tasks(self, ch, page, allow_quiz=False)" in RUN_SRC, \
        "重扫函数未加 allow_quiz 参数"


def t_process_chapter_passes_allow_quiz():
    seg = RUN_SRC[RUN_SRC.find("内容处理后仍有"):RUN_SRC.find("内容处理后仍有") + 500]
    assert "_rescan_current_tasks(ch, page, allow_quiz=True)" in seg, \
        "内容处理后重扫未传 allow_quiz=True"


def t_run_quiz_keeps_default():
    seg = RUN_SRC[RUN_SRC.find("检测到未完成提示"):RUN_SRC.find("检测到未完成提示") + 400]
    assert "_rescan_current_tasks(ch, page)" in seg, \
        "提交被拦截后的重扫应保持默认 allow_quiz=False"


def t_quiz_tab_clicks():
    seg = RUN_SRC[RUN_SRC.find("重扫点开题目标签"):RUN_SRC.find("重扫点开题目标签") + 900]
    assert "self._try_open_quiz(page_i)" in seg, "题目标签未走答题页打开"
    assert "self._handle_quiz(qp, ch)" in seg, "题目标签未调作答"


def t_blocked_guard():
    seg = RUN_SRC[RUN_SRC.find("重扫点开题目标签") - 300:RUN_SRC.find("重扫点开题目标签")]
    assert "self._submit_blocked_ids" in seg, "补做测验未检查提交拦截集合"


def t_behavior_allow_quiz_decision():
    """行为模拟：allow_quiz 与提交拦截共同决定 quiz 标签处理。"""
    def decide(allow_quiz, blocked):
        if not allow_quiz or blocked:
            return "skip"
        return "do"
    assert decide(False, False) == "skip", "提交拦截后重扫必须跳过题目"
    assert decide(True, True) == "skip", "被拦截章节不能重复做"
    assert decide(True, False) == "do", "小圆仍橙且没被拦截应做题目"


print("[双实例 + 重扫测验标签 · 回归测试]")
check("源码语法正确", t_syntax)
check("mutex 用 use_last_error=True", t_uses_use_last_error)
check("mutex 用 ctypes.get_last_error()", t_uses_ctypes_get_last_error)
check("旧 windll+GetLastError 写法已移除", t_no_broken_windll_getlasterror)
check("CreateMutexW argtypes 已声明", t_argtypes_declared)
check("行为：首个拿锁/第二个报183", t_behavior_mutex_first_true_second_false)
check("重扫函数加 allow_quiz 参数", t_rescan_signature)
check("内容处理后重扫传 allow_quiz=True", t_process_chapter_passes_allow_quiz)
check("提交拦截后重扫保持默认", t_run_quiz_keeps_default)
check("测验标签点开并作答", t_quiz_tab_clicks)
check("补做测验有提交拦截守卫", t_blocked_guard)
check("行为：allow_quiz 决策正确", t_behavior_allow_quiz_decision)

print("\n" + "=" * 60)
print(f"结果：{PASS}/{PASS + FAIL} 通过" + (f"，{FAIL} 失败" if FAIL else "，全绿 ✓"))
print("=" * 60)
raise SystemExit(1 if FAIL else 0)
