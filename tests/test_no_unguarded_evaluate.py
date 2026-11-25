# -*- coding: utf-8 -*-
"""回归测试：_open_chapter 调用链上不再有无超时的全-frame evaluate。

卡死根因：Playwright evaluate 没有内建超时；视频 iframe JS 忙时会永久阻塞。
本测试守住三条高危路径不会被回退。只检查【实际代码行】（跳过注释/docstring）。

用法：python tests/test_no_unguarded_evaluate.py
"""
import io
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def code_lines(name):
    """返回该文件去掉注释和字符串后的代码行（保留行号顺序）。"""
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


CODE = {n: code_lines(n) for n in ("runner", "popups", "course", "video")}
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


def t_chapter_locked_uses_locator():
    seg = CODE["runner"]
    body = seg[seg.find("def _chapter_locked"):seg.find("def _open_chapter")]
    assert "inner_text(timeout=" in body, "_chapter_locked 未用带超时的 inner_text"
    assert ".evaluate(" not in body, "_chapter_locked 仍有裸 evaluate"


def t_popup_uses_locator():
    body = CODE["popups"]
    assert "page.evaluate(JS_DIALOGS)" not in body, "_close_known 仍有裸 evaluate"
    assert "inner_text(timeout=" in body, "_close_known 未用带超时的 inner_text"


def t_chapter_element_guards_js():
    body = CODE["course"]
    seg = body[body.find("def chapter_element"):body.find("def unfinished_task_cards")]
    assert seg.count("frame.evaluate(JS_MARK_CHAPTER") == 1, \
        f"JS_MARK_CHAPTER 调用点数量异常: {seg.count('frame.evaluate(JS_MARK_CHAPTER')}"
    # 选择器字符串被 tokenize 过滤，用原始源码核对守卫
    raw = (ROOT / "cxlab" / "course.py").read_text(encoding="utf-8")
    rseg = raw[raw.find("def chapter_element"):raw.find("def unfinished_task_cards")]
    js_pos = rseg.find("frame.evaluate(JS_MARK_CHAPTER, str(cid))")
    guard_pos = rseg.find(".posCatalog_select")
    assert 0 < guard_pos < js_pos, "JS_MARK_CHAPTER 前未做章节树元素守卫"


def t_completion_ratio_uses_locator():
    body = CODE["video"]
    seg = body[body.find("def detect_completion_ratio"):body.find("def probe_playback_rate")]
    assert "frame.evaluate(JS_GET_TEXT)" not in seg, "完成度探测仍有裸 evaluate"
    assert "inner_text(timeout=" in seg, "完成度探测未用带超时的 inner_text"


def t_heartbeats_are_info_level():
    seg = CODE["runner"]
    for mark in ("定位章节树", "点击完成，检查章节开放状态",
                 "开放检查完成，等待内容帧", "内容帧", "读取 mArg.attachments"):
        assert mark in seg, f"心跳标记缺失: {mark}"
    assert "log.info(f\"  [心跳]" in seg or "log.info('  [心跳]" in seg, \
        "心跳未用 INFO 级"


print("[无超时 evaluate 清除 · 回归测试]")
check("_chapter_locked 用带超时 locator", t_chapter_locked_uses_locator)
check("弹窗探测用带超时 locator", t_popup_uses_locator)
check("章节元素 JS 有前置守卫", t_chapter_element_guards_js)
check("完成度探测用带超时 locator", t_completion_ratio_uses_locator)
check("心跳日志为 INFO 级且齐全", t_heartbeats_are_info_level)

print("\n" + "=" * 60)
print(f"结果：{PASS}/{PASS + FAIL} 通过" + (f"，{FAIL} 失败" if FAIL else "，全绿 ✓"))
print("=" * 60)
raise SystemExit(1 if FAIL else 0)
