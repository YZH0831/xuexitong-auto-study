# -*- coding: utf-8 -*-
"""从真实答题页抓取「加密字体 + 解码前后文本」，固化为回归测试用例。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cxlab.browser import BrowserSession  # noqa: E402
from cxlab.config import load_config  # noqa: E402
from cxlab.logutil import setup_logging  # noqa: E402
from cxlab import course as course_mod  # noqa: E402
from cxlab import quiz as quiz_mod  # noqa: E402
from cxlab.font_decoder import extract_font_css, FontDecoder  # noqa: E402

cfg = load_config(str(ROOT / "config.yaml"))
cfg["browser"]["headless"] = True
log = setup_logging(cfg.log_dir, "INFO")
session = BrowserSession(cfg, log)

cfg["course"]["name_contains"] = "实验室安全"
course_mod.enter_course(session, course_mod.choose_courses(session, cfg, log)[0], cfg, log)
chapters = course_mod.discover_chapters(session.page, log)
quiz_ch = next(c for c in chapters if "测验" in c.title)
page = session.page
frame, el = course_mod.chapter_element(page, quiz_ch.id)
page, _ = session.click_capture(page, el, wait_s=12)
page.wait_for_timeout(10000)

qf = None
for _ in range(8):
    qf = next((f for f in course_mod.all_frames(page) if "dohomework" in (f.url or "").lower()), None)
    if qf is not None:
        break
    page.wait_for_timeout(2000)

html = qf.evaluate("() => document.documentElement.outerHTML") or ""
css = extract_font_css(html)
questions = quiz_mod.extract_questions(qf, log)
before = [q.stem for q in questions]
quiz_mod.decode_font_texts(qf, questions, cfg, log)
after = [q.stem for q in questions]

fixture = {
    "note": "由 tests/_tmp_capture_fixture.py 从真实答题页抓取；用于回归测试字体解码",
    "font_css": css,
    "cases": [[b, a] for b, a in zip(before, after)],
}
out = ROOT / "tests" / "fixtures"
out.mkdir(parents=True, exist_ok=True)
(out / "font_decode_cases.json").write_text(json.dumps(fixture, ensure_ascii=False, indent=1), encoding="utf-8")
print("fixture 已保存:", (out / 'font_decode_cases.json'), (out / 'font_decode_cases.json').stat().st_size, "bytes")
print("用例数:", len(fixture["cases"]))
for b, a in fixture["cases"][:2]:
    print("  解码前:", b[:40])
    print("  解码后:", a[:40])

session.close()
