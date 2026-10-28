# -*- coding: utf-8 -*-
"""课程状态/截止日期判定的回归测试（纯逻辑，无需浏览器）。"""
import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 本地用 config.yaml（含真实 Key），CI 里没有该文件 → 回退到示例配置
CFG_PATH = ROOT / "config.yaml"
if not CFG_PATH.exists():
    CFG_PATH = ROOT / "config.example.yaml"
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("test")

from cxlab.config import Config, load_config  # noqa: E402
from cxlab.course import (  # noqa: E402
    classify_course, filter_by_status, parse_course_dates,
)

TODAY = date(2026, 10, 14)

# ---------------- 1) 日期解析 ----------------
cases = [
    ("示例大学实验室安全 teacher 开课时间：2026-09-01～2026-12-31", date(2026, 9, 1), date(2026, 12, 31)),
    ("2026秋某思政课 第6次开课 开课时间：2026-08-28~2027-03-28", date(2026, 8, 28), date(2027, 3, 28)),
    ("某某课程 开课时间：2025-03-01 至 2025-06-30 张老师", date(2025, 3, 1), date(2025, 6, 30)),
    ("某课程 截止日期：2026-01-15", None, date(2026, 1, 15)),
    ("某专业课 大一第一学期开课 张老师", None, None),
    ("某通识课程 李老师", None, None),
]
for text, want_start, want_end in cases:
    got_start, got_end = parse_course_dates(text)
    assert got_start == want_start, f"开课日期不符: {text!r} → {got_start}（期望 {want_start}）"
    assert got_end == want_end, f"结束日期不符: {text!r} → {got_end}（期望 {want_end}）"
print(f"[1] 日期解析 OK（{len(cases)} 条，含全角～/半角~/中文“至”）")

# ---------------- 2) 状态判定 ----------------
cfg = Config(load_config(str(CFG_PATH)))


def status(card, excluded=None, **over):
    c = Config(load_config(str(CFG_PATH)))
    for k, v in over.items():
        c["course"][k] = v
    return classify_course(card, c, excluded=excluded, today=TODAY)[0]


active_card = {"name": "示例大学实验室安全", "course_id": "100000001",
               "text": "示例大学实验室安全 开课时间：2026-09-01～2026-12-31"}
past_card = {"name": "某过期课", "course_id": "111",
             "text": "某过期课 开课时间：2025-03-01～2025-06-30"}
future_card = {"name": "某未开课", "course_id": "222",
               "text": "某未开课 开课时间：2026-12-01～2027-03-01"}
nodate_card = {"name": "某通识课程", "course_id": "100000004",
               "text": "某通识课程 李老师"}

assert status(active_card) == "active"
assert status(past_card) == "ended"
assert status(future_card) == "not_started"
assert status(nodate_card) == "unknown"
# 平台标记优先：名义结束日期在未来，但平台已把它移入「已结束」
assert status(active_card, excluded={"100000001": "已结束课程"}) == "ended"
assert status(nodate_card, excluded={"100000004": "已退课课程"}) == "dropped"
# 手工覆盖
assert status(past_card, deadline_overrides={"某过期课": "忽略"}) == "active"
assert status(past_card, deadline_overrides={"某过期课": "2027-01-01"}) == "active"
assert status(active_card, deadline_overrides={"实验室安全": "2026-10-01"}) == "ended"
# 关掉日期判定 → 过期课仍处理
assert status(past_card, skip_past_deadline=False) == "active"
print("[2] 状态判定 OK（平台标记 > 手工覆盖 > 日期范围 > 未知）")

# ---------------- 3) 过滤策略 ----------------
cards = [dict(active_card), dict(past_card), dict(future_card), dict(nodate_card)]
for c in cards:
    c["status"], c["status_reason"] = classify_course(c, cfg, today=TODAY)

kept, dropped = filter_by_status(cards, cfg, log)
# 默认：过期课被排除；未开课与无日期的课程保留（可通过配置收紧）
assert [c["name"] for c in kept] == ["示例大学实验室安全", "某未开课", "某通识课程"], \
    [c["name"] for c in kept]
assert [c["name"] for c in dropped] == ["某过期课"], [c["name"] for c in dropped]

cfg_ns = Config(load_config(str(CFG_PATH)))
cfg_ns["course"]["skip_not_started"] = True
kept_ns, dropped_ns = filter_by_status([dict(c) for c in cards], cfg_ns, log)
assert "某未开课" not in [c["name"] for c in kept_ns], "skip_not_started=True 时应排除未开课课程"
assert len(dropped_ns) == 2, [c["name"] for c in dropped_ns]

cfg2 = Config(load_config(str(CFG_PATH)))
cfg2["course"]["deadline_policy"] = "mark"
kept2, dropped2 = filter_by_status([dict(c) for c in cards], cfg2, log)
assert len(kept2) == 4 and not dropped2, "mark 策略应保留全部课程只做标记"

cfg3 = Config(load_config(str(CFG_PATH)))
cfg3["course"]["unknown_status_policy"] = "skip"
kept3, dropped3 = filter_by_status([dict(c) for c in cards], cfg3, log)
assert all(c["name"] != "某通识课程" for c in kept3), "unknown=skip 时应排除无日期课程"
print("[3] 过滤策略 OK（skip / mark / unknown=skip 三种模式）")

print("\n[OK] 截止日期与课程状态判定测试全部通过")
