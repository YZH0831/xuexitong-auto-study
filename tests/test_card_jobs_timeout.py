# -*- coding: utf-8 -*-
"""回归测试：card_jobs 的 evaluate/content 有超时保护（卡死 10.2 的真正根因）。

离线，不联网不开浏览器。
用法：python tests/test_card_jobs_timeout.py
"""
import io
import sys
import threading
import time
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cxlab.course import call_with_timeout, eval_with_timeout  # noqa: E402

RAW = (ROOT / "cxlab" / "course.py").read_text(encoding="utf-8")
PASS, FAIL = 0, 0


def code_text(name):
    src = (ROOT / "cxlab" / f"{name}.py").read_text(encoding="utf-8")
    raw = src.splitlines()
    keep = set()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        if tok.type in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                        tokenize.DEDENT, tokenize.ENDMARKER):
            continue
        keep.add(tok.start[0] - 1)
    return "\n".join(raw[i] for i in sorted(keep))


CODE = code_text("course")


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        print(f"  ✓ {name}")
    except Exception as exc:  # noqa: BLE001
        FAIL += 1
        print(f"  ✗ {name} — {type(exc).__name__}: {exc}")


def t_helpers_exist():
    assert callable(call_with_timeout) and callable(eval_with_timeout)


def t_card_jobs_uses_guards():
    seg = CODE[CODE.find("def card_jobs"):CODE.find("def card_type")]
    assert "eval_with_timeout(" in seg, "card_jobs 未用带超时的 evaluate"
    assert "call_with_timeout(cards_frame.content" in seg, "content() 未加超时"
    assert "cards_frame.evaluate(" not in seg, "card_jobs 仍有裸 evaluate"


def t_behavior_call_timeout_returns():
    """慢函数会在超时后返回 default，不等它跑完。"""
    t0 = time.time()
    got = call_with_timeout(lambda: time.sleep(2) or "slow", timeout_s=0.2, default="FAST")
    elapsed = time.time() - t0
    assert got == "FAST", f"超时应返回 default，实际 {got}"
    assert elapsed < 1.0, f"超时未生效，耗时 {elapsed:.2f}s"


def t_behavior_call_normal_returns_value():
    got = call_with_timeout(lambda: 42, timeout_s=2.0, default=None)
    assert got == 42, f"正常调用应返回值，实际 {got}"


def t_behavior_eval_timeout():
    """模拟一个 evaluate 永久卡住的 frame，验证主线程能返回。"""
    class StuckFrame:
        def evaluate(self, script):
            time.sleep(3)
            return ["SHOULD_NOT_APPEAR"]

    t0 = time.time()
    got = eval_with_timeout(StuckFrame(), "() => 1", timeout_s=0.2, default=[])
    elapsed = time.time() - t0
    assert got == [], f"超时应返回 default，实际 {got}"
    assert elapsed < 1.0, f"evaluate 超时未生效，耗时 {elapsed:.2f}s"


print("[card_jobs 超时保护 · 回归测试]")
check("超时 helper 存在", t_helpers_exist)
check("card_jobs 用超时包装 evaluate/content", t_card_jobs_uses_guards)
check("行为：慢调用超时返回 default", t_behavior_call_timeout_returns)
check("行为：正常调用返回值", t_behavior_call_normal_returns_value)
check("行为：卡住的 evaluate 超时返回", t_behavior_eval_timeout)

print("\n" + "=" * 60)
print(f"结果：{PASS}/{PASS + FAIL} 通过" + (f"，{FAIL} 失败" if FAIL else "，全绿 ✓"))
print("=" * 60)
raise SystemExit(1 if FAIL else 0)
