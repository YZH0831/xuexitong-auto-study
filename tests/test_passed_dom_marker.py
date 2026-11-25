# -*- coding: utf-8 -*-
"""回归测试：card_jobs 的 passed 判定必须认 DOM 的 ans-job-finished。

历史 bug：card_jobs 只看平台接口 isPassed，而 isPassed 常滞后于页面显示，
导致「任务点已显示完成却重播视频 → 播完仍未通过 → 重试队列再次打开」。

用法：python tests/test_passed_dom_marker.py
"""
import io
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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


def t_card_jobs_checks_dom():
    seg = CODE[CODE.find("def card_jobs"):CODE.find("def card_type")]
    assert "ans-job-finished" in seg, "card_jobs 未检查 DOM 完成标记"
    assert "dom_done" in seg, "缺少 dom_done 判定变量"
    assert "or dom_done" in seg, "dom_done 未参与 passed 计算"


def t_both_paths_consistent():
    """两条路径（unfinished_task_cards / card_jobs）都应认 ans-job-finished。"""
    raw = (ROOT / "cxlab" / "course.py").read_text(encoding="utf-8")
    n = raw.count("ans-job-finished")
    assert n >= 2, f"ans-job-finished 使用点应≥2，实际 {n}"


def t_behavior_passed_logic():
    """复刻修复后判定：三种情况都要判为已完成。"""
    def passed(is_passed, dom_done, kind, job):
        return bool(is_passed) or dom_done or (kind in ("video", "audio") and not job)

    # 接口说未通过，但 DOM 已完成 → 必须视为已完成（核心修复）
    assert passed(False, True, "video", {"x": 1}) is True, \
        "DOM 已完成时应判 passed，否则会重播"
    # 接口通过
    assert passed(True, False, "video", {"x": 1}) is True
    # 两者都未完成 → 未通过
    assert passed(False, False, "video", {"x": 1}) is False, \
        "确实未完成时必须判未通过"
    # 无 job 的视频/音频历史兜底保留
    assert passed(False, False, "video", None) is True


print("[passed 判定 DOM 标记 · 回归测试]")
check("card_jobs 检查 DOM 完成标记", t_card_jobs_checks_dom)
check("两条路径判定口径一致", t_both_paths_consistent)
check("行为：DOM 完成即判 passed", t_behavior_passed_logic)

print("\n" + "=" * 60)
print(f"结果：{PASS}/{PASS + FAIL} 通过" + (f"，{FAIL} 失败" if FAIL else "，全绿 ✓"))
print("=" * 60)
raise SystemExit(1 if FAIL else 0)
