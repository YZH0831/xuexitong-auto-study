# -*- coding: utf-8 -*-
"""回归测试：多页分支播放视频前必须检查该页音视频任务点是否已完成。

历史 bug：①-B 多页分支只要检测到 <video> 就无条件播放，导致
「任务点已显示完成还从头重看」，而且视频占满时间、后面的章节测验轮不到。

用法：python tests/test_page_media_finished.py
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


CODE = code_text("runner")


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        print(f"  ✓ {name}")
    except Exception as exc:  # noqa: BLE001
        FAIL += 1
        print(f"  ✗ {name} — {type(exc).__name__}: {exc}")


def t_helper_exists():
    assert "def _page_media_finished" in CODE, "缺少 _page_media_finished 辅助函数"
    seg = CODE[CODE.find("def _page_media_finished"):CODE.find("def _should_handle")]
    assert "ans-attach-ct" in (ROOT / "cxlab" / "runner.py").read_text(encoding="utf-8")
    assert "unfinished_task_cards" in seg, "未复用 unfinished_task_cards 判定"


def t_multi_page_checks_before_play():
    """多页分支里，完成检查必须在「检测到视频 → 播放」之前（用原始源码判断）。"""
    raw = (ROOT / "cxlab" / "runner.py").read_text(encoding="utf-8")
    start = raw.find("发现 {len(pm)} 个内容页签")
    assert start > 0, "未找到多页分支"
    seg = raw[start:start + 6000]
    check_pos = seg.find("_page_media_finished(cards_i)")
    play_pos = seg.find("检测到视频")
    assert check_pos > 0, "多页分支未调用 _page_media_finished"
    assert play_pos > 0, "未找到播放分支"
    assert check_pos < play_pos, "完成检查必须在播放之前"


def t_skip_log_present():
    raw = (ROOT / "cxlab" / "runner.py").read_text(encoding="utf-8")
    assert "已显示完成 → 跳过，不重看" in raw, "缺少跳过日志"


# ---- 行为模拟 ----
def _sim(cards_cls_list, pending_kinds):
    """复刻 _page_media_finished 的判定。cards_cls_list=None 表示非卡片布局。"""
    if cards_cls_list is None:
        return False
    if not cards_cls_list:
        return False
    return not any(k in ("video", "audio") for k in pending_kinds)


def t_behavior():
    # 有卡片、音视频全部完成（无未完成项）→ 跳过播放
    assert _sim(["ans-attach-ct ans-job-finished"], []) is True, \
        "视频已完成时应跳过播放"
    # 有卡片、视频仍未完成 → 照常播放
    assert _sim(["ans-attach-ct"], ["video"]) is False, \
        "视频未完成时必须播放"
    # 视频完成但测验未完成（pending 里不含 video）→ 跳过播放，去做题
    assert _sim(["ans-attach-ct ans-job-finished", "ans-attach-ct"], []) is True, \
        "视频完成、仅测验未完成时应跳过视频去做题"
    # 非卡片布局 → 无法判断，照常播放
    assert _sim(None, []) is False, "非卡片布局不应擅自跳过"


print("[多页分支视频完成检查 · 回归测试]")
check("辅助函数存在且复用 unfinished_task_cards", t_helper_exists)
check("播放前先做完成检查", t_multi_page_checks_before_play)
check("跳过日志存在", t_skip_log_present)
check("行为：完成则跳过、未完成则播放", t_behavior)

print("\n" + "=" * 60)
print(f"结果：{PASS}/{PASS + FAIL} 通过" + (f"，{FAIL} 失败" if FAIL else "，全绿 ✓"))
print("=" * 60)
raise SystemExit(1 if FAIL else 0)
