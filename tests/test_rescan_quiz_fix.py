# -*- coding: utf-8 -*-
"""回归测试：视频 job 处理后补做测验，避免「重复刷视频不点章节检测」。

离线，不联网不开浏览器。验证：
  1. 源码守卫：补做测验块位于 tab 短路之前、1x 重播受 quiz_done_after_jobs 守卫；
  2. 行为：小圆仍橙且有答题入口 → 补做测验、置标志、1x 重播被跳过；
  3. 行为：提交被拦截（_submit_blocked_ids）→ 不补做测验；
  4. 行为：小圆已绿（hint=0）→ 不补做测验。

用法：python tests/test_rescan_quiz_fix.py
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "cxlab" / "runner.py").read_text(encoding="utf-8")
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


# ---------- 静态守卫 ----------
def t_syntax_ok():
    ast.parse(SRC)


def t_makeup_before_tab_shortcircuit():
    makeup = SRC.find("补做章节测验/作业")
    shortc = SRC.find("tabs = [] if handled_by_jobs")
    assert 0 < makeup < shortc, "补做测验逻辑必须在 handled_by_jobs 的 tab 短路之前"


def t_makeup_uses_real_handlers():
    assert "self._try_open_quiz(page_q)" in SRC, "补做未调用 _try_open_quiz"
    assert "self._handle_quiz(qp, ch)" in SRC, "补做未调用 _handle_quiz"
    assert "self.task_count += 1" in SRC[SRC.find("补做章节测验/作业"):SRC.find("补做章节测验/作业") + 600], \
        "补做测验未计入任务数"


def t_replay_guard():
    start = SRC.find("if (rate_used > 1.0")
    end = SRC.find("平台未登记倍速播放")
    seg = SRC[start:end]
    assert "quiz_done_after_jobs" in seg, "1x 重播未受补做标志守卫"
    assert "and not quiz_done_after_jobs" in seg, "守卫方向错误（应为 not）"


def t_submit_block_guard():
    seg = SRC[SRC.find("补做章节测验/作业") - 400:SRC.find("补做章节测验/作业")]
    assert "str(ch.id) not in self._submit_blocked_ids" in seg, \
        "补做测验未检查提交拦截集合"


# ---------- 行为模拟 ----------
def _sim(hint_after_jobs, has_quiz_entry, quiz_status="done", submit_blocked=False):
    """复刻修复后 jobid 分支的补做测验 + 1x 重播守卫决策。"""
    calls = {"try_open_quiz": 0, "handle_quiz": 0, "replay_1x": 0}
    quiz_done_after_jobs = False
    status, note, ctype = "done", "播放完成 rate=3.0x", "video"
    rate_used = 3.0
    submit_blocked_ids = {"blocked"} if submit_blocked else set()
    ch_id = "blocked" if submit_blocked else "jobquiz"

    # —— 修复块 ——
    if str(ch_id) not in submit_blocked_ids:            # 提交拦截守卫
        hint_jobs = hint_after_jobs
        if isinstance(hint_jobs, int) and hint_jobs > 0:
            if has_quiz_entry:
                calls["try_open_quiz"] += 1
                qp = "QHOST"
                if qp is not None:
                    calls["handle_quiz"] += 1
                    s_q = quiz_status
                    if s_q != "skipped":
                        status, note, ctype = s_q, f"{note}；补做测验：ok", "quiz"
                        quiz_done_after_jobs = True

    # —— 1x 重播兜底（复核后 hint）——
    hint = hint_after_jobs
    if rate_used > 1.0 and isinstance(hint, int) and hint > 0 and not quiz_done_after_jobs:
        calls["replay_1x"] += 1

    return calls, quiz_done_after_jobs, status, ctype


def t_behavior_makeup_and_skip_replay():
    calls, flag, status, ctype = _sim(hint_after_jobs=1, has_quiz_entry=True)
    assert calls["handle_quiz"] == 1, "小圆仍橙有入口应补做测验"
    assert flag is True, "补做后应置标志"
    assert calls["replay_1x"] == 0, "补做后不应 1x 重播视频"
    assert ctype == "quiz", "ctype 应更新为 quiz"


def t_behavior_no_makeup_when_green():
    calls, flag, status, ctype = _sim(hint_after_jobs=0, has_quiz_entry=True)
    assert calls["handle_quiz"] == 0, "小圆已绿不应补做测验"
    assert flag is False
    assert calls["replay_1x"] == 0, "小圆已绿不应重播"


def t_behavior_no_makeup_when_blocked():
    calls, flag, status, ctype = _sim(hint_after_jobs=1, has_quiz_entry=True,
                                     submit_blocked=True)
    assert calls["handle_quiz"] == 0, "提交被拦截不应重复补做测验"
    assert calls["replay_1x"] == 1, "未补做时原有 1x 重播兜底仍生效"


def t_behavior_video_only_still_replays():
    """纯视频章节（无答题入口）：不补做测验，1x 重播兜底保留。"""
    calls, flag, status, ctype = _sim(hint_after_jobs=1, has_quiz_entry=False)
    assert calls["handle_quiz"] == 0 and flag is False
    assert calls["replay_1x"] == 1, "纯视频章节仍应保留 1x 重播兜底"


print("[学习通 重复刷视频修复 · 回归测试]")
check("源码语法正确", t_syntax_ok)
check("补做测验在 tab 短路之前", t_makeup_before_tab_shortcircuit)
check("补做走真实答题入口/处理函数", t_makeup_uses_real_handlers)
check("1x 重播受补做标志守卫", t_replay_guard)
check("提交拦截章节不重复补做", t_submit_block_guard)
check("行为：仍橙有入口→补做且不重播", t_behavior_makeup_and_skip_replay)
check("行为：小圆已绿→不补做", t_behavior_no_makeup_when_green)
check("行为：提交拦截→不补做", t_behavior_no_makeup_when_blocked)
check("行为：纯视频章节保留重播兜底", t_behavior_video_only_still_replays)

print("\n" + "=" * 60)
print(f"结果：{PASS}/{PASS + FAIL} 通过" + (f"，{FAIL} 失败" if FAIL else "，全绿 ✓"))
print("=" * 60)
raise SystemExit(1 if FAIL else 0)
