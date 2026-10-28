# -*- coding: utf-8 -*-
"""双源核对（通用搜索 + 作业帮）测试：一致 / 冲突采信 / 单源 / 均无。"""
import logging
import sys
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
from cxlab.online_answer import (  # noqa: E402
    OnlineAnswer, anchor_score, evidence_snippet, resolve_letters, same_answer,
)

OPTS = [{"text": "带电作业的工作场所"}, {"text": "坑穴或密闭场所"},
        {"text": "高空作业"}, {"text": "静电"}]
STEM = "金属梯子不适于以下什么工作场所"


def make_oa(text_search="", text_zyb="", policy="prefer_zuoyebang"):
    cfg = Config(load_config(str(CFG_PATH)))
    cfg["quiz"]["online_search"]["min_interval_s"] = 0      # 测试不等待
    cfg["quiz"]["online_search"]["skip_when_obfuscated"] = False
    cfg["quiz"]["online_search"]["conflict_policy"] = policy
    cfg["quiz"]["online_search"]["cross_check"] = True
    oa = OnlineAnswer(cfg, log)
    oa._fetch_text = lambda ctx, q, engine=None: (text_zyb if q.startswith("作业帮") else text_search)
    return oa


# ---------------- 1) 双源一致 → 判定有效 ----------------
oa = make_oa(f"题库页 {STEM} A 带电作业的工作场所 B 坑穴或密闭场所 C 高空作业 D 静电 正确答案：B 解析…",
              f"作业帮 {STEM} A 带电作业的工作场所 B 坑穴或密闭场所 答案：B 解析…")
r = oa.search_cross(None, STEM, OPTS, "single")
assert r["agree"] is True and r["answer"] == "B", r
assert r["source"] == "cross_checked", r
print("[1] 双源一致 → 判定有效（source=cross_checked）OK")

# ---------------- 2) 字母顺序不同也算一致（ABD == BDA；
# 且源页选项乱序时，对齐后指向同一内容的两个字母也判一致）----------------
oa = make_oa(f"{STEM} A 带电作业的工作场所 B 坑穴或密闭场所 C 高空作业 D 静电 正确答案：ABD",
              f"作业帮 {STEM} A 带电作业的工作场所 B 坑穴或密闭场所 C 高空作业 D 静电 答案：BDA")
r = oa.search_cross(None, STEM, OPTS, "multi")
assert r["agree"] is True and set(r["answer"]) == {"A", "B", "D"}, r
print("[2] 字母顺序无关的一致判定 OK")

# ---------------- 3) 冲突 + 命中作业帮站点 → 采信作业帮（默认策略） ----------------
oa = make_oa(f"某站 {STEM} A 带电作业的工作场所 B 坑穴或密闭场所 正确答案：A",
              f"作业帮 www.zybang.com {STEM} A 带电作业的工作场所 B 坑穴或密闭场所 本题答案：B")
r = oa.search_cross(None, STEM, OPTS, "single")
assert r["agree"] is False, r
assert r["answer"] == "B", r
assert r["source"] == "online_conflict", r
assert r["zyb_hit"] is True and r["ev1"] and r["ev2"], "应给出双方依据片段"
print("[3] 冲突→命中作业帮站点→采信作业帮，并带双方依据 OK")

# ---------------- 4) 冲突 + prefer_search → 采信通用搜索 ----------------
oa = make_oa(f"某站 {STEM} A 带电作业的工作场所 B 坑穴或密闭场所 正确答案：A",
              f"作业帮 zybang.com {STEM} A 带电作业的工作场所 B 坑穴或密闭场所 答案：B", policy="prefer_search")
r = oa.search_cross(None, STEM, OPTS, "single")
assert r["answer"] == "A", r
print("[4] 冲突 + prefer_search → 采信通用搜索 OK")

# ---------------- 5) 冲突 + manual → 不采信（转人工） ----------------
oa = make_oa(f"某站 {STEM} A 带电作业的工作场所 B 坑穴或密闭场所 正确答案：A",
              f"作业帮 zybang.com {STEM} A 带电作业的工作场所 B 坑穴或密闭场所 答案：B", policy="manual")
r = oa.search_cross(None, STEM, OPTS, "single")
assert r["answer"] is None, r
assert "冲突" in r["note"], r
print("[5] 冲突 + manual → 不采信并提示转人工 OK")

# ---------------- 6) 冲突但未确认作业帮站点 → 暂采信通用搜索 ----------------
oa = make_oa(f"某站 {STEM} A 带电作业的工作场所 B 坑穴或密闭场所 正确答案：A",
              f"普通站 {STEM} A 带电作业的工作场所 B 坑穴或密闭场所 答案：B")
r = oa.search_cross(None, STEM, OPTS, "single")
assert r["answer"] == "A" and r["zyb_hit"] is False, r
print("[6] 冲突且作业帮来源未确认 → 暂采信通用搜索 OK")

# ---------------- 7) 仅单源有结果 → 记为未验证 ----------------
oa = make_oa(f"某站 {STEM} A 带电作业的工作场所 B 坑穴或密闭场所 正确答案：A", "没有相关内容")
r = oa.search_cross(None, STEM, OPTS, "single")
assert r["answer"] == "A" and r["agree"] is False and r["source"] == "online", r
print("[7] 仅单源有结果 → 不满足双源一致，记为未验证 OK")

# ---------------- 8) 双源均无 → 转人工 ----------------
oa = make_oa("无关内容", "无关内容")
r = oa.search_cross(None, STEM, OPTS, "single")
assert r["answer"] is None and r["agree"] is False, r
print("[8] 双源均无结果 → 转人工作答 OK")

# ---------------- 9) 工具函数 ----------------
assert same_answer("ABD", "BDA") and same_answer("答案：A", "A")
assert not same_answer("A", "B")
assert "答案" in evidence_snippet("前面内容 正确答案：A 后面内容", "A")

# 文本 ↔ 字母 等价（"以上都是" 就是选项 D）
opts4 = [{"text": "热传导"}, {"text": "热对流"}, {"text": "热辐射"}, {"text": "以上都是"}]
assert resolve_letters("以上都是", opts4) == {"D"}, resolve_letters("以上都是", opts4)
assert resolve_letters("D", opts4) == {"D"}
assert same_answer("以上都是", "D", opts4), "文本答案与其字母应判为等价"
assert not same_answer("以上都是", "A", opts4)
# 答案块评分：离本题选项近的答案得分更高
page_ok = "前文无关 火灾蔓延的途径是 A 热传导 B 热对流 C 热辐射 D 以上都是 答案：D 解析"
page_bad = "前文无关 答案：A 别的题目 内容内容"
assert anchor_score(page_ok, opts4, "D") >= 3
assert anchor_score(page_bad, opts4, "A") <= 1
print("[9] same_answer / resolve_letters / anchor_score / evidence_snippet OK")

print("\n[OK] 双源核对（通用搜索 + 作业帮）测试全部通过")
