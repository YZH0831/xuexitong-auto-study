# -*- coding: utf-8 -*-
"""新增功能自测：课程单选/多选解析、拟人化延时、联网搜题解析与限额、安全限制。"""
import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 本地用 config.yaml（含真实 Key），CI 里没有该文件 → 回退到示例配置
CFG_PATH = ROOT / "config.yaml"
if not CFG_PATH.exists():
    CFG_PATH = ROOT / "config.example.yaml"
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("test")

from cxlab.config import Config, load_config  # noqa: E402
from cxlab.course import (  # noqa: E402
    is_all_spec, label_for, label_index, match_course, parse_index_spec, split_tokens,
)
from cxlab.humanize import Humanizer  # noqa: E402
from cxlab.online_answer import OnlineAnswer, looks_obfuscated  # noqa: E402
from cxlab.runner import Runner  # noqa: E402

cfg = Config(load_config(str(CFG_PATH)))

# ---------------- 1) 课程选择解析 ----------------
assert split_tokens("实验室安全, 高等数学") == ["实验室安全", "高等数学"]
assert split_tokens("实验室安全，高等数学") == ["实验室安全", "高等数学"]   # 中文逗号
assert split_tokens(["a", " b "]) == ["a", "b"]
assert split_tokens("") == []

card_a = {"name": "示例大学实验室安全", "course_id": "100000001", "clazzid": "200000002"}
card_b = {"name": "高等数学C园艺", "course_id": "267530203", "clazzid": "155823758"}
assert match_course(card_a, "实验室安全")          # 名称片段
assert match_course(card_a, "100000001")           # courseId
assert match_course(card_a, "200000002")           # clazzid
assert not match_course(card_b, "实验室安全")
print("[1] 课程单选/多选解析 OK")

# ---------------- 1b) 字母标签与多选表达式 ----------------
assert label_for(1) == "A" and label_for(26) == "Z" and label_for(27) == "AA"
assert label_index("A") == 1 and label_index("Z") == 26 and label_index("AA") == 27
assert label_index("") is None and label_index("A1") is None

assert parse_index_spec("A C F", 10) == {1, 3, 6}      # 字母多选（空格分隔）
assert parse_index_spec("A,F", 10) == {1, 6}           # 字母多选（逗号分隔）
assert parse_index_spec("A-C", 10) == {1, 2, 3}        # 字母区间
assert parse_index_spec("1,3,5", 10) == {1, 3, 5}      # 数字多选
assert parse_index_spec("2-4", 10) == {2, 3, 4}        # 数字区间
assert parse_index_spec("1,A", 10) == {1}              # 混合（1 与 A 同为第 1 个）
assert parse_index_spec("all", 5) == {1, 2, 3, 4, 5}
assert parse_index_spec("", 5) == {1, 2, 3, 4, 5}      # 空 = 全部
assert parse_index_spec("99", 5) == set()              # 越界被过滤
assert is_all_spec("all") and is_all_spec("") and is_all_spec("全部")
assert not is_all_spec("A"), "字母 A 是有效课程标签，不能被当作全选"
print("[1b] 字母标签 + 课时多选表达式（A C F / A-C / 1,3,5-8 / all）OK")

# ---------------- 2) 拟人化延时 ----------------
h = Humanizer(cfg, log)
assert h.enabled, "humanize 应默认开启"
samples = []
for _ in range(5):
    t0 = time.time()
    h.pause("click_before_ms", (200, 900))
    samples.append((time.time() - t0) * 1000)
assert all(150 <= s <= 1100 for s in samples), samples
assert len(set(round(s) for s in samples)) > 1, "延时应带随机性"
t0 = time.time()
h.task_interval(3)
dt = time.time() - t0
assert 2.0 <= dt <= 5.5, dt
print(f"[2] 拟人化 OK：点击前延时样本 {[round(s) for s in samples]} ms，任务间隔 {dt:.1f}s")

