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


def t_main_no_empty_url_call():
    """main 里抢锁失败分支不得再出现 open_app_window(url) 空值调用。"""
    src = (ROOT / "tools" / "web_ui.py").read_text(encoding="utf-8")
    seg = src[src.find("if not acquire_instance_mutex():"):]
    seg = seg[:seg.find("running = find_running()")]
    assert 'url = f"http://127.0.0.1:{port}" if port else ""' not in seg, \
        "仍在构造可能为空的 url"
    assert "if port:" in seg, "应先判断 port 是否就绪"
    assert "尚未就绪" in seg, "端口未就绪应只提示"


print("[空地址不启动浏览器 · 回归测试]")
check("空 url 不启动任何浏览器", t_empty_url_opens_nothing)
check("纯空白 url 不启动浏览器", t_blank_url_opens_nothing)
check("有效 url 仍正常打开", t_valid_url_still_opens)
check("main 抢锁分支不再传空 url", t_main_no_empty_url_call)

print("\n" + "=" * 60)
print(f"结果：{PASS}/{PASS + FAIL} 通过" + (f"，{FAIL} 失败" if FAIL else "，全绿 ✓"))
print("=" * 60)
raise SystemExit(1 if FAIL else 0)
