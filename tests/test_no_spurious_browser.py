# -*- coding: utf-8 -*-
"""回归测试：没有有效地址时绝不允许启动浏览器。

历史 bug：连点两次 bat 时，第二个实例抢锁失败、首个实例端口还没绑好，
会带着空 url 执行 `edge --app=`（空地址）。Edge 拿不到目标就恢复上次会话，
用户看到「还没选课就自动弹出学习通」。

用法：python tests/test_no_spurious_browser.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import web_ui  # noqa: E402

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


class _Recorder:
    """记录 Popen / webbrowser.open 是否被调用。"""

    def __init__(self):
        self.popen_calls = []
        self.web_calls = []


def _patch(rec):
    orig_popen = web_ui.subprocess.Popen
    orig_web = web_ui.webbrowser.open
    orig_focus = web_ui.focus_window
    orig_wait = web_ui.wait_for_window

    def fake_popen(args, **kw):
        rec.popen_calls.append(args)
        return type("P", (), {"pid": 0})()

    web_ui.subprocess.Popen = fake_popen
    web_ui.webbrowser.open = lambda u, *a, **k: rec.web_calls.append(u)
    # 避免真的去找/聚焦窗口
    web_ui.focus_window = lambda: False
    web_ui.wait_for_window = lambda *a, **k: False
    return (orig_popen, orig_web, orig_focus, orig_wait)


def _unpatch(saved):
    web_ui.subprocess.Popen, web_ui.webbrowser.open, \
        web_ui.focus_window, web_ui.wait_for_window = saved


def t_empty_url_opens_nothing():
    rec = _Recorder()
    saved = _patch(rec)
    try:
        ok = web_ui.open_app_window("")
    finally:
        _unpatch(saved)
    assert ok is False, "空 url 应返回 False"
    assert not rec.popen_calls, f"空 url 不应启动浏览器进程，实际: {rec.popen_calls}"
    assert not rec.web_calls, f"空 url 不应调 webbrowser.open，实际: {rec.web_calls}"


def t_blank_url_opens_nothing():
    rec = _Recorder()
    saved = _patch(rec)
    try:
        ok = web_ui.open_app_window("   ")
    finally:
        _unpatch(saved)
    assert ok is False and not rec.popen_calls and not rec.web_calls, \
        "纯空白 url 也不应启动浏览器"


def t_valid_url_still_opens():
    """有有效地址时仍要正常打开（不能把功能改没了）。"""
    rec = _Recorder()
    saved = _patch(rec)
    orig_edge = web_ui.edge_path
    web_ui.edge_path = lambda: Path("C:/fake/msedge.exe")
    web_ui._LAST_APP_LAUNCH = 0.0
    try:
        ok = web_ui.open_app_window("http://127.0.0.1:8765")
    finally:
        web_ui.edge_path = orig_edge
        _unpatch(saved)
    assert ok is True, "有效地址应返回 True"
    assert rec.popen_calls, "有效地址应启动 Edge"
    joined = " ".join(str(x) for x in rec.popen_calls[0])
    assert "http://127.0.0.1:8765" in joined, f"命令行应含目标地址: {joined}"
    assert "--app=http://127.0.0.1:8765" in joined, "应以 --app 模式打开"


def t_url_must_be_local_console():
    """外部地址 / 畸形地址绝不能交给浏览器。"""
    good = "http://127.0.0.1:8765"
    assert web_ui.safe_console_url(good) == good
    for bad in ("", "   ", "https://www.chaoxing.com", "http://localhost:8765",
                "http://127.0.0.1:1234", "http://127.0.0.1:8765/evil",
                "http://127.0.0.1:8765\nhttp://evil.com", "file:///C:/Windows"):
        assert web_ui.safe_console_url(bad) == "", f"不该放行: {bad!r}"


def t_external_url_launches_nothing():
    rec = _Recorder()
    saved = _patch(rec)
    try:
        ok = web_ui.open_app_window("https://www.chaoxing.com")
    finally:
        _unpatch(saved)
    assert ok is False and not rec.popen_calls and not rec.web_calls, \
        "外部地址不应启动任何浏览器"


def t_focus_only_never_launches():
    """allow_launch=False 时只聚焦窗口，绝不新开浏览器。"""
    rec = _Recorder()
    saved = _patch(rec)
    try:
        ok = web_ui.open_app_window("http://127.0.0.1:8765", allow_launch=False)
    finally:
        _unpatch(saved)
    assert ok is False and not rec.popen_calls and not rec.web_calls, \
        "只聚焦模式不应启动浏览器"


def t_debounce_blocks_double_launch():
    """2 秒内的第二次开窗请求被吞掉。"""
    rec = _Recorder()
    saved = _patch(rec)
    orig_edge = web_ui.edge_path
    web_ui.edge_path = lambda: Path("C:/fake/msedge.exe")
    web_ui._LAST_APP_LAUNCH = 0.0
    try:
        web_ui.open_app_window("http://127.0.0.1:8765")
        web_ui.open_app_window("http://127.0.0.1:8765")
    finally:
        web_ui.edge_path = orig_edge
        _unpatch(saved)
    assert len(rec.popen_calls) == 1, f"连点只应起一次 Edge，实际 {len(rec.popen_calls)} 次"


def t_no_default_browser_fallback():
    """源码层面禁止 webbrowser.open 兜底：默认浏览器会恢复上次会话。"""
    src = (ROOT / "tools" / "web_ui.py").read_text(encoding="utf-8")
    seg = src[src.find("def open_app_window("):]
    seg = seg[:seg.find("\ndef find_running(")]
    assert "webbrowser.open(" not in seg, "open_app_window 里仍会打开默认浏览器"


def t_main_loser_never_launches():
    """抢锁失败分支只聚焦窗口，不启动浏览器。"""
    src = (ROOT / "tools" / "web_ui.py").read_text(encoding="utf-8")
    seg = src[src.find("if not acquire_instance_mutex():"):]
    seg = seg[:seg.find("running = find_running()")]
    assert "wait_for_window(" in seg, "应等已有窗口出现"
    assert "open_app_window(" not in seg, "抢锁失败的实例不应自己开浏览器"
    assert "尚未就绪" in seg, "窗口没出现时应提示"


def t_run_is_serialized():
    """/api/run 的检查与赋值必须串行化，防止连点起两个刷课进程。"""
    src = (ROOT / "tools" / "web_ui.py").read_text(encoding="utf-8")
    seg = src[src.find("def start_job("):]
    seg = seg[:seg.find("\ndef stop_job(")]
    assert "with START_LOCK:" in seg, "start_job 缺少串行化锁"
    assert seg.index("with START_LOCK:") < seg.index("PROC, PROC_CMD, PROC_START = p"), \
        "进程登记必须在锁内"


print("[空地址不启动浏览器 · 回归测试]")
check("空 url 不启动任何浏览器", t_empty_url_opens_nothing)
check("纯空白 url 不启动浏览器", t_blank_url_opens_nothing)
check("有效 url 仍正常打开", t_valid_url_still_opens)
check("只放行本地控制台地址", t_url_must_be_local_console)
check("外部地址不启动浏览器", t_external_url_launches_nothing)
check("只聚焦模式不启动浏览器", t_focus_only_never_launches)
check("连点开窗有 2 秒防抖", t_debounce_blocks_double_launch)
check("不再调用默认浏览器兜底", t_no_default_browser_fallback)
check("抢锁失败实例只聚焦", t_main_loser_never_launches)
check("运行任务启动已串行化", t_run_is_serialized)

print("\n" + "=" * 60)
print(f"结果：{PASS}/{PASS + FAIL} 通过" + (f"，{FAIL} 失败" if FAIL else "，全绿 ✓"))
print("=" * 60)
raise SystemExit(1 if FAIL else 0)