# ---------------- 3) 联网搜题（只测解析与限额，不发真实请求） ----------------
oa = OnlineAnswer(cfg, log)
assert oa.enabled
opts = [{"text": "A 保护优先"}, {"text": "B 预防为主"}, {"text": "C 综合治理"}]
# 新约束：答案必须出现在题干附近（模拟真实搜索结果：题干与答案同页）
# 且源页字母须经"选项文本对齐"换算（源页选项顺序可能与我们的不同）
STEM_T = "我国新《环境保护法》第五条规定"
OPTS_AB = [{"text": "A 政府为主", "letter": "A"}, {"text": "B 公众参与", "letter": "B"}]
# 我们的选项（顺序与源页可能不同）
opts_c = [{"text": "保护优先", "letter": "A"}, {"text": "预防为主", "letter": "B"},
          {"text": "综合治理", "letter": "C"}, {"text": "公众参与", "letter": "D"}]
# 源页选项与我们同序 → 字母直接可用（源C=综合治理=我们的C）
page_same = f"某题库 {STEM_T} A 保护优先 B 预防为主 C 综合治理 D 公众参与 正确答案：C 解析…"
assert oa._extract(page_same, opts_c, stem=STEM_T) == "C", oa._extract(page_same, opts_c, stem=STEM_T)
# 源页选项乱序（它把"综合治理"排在A）→ 源答案A应映射到我们的C
page_shuf = f"某库 {STEM_T} A 综合治理 B 预防为主 C 保护优先 D 公众参与 答案：A"
assert oa._extract(page_shuf, opts_c, stem=STEM_T) == "C", oa._extract(page_shuf, opts_c, stem=STEM_T)
# 源D=公众参与=我们的D
page_shuf2 = f"某库 {STEM_T} A 综合治理 B 预防为主 C 保护优先 D 公众参与 答案：D"
assert oa._extract(page_shuf2, opts_c, stem=STEM_T) == "D"
assert oa._extract(f"{STEM_T} A 政府为主 B 公众参与 答案 是 AB 。", OPTS_AB,
                   stem=STEM_T) == "AB"
assert oa._extract("里面没有答案标记", opts, stem=STEM_T) is None
# 页面里没有本题题干 → 裸字母答案不采信（避免从热搜/别的题误抓）
assert oa._extract("热搜榜 1 某新闻 2 某新闻 正确答案：C", opts, stem=STEM_T) is None
# 题干在页面上但答案离得很远（>600 字）→ 也不采信
assert oa._extract(STEM_T + "填充" * 800 + " 正确答案：C", opts, stem=STEM_T) is None
assert oa._extract("命中原文：综合治理 综合治理", opts, stem=STEM_T) == "综合治理"
assert looks_obfuscated("我国新《环糵保护孾》宎五条宄宍") is True
assert looks_obfuscated("我国新《环境保护法》第五条") is False
oa.max_searches = 1
oa.count = 0
assert oa.can_search()[0] is True
oa.count = 1
assert oa.can_search()[0] is False
print("[3] 联网搜题解析/限额 OK")

# ---------------- 4) 安全限制 ----------------
cfg2 = Config(load_config(str(CFG_PATH)))
args = argparse.Namespace(select="", pick=False, list_courses=False, dry_run=True,
                          only="", headless=True, course="", config="config.yaml")
runner = Runner(cfg2, log, args)
runner.start_ts = time.time()
assert runner._limit_reached() is False
cfg2["safety"]["max_tasks_per_run"] = 2
runner.task_count = 2
assert runner._limit_reached() is True and "任务点" in runner.stop_reason
cfg2["safety"]["max_tasks_per_run"] = 0
cfg2["safety"]["max_runtime_min"] = 0.02          # 1.2 秒
runner.start_ts = time.time() - 2
assert runner._limit_reached() is True and "时长" in runner.stop_reason
print("[4] 安全限制 OK（任务点数上限 / 运行时长上限）")

print("\n[OK] 新增功能自测全部通过")
