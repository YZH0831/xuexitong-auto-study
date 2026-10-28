# -*- coding: utf-8 -*-
"""文档 / 课件任务点：自动翻页浏览，处理"阅读 N 分钟"的真实时长要求。"""
import re
import time

NEXT_SELECTORS = [
    'button[aria-label*="下一"]',
    '[class*="next"]',
    "#next",
    ".nextBtn",
    "#nextPage",
    'text=下一页',
    'text=下一张',
    'text=下一页签',
    'text=下一个',
]

PAGE_NUM_PATTERNS = [
    re.compile(r"(\d+)\s*/\s*(\d+)"),
    re.compile(r"共\s*(\d+)\s*页"),
]

READ_RULE = re.compile(r"(累计阅读|阅读时长|需阅读|阅读满|阅读时间)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(分钟|min)")


def _frame_texts(page):
    texts = []
    for frame in page.frames:
        try:
            t = frame.evaluate("() => (document.body ? document.body.innerText : '')") or ""
        except Exception:  # noqa: BLE001
            continue
        if t:
            texts.append(t)
    return texts


def detect_total_pages(page):
    for text in _frame_texts(page):
        for pat in PAGE_NUM_PATTERNS:
            m = pat.search(text)
            if m:
                if pat is PAGE_NUM_PATTERNS[0]:
                    return int(m.group(2))
                return int(m.group(1))
    return None


def detect_read_duration(page):
    for text in _frame_texts(page):
        m = READ_RULE.search(text)
        if m:
            return float(m.group(2))
    return None


JS_DOC_STATE = r"""
() => {
  const t = document.body ? document.body.innerText : '';
  let cur = null, total = null;
  let m = /(\d+)\s*\/\s*(\d+)/.exec(t);
  if (m) { cur = parseInt(m[1], 10); total = parseInt(m[2], 10); }
  else { m = /共\s*(\d+)\s*页/.exec(t); if (m) total = parseInt(m[1], 10); }
  return { cur: cur, total: total };
}
"""

_DOC_FRAME = {"frame": None}


def doc_state(page):
    """单次 JS 取回「当前页/总页数」，并复用上次命中的阅读器帧。

    原实现每取一次页码都要遍历所有帧 dump innerText（约 10 次 CDP），
    翻页循环里每页调用两次，是本模块的最大开销。
    """
    cached = _DOC_FRAME.get("frame")
    frames = []
    if cached is not None:
        frames.append(cached)
    frames.extend([f for f in page.frames if f is not cached])
    for f in frames:
        try:
            st = f.evaluate(JS_DOC_STATE)
        except Exception:  # noqa: BLE001
            continue
        if st and (st.get("cur") or st.get("total")):
            _DOC_FRAME["frame"] = f
            return st
    return {"cur": None, "total": None}


def current_page_number(page):
    return doc_state(page).get("cur")


def advance_page(page, log):
    """点一次"下一页"，成功返回 True。"""
    for sel in NEXT_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el is not None and el.is_visible():
                el.click(timeout=3000)
                return True
        except Exception:  # noqa: BLE001
            continue
    # 键盘兜底
    try:
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(200)
        page.keyboard.press("PageDown")
        return True
    except Exception:  # noqa: BLE001
        return False


def browse_document(session, cfg, log, page, popup, tp):
    dcfg = cfg.document
    total = detect_total_pages(page)
    read_min = detect_read_duration(page)
    if total:
        log.info(f"  文档共 {total} 页")
    if read_min:
        log.info(f"  检测到平台要求：累计阅读 {read_min} 分钟 → 按真实时间停留")

    start = time.time()
    limit = float(dcfg.max_seconds)
    delay = int(dcfg.page_delay_ms)
    stall = 0
    last_num = None
    pages_done = 0
    no_pager = 0          # 连续多少次既没页码、也翻不动 → 判定为"单页正文"

    while time.time() - start < limit:
        popup.tick(page)
        session.keep_alive()
        cur = doc_state(page).get("cur")
        ok = advance_page(page, log)
        page.wait_for_timeout(delay)
        pages_done += 1

        new_num = doc_state(page).get("cur")
        if total and new_num is not None and new_num >= total:
            log.info(f"  已翻到最后一页（{total}/{total}）")
            break
        # 读不到页码 = 大概率是"单页正文"（新版知识点页没有阅读器分页）
        # 注意：不能看 advance_page 的返回值——它的键盘兜底几乎总是返回 True
        if new_num is None:
            no_pager += 1
            if no_pager >= 4:
                log.info("  页面无页码信息（单页正文）→ 滚动浏览后结束")
                for _ in range(4):
                    try:
                        page.mouse.wheel(0, 700)
                    except Exception:  # noqa: BLE001
                        pass
                    page.wait_for_timeout(1000)
                break
        else:
            no_pager = 0
        if new_num is not None and cur is not None and new_num == cur:
            stall += 1
            if stall >= 4:
                log.info("  页码不再变化，判定已到末尾或无法继续翻页")
                break
        else:
            stall = 0
        last_num = new_num
        if not ok and new_num is None:
            # 完全无法识别翻页控件：退化为滚动浏览
            try:
                page.mouse.wheel(0, 600)
            except Exception:  # noqa: BLE001
                pass

    # 最后一页停留，等待阅读器上报
    page.wait_for_timeout(int(dcfg.dwell_last_page_s) * 1000)

    # 若平台要求阅读时长，补足真实停留时间（平台按阅读器打点累计，无加速通道）
    if read_min and bool(dcfg.read_duration_1x):
        need = read_min * 60 - (time.time() - start)
        if need > 0:
            log.info(f"  需补足阅读时长 {need/60:.1f} 分钟（平台按真实时间计）")
            end = time.time() + need
            while time.time() < end:
                popup.tick(page)
                session.keep_alive()
                try:
                    page.mouse.wheel(0, 300)
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(20)

    note = f"翻页 {pages_done} 次"
    if total:
        note += f" / 共 {total} 页"
    if read_min:
        note += f"（满足 {read_min} 分钟阅读要求）"
    if last_num == 1 and total and total > 3:
        return "manual", note + "；未能翻页，可能需要人工确认"
    return "done", note
