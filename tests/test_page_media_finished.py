# -*- coding: utf-8 -*-
"""回归测试：多页分支的音视频完成判定。

历史 bug 两连：
  ① 旧版只要检测到 <video> 就无条件播放 → 已完成的视频被反复重看，
     后面的章节测验永远轮不到。
  ② v1.2.0 初版判成「没有未完成音视频就跳过」→ **纯测验页也被跳过**，
     结果视频跳过了、题也没做。

正确口径：只有「本页确实含音视频任务卡」且「它们全部 ans-job-finished」
才允许跳过播放；纯测验/文档页一律返回 False，交给答题流程。

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


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        print(f"  ✓ {name}")
    except Exception as exc:  # noqa: BLE001
        FAIL += 1
        print(f"  ✗ {name} — {type(exc).__name__}: {exc}")


def t_helper_requires_media_cards():
    code = code_text("runner")
    seg = code[code.find("def _page_media_finished"):code.find("def _should_handle")]
    assert seg, "缺少 _page_media_finished"
    assert "has_media" in seg, "缺少 has_media 判定（否则纯测验页会被误跳）"
    assert "return has_media and not media_unfinished" in seg, \
        "返回条件必须是「有音视频卡片且全部完成」"


def t_media_detection_present():
    raw = (ROOT / "cxlab" / "runner.py").read_text(encoding="utf-8")
    seg = raw[raw.find("def _page_media_finished"):raw.find("def _should_handle")]
    for mark in ("modules/video", "modules/audio"):
        assert mark in seg, f"缺少音视频识别标记 {mark}"


def t_checks_before_play():
    raw = (ROOT / "cxlab" / "runner.py").read_text(encoding="utf-8")
    start = raw.find("发现 {len(pm)} 个内容页签")
    seg = raw[start:start + 6000]
    chk = seg.find("_page_media_finished(cards_i)")
    play = seg.find("检测到视频")
    assert 0 < chk < play, "完成检查必须在播放之前"


# ---- 行为模拟：严格对齐新实现 ----
def _sim(cards):
    """cards: None=非卡片布局；[(is_media, finished), ...]。

    对齐实现：has_media and not media_unfinished
    """
    if not cards:
        return False
    has_media = False
    media_unfinished = False
    for is_media, finished in cards:
        if not is_media:
            continue
        has_media = True
        if not finished:
            media_unfinished = True
            break
    return has_media and not media_unfinished


def t_behavior():
    # ① 视频卡片已完成 → 跳过播放
    assert _sim([(True, True)]) is True, "已完成的视频应跳过"
    # ② 视频卡片未完成 → 正常播放
    assert _sim([(True, False)]) is False, "未完成的视频必须播放"
    # ③ 【本次修复核心】纯测验页（无音视频卡片）→ 绝不能跳过，必须去做题
    assert _sim([(False, False)]) is False, \
        "纯测验页不得跳过（否则视频跳了、题也没做）"
    # ④ 视频已完成 + 测验未完成 → 跳过视频，留给后续做题
    assert _sim([(True, True), (False, False)]) is True, \
        "视频完成、仅测验未完成时应跳过视频去做题"
    # ⑤ 非卡片布局 → 读不准，保守不跳
    assert _sim(None) is False, "非卡片布局不应擅自跳过"


print("[多页分支音视频完成判定 · 回归测试]")
check("辅助函数要求「确实含音视频卡片」", t_helper_requires_media_cards)
check("音视频识别标记齐全", t_media_detection_present)
check("完成检查先于播放", t_checks_before_play)
check("行为：纯测验页不跳 / 已完成视频才跳", t_behavior)

print("\n" + "=" * 60)
print(f"结果：{PASS}/{PASS + FAIL} 通过" + (f"，{FAIL} 失败" if FAIL else "，全绿 ✓"))
print("=" * 60)
raise SystemExit(1 if FAIL else 0)
