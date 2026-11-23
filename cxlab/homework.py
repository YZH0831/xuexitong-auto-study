# -*- coding: utf-8 -*-
"""课程作业：定位作业 tab，按配置自动填写提交或转人工。"""
import time

HOMEWORK_TAB_SELECTORS = [
    'text=作业',
    'text=课程作业',
    'a:has-text("作业")',
    'li:has-text("作业")',
    '[class*="tab"]:has-text("作业")',
    'a[href*="/work/list"]',
    'a[href*="homework"]',
]

# 学习通把作业入口放在 iframe 里，且常见文案是"作业/课程作业/章节作业"。
# 命中后用 URL 校验是否真的进了作业页（dohomework / work 路径），
# 避免把导航菜单里的文字当成入口。
WORK_URL_HINTS = ("/work/", "dohomework", "homework", "studentwork", "insertwork")


def _has_work_items(page) -> bool:
    """页面上是否出现作业条目或"提交作业"按钮，用于确认入口点对了。"""
    try:
        txt = page.evaluate(
            "() => document.body ? document.body.innerText.slice(0, 6000) : ''") or ""
    except Exception:  # noqa: BLE001
        return False
    return any(k in txt for k in ("未提交", "待提交", "提交作业", "作业详情", "未完成作业"))

HOMEWORK_ITEM_SELECTORS = [
    '[class*="workItem"]',
    '[class*="homework"]',
    '[class*="task"] li',
    'li:has-text("作业")',
]

SUBMIT_SELECTORS = [
    'button:has-text("提交")',
    'a:has-text("提交")',
    "#submitBtn",
    'input[value*="提交"]',
]


def run_homework(session, cfg, log, popup, report):
    page = session.page
    if cfg.homework.mode == "manual":
        log.info("作业处理模式=manual：仅列出未提交作业，由人工完成")
    else:
        log.info("作业处理模式=auto：将按配置的 answer_text 自动填写并提交")

    # 遍历所有 frame：学习通的章节树/作业入口常驻 iframe，只搜主 frame
    # 会漏掉真实入口（原来误报率极高的根因）。
    try:
        from .course import all_frames
        frames = all_frames(page)
    except Exception:  # noqa: BLE001
        frames = [page]

    opened = False
    for frame in frames:
        for sel in HOMEWORK_TAB_SELECTORS:
            try:
                el = frame.query_selector(sel)
                if el is None or not el.is_visible():
                    continue
                target, _ = session.click_capture(frame, el, wait_s=10)
                page = target
                page.wait_for_timeout(2500)
                # 点击后若确实进入作业页，或页面出现作业条目，才算找到
                cur_url = (page.url or "").lower()
                # 新标签页看 url；同框架/iframe 内跳转则逐 frame 找作业条目
                hit = any(h in cur_url for h in WORK_URL_HINTS)
                if not hit:
                    try:
                        hit = any(_has_work_items(fr) for fr in all_frames(page))
                    except Exception:  # noqa: BLE001
                        hit = _has_work_items(page)
                if hit:
                    opened = True
                    break
                log.debug(f"  作业候选「{sel}」点击后未进入作业页：{page.url}")
            except Exception:  # noqa: BLE001
                continue
        if opened:
            break
    if not opened:
        report.add("作业", "作业入口", "manual", "未找到作业 tab，请手动在课程中查看作业")
        log.warning("未找到课程「作业」入口（已检查所有框架；该课程可能未布置作业）")
        return

    items = []
    for sel in HOMEWORK_ITEM_SELECTORS:
        try:
            found = page.query_selector_all(sel)
        except Exception:  # noqa: BLE001
            continue
        for el in found:
            try:
                if not el.is_visible():
                    continue
                text = (el.inner_text() or "").strip().replace("\n", " ")
                if not text or len(text) > 120:
                    continue
                if any(k in text for k in ("未提交", "未完成", "待提交", "进行中")):
                    items.append(el)
            except Exception:  # noqa: BLE001
                continue
        if items:
            break

    log.info(f"发现 {len(items)} 项未完成的作业")
    if not items:
        report.add("作业", "全部作业", "skipped", "未发现未提交作业")
        return

    for el in items:
        try:
            title = (el.inner_text() or "").strip().replace("\n", " ")[:60]
        except Exception:  # noqa: BLE001
            title = "作业"
        popup.tick(page)
        target, is_new = session.click_capture(page, el, wait_s=10)
        target.wait_for_timeout(3000)
        popup.tick(target)

        if cfg.homework.mode == "manual":
            report.add("作业", title, "manual", "已打开作业页，等待人工完成")
            log.info(f"  作业已打开待人工处理: {title}")
            continue

        # 自动模式：填写文本框并提交
        filled = False
        try:
            boxes = target.query_selector_all('textarea, [contenteditable="true"], input[type=text]')
            for box in boxes:
                try:
                    if box.is_visible():
                        box.fill(cfg.homework.answer_text)
                        filled = True
                        break
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass
        if not filled:
            report.add("作业", title, "manual", "无文本输入框（可能需上传附件），请人工完成")
            log.info(f"  作业无文本输入框，转人工: {title}")
            continue

        submitted = False
        for sel in SUBMIT_SELECTORS:
            try:
                btn = target.query_selector(sel)
                if btn is not None and btn.is_visible():
                    btn.click(timeout=3000)
                    target.wait_for_timeout(2000)
                    for csel in ['button:has-text("确定")', ".layui-layer-btn0", 'button:has-text("确认")']:
                        try:
                            c = target.query_selector(csel)
                            if c is not None and c.is_visible():
                                c.click(timeout=3000)
                                break
                        except Exception:  # noqa: BLE001
                            continue
                    target.wait_for_timeout(3000)
                    submitted = True
                    break
            except Exception:  # noqa: BLE001
                continue
        status = "done" if submitted else "failed"
        report.add("作业", title, status, "自动填写并提交" if submitted else "提交按钮未命中，需人工提交")
        log.info(f"  作业 {title} -> {status}")
        if is_new:
            session.close_page(target)
        time.sleep(1)
