# -*- coding: utf-8 -*-
"""2026-10-19 回归：多选题只选 1 个选项（生产日志 run_20261019_210305.log:21:24:25）。

真实失败形态：
  题干：中国军队应坚决维护（），为全面建成小康社会、实现中华民族伟大复兴提供坚强保障。
  源① 通用搜索 → 只抽出第 1 个选项「中国共产党的领导和中国特色社会主义制度」
  源② 作业帮   → 抽出全部 4 个选项
  旧逻辑：kind/hit 判定采信源① → "多选题已选 1 项：A"
"""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 本地用 config.yaml（含真实 Key），CI 里没有该文件 → 回退到示例配置
CFG_PATH = ROOT / "config.yaml"
if not CFG_PATH.exists():
    CFG_PATH = ROOT / "config.example.yaml"
sys.path.insert(0, str(ROOT))

from cxlab.config import Config, load_config  # noqa: E402
from cxlab.online_answer import OnlineAnswer, resolve_letters  # noqa: E402

logging.basicConfig(level=logging.CRITICAL)
log = logging.getLogger("t")

STEM = "中国军队应坚决维护（），为全面建成小康社会、实现中华民族伟大复兴提供坚强保障。"
OPTS = [
    {"text": "中国共产党的领导和中国特色社会主义制度", "letter": "A"},
    {"text": "坚决维护国家主权、安全、发展利益", "letter": "B"},
    {"text": "坚决维护国家发展的重要战略机遇期", "letter": "C"},
    {"text": "坚决维护地区与世界和平", "letter": "D"},
]
ALL = {"A", "B", "C", "D"}


def make_oa(text_search="", text_zyb=""):
    cfg = Config(load_config(str(CFG_PATH)))
    cfg["quiz"]["online_search"]["min_interval_s"] = 0
    cfg["quiz"]["online_search"]["skip_when_obfuscated"] = False
    cfg["quiz"]["online_search"]["cross_check"] = True
    oa = OnlineAnswer(cfg, log)
    oa._fetch_text = lambda ctx, q, engine=None: (text_zyb if q.startswith("作业帮") else text_search)
    return oa


# ---------- 1) 源①页面只有第 1 项重复出现，但答案句是连续四项：应自动补全 ----------
t1 = (
    f"某题库 {STEM} 解析：中国军队有效履行新的历史时期军队使命，"
    f"坚决维护中国共产党的领导和中国特色社会主义制度，"
    f"坚决维护国家主权、安全、发展利益，"
    f"坚决维护国家发展的重要战略机遇期，"
    f"坚决维护地区与世界和平。"
    f" 相关：中国共产党的领导和中国特色社会主义制度"
)
oa = make_oa("", "")
a1 = oa._extract(t1, OPTS, "multi", STEM)
assert a1 is not None and resolve_letters(a1, OPTS) == ALL, f"源①应补全 4 项，实际 {a1!r}"
print(f"[1] 多选答案句自动补全 OK -> {a1}（{sorted(resolve_letters(a1, OPTS))}）")

# ---------- 2) 双源：源①补全后与源②一致 → 不再误采信单项 ----------
t2 = (
    f"作业帮 {STEM} "
    f"答案：中国共产党的领导和中国特色社会主义制度、坚决维护国家主权、安全、发展利益、"
    f"坚决维护国家发展的重要战略机遇期、坚决维护地区与世界和平"
)
r = make_oa(t1, t2).search_cross(None, STEM, OPTS, "multi")
letters = resolve_letters(r["answer"], OPTS)
assert r["agree"] is True, r
assert letters == ALL, f"双源应一致采信 4 项，实际 {r['answer']!r}"
print(f"[2] 双源一致采信全部选项 OK -> {r['answer']}（{sorted(letters)}）")

# ---------- 3) 兜底：万一源①仍只抽到 1 项，冲突时优先"更完整的超集" ----------
t1_single = f"某站 {STEM} 答案：中国共产党的领导和中国特色社会主义制度"
r2 = make_oa(t1_single, t2).search_cross(None, STEM, OPTS, "multi")
letters2 = resolve_letters(r2["answer"], OPTS)
assert letters2 == ALL, f"多选冲突应采信超集，实际 {r2['answer']!r}"
print(f"[3] 多选冲突优先完整答案 OK -> {r2['answer']}（{sorted(letters2)}）")

print()
print("[OK] 2026-10-19 多选题只选 1 项回归通过")
