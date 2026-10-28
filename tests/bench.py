# -*- coding: utf-8 -*-
"""性能基准：测量热路径的耗时与 CDP 往返次数（优化前后对比用）。

用法：.venv\\Scripts\\python.exe tests/bench.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import Frame, Page  # noqa: E402

from cxlab.browser import BrowserSession  # noqa: E402
from cxlab.config import load_config  # noqa: E402
from cxlab.logutil import setup_logging  # noqa: E402
from cxlab.popups import PopupGuard  # noqa: E402
from cxlab import course as course_mod  # noqa: E402
from cxlab import document as document_mod  # noqa: E402
from cxlab import video as video_mod  # noqa: E402

CALLS = {"evaluate": 0, "query_selector": 0, "query_selector_all": 0, "inner": 0}


def instrument():
    """统计 CDP 往返：evaluate / query_selector(_all)。"""
    for cls in (Page, Frame):
        for name, key in (("evaluate", "evaluate"),
                          ("query_selector", "query_selector"),
                          ("query_selector_all", "query_selector_all")):
            if not hasattr(cls, name):
                continue
            orig = getattr(cls, name)
            if getattr(orig, "_counted", False):
                continue

            def make(o):
                def wrapper(self, *a, **k):
                    CALLS[key] += 1
                    return o(self, *a, **k)
                wrapper._counted = True
                return wrapper
            setattr(cls, name, make(orig))


def reset():
    for k in CALLS:
        CALLS[k] = 0


def total_calls():
    return sum(CALLS.values())


def bench(label, fn, times=1):
    reset()
    t0 = time.perf_counter()
    for _ in range(times):
        fn()
    dt = (time.perf_counter() - t0) / times
    print(f"  {label:<34} 单次 {dt*1000:7.1f} ms   CDP 往返 {total_calls()//times:>5} 次")
    return dt


def main():
    instrument()
    cfg = load_config(str(ROOT / "config.yaml"))
    cfg["browser"]["headless"] = True
    log = setup_logging(cfg.log_dir, "WARNING")
    session = BrowserSession(cfg, log)

    cfg["course"]["name_contains"] = "实验室安全"
    course = course_mod.enter_course(session, course_mod.choose_courses(session, cfg, log)[0], cfg, log)
    page = session.page

    print("\n===== 基准测试（优化前）=====")
    chapters = course_mod.discover_chapters(page, log)
    print(f"  章节数: {len(chapters)}")

    n = len(chapters)
    d1 = bench("discover_chapters（强制重解析）", lambda: course_mod.discover_chapters(page, log, force=True), 5)
    print(f"  → 真实解析开销；单轮 66 章节若每章重解析 = {d1*n:.2f} s")
    d1c = bench("discover_chapters（缓存命中）", lambda: course_mod.discover_chapters(page, log), 20)
    print(f"  → 缓存命中路径；单轮 66 章节 = {d1c*n:.3f} s")
    d1v = bench("chapter_state（定点复核，单次 JS）", lambda: course_mod.chapter_state(page, chapters[0].id), 20)
    print(f"  → 每章复核 1 次，66 章 = {d1v*n:.2f} s")

    cid = chapters[0].id
    d2 = bench("chapter_element（定位单个知识点）", lambda: course_mod.chapter_element(page, cid), 5)
    print(f"  → 每章调用 1 次，66 章估算 {d2*n:.2f} s")

    popup = PopupGuard(cfg, log)
    d3 = bench("popup.tick（弹窗看门狗）", lambda: popup.tick(page), 10)
    print(f"  → 视频轮询 3s/次，1 小时约 1200 次，估算 {d3*1200:.1f} s")

    # 视频规则检测（会遍历所有 frame 取 innerText）
    bench("detect_watch_duration + detect_no_speed", lambda: (
        video_mod.detect_watch_duration(page), video_mod.detect_no_speed(page)), 3)

    # 打开一个知识点学习页，测文档/内容帧相关热路径
    frame, el = course_mod.chapter_element(page, chapters[0].id)
    if el is not None:
        page, _ = session.click_capture(page, el, wait_s=10)
        page.wait_for_timeout(5000)
        bench("find_content_frame", lambda: course_mod.find_content_frame(page), 10)
        bench("card_type（任务点类型判定）", lambda: course_mod.card_type(course_mod.find_content_frame(page)), 3)
        bench("document._frame_texts（全帧取文本）", lambda: document_mod._frame_texts(page), 5)

    # ---- 测验题目解析基准（打开一个章节测验页） ----
    try:
        from cxlab import quiz as quiz_mod
        quiz_ch = next((c for c in chapters if "测验" in c.title), None)
        if quiz_ch is not None:
            frame, el = course_mod.chapter_element(page, quiz_ch.id)
            page, _ = session.click_capture(page, el, wait_s=10)
            page.wait_for_timeout(9000)
            qf = None
            for _ in range(6):
                qf = next((f for f in course_mod.all_frames(page)
                           if "dohomework" in (f.url or "").lower()), None)
                if qf is not None:
                    break
                page.wait_for_timeout(2000)
            if qf is not None:
                bench("quiz.extract_questions（题目解析）", lambda: quiz_mod.extract_questions(qf, log), 3)
    except Exception as exc:  # noqa: BLE001
        print(f"  测验解析基准跳过: {str(exc)[:60]}")

    session.close()

    # ---- 题库查询基准（合成 5000 条，检查数据结构/查询效率） ----
    import json
    import tempfile
    from cxlab.answers import QuestionBank

    tmp = Path(tempfile.mkdtemp()) / "big_bank.json"
    big = {f"题目{i}：以下关于实验室安全的说法正确的是": f"{'ABC'[i % 3]}" for i in range(5000)}
    tmp.write_text(json.dumps({"questions": [
        {"stem": k, "answer": v, "type": "single"} for k, v in big.items()
    ]}, ensure_ascii=False), encoding="utf-8")
    bank = QuestionBank(str(tmp), str(tmp.with_suffix(".pool.json")), log)

    import time as _t
    def timeit(fn, n=200):
        t0 = _t.perf_counter()
        for _ in range(n):
            fn()
        return (_t.perf_counter() - t0) / n * 1000

    exact = timeit(lambda: bank.lookup("题目2500：以下关于实验室安全的说法正确的是"))
    fuzzy = timeit(lambda: bank.lookup("题目9999：完全不同的题干内容XYZ"))
    print("\n===== 题库查询（5000 条）=====")
    print(f"  精确命中      {exact:7.3f} ms/次")
    print(f"  未命中(模糊)  {fuzzy:7.3f} ms/次")
    print("\n[OK] 基准测试完成")


if __name__ == "__main__":
    main()
