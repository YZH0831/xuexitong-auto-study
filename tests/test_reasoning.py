# -*- coding: utf-8 -*-
"""本地推理作答测试（无需 API Key）：证据语义匹配 + 判断题判定语 + 置信度弃权。"""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 本地用 config.yaml（含真实 Key），CI 里没有该文件 → 回退到示例配置
CFG_PATH = ROOT / "config.yaml"
if not CFG_PATH.exists():
    CFG_PATH = ROOT / "config.example.yaml"
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.WARNING, format="%(message)s")
log = logging.getLogger("test")

from cxlab import reasoning as R  # noqa: E402
from cxlab.config import Config, load_config  # noqa: E402
from cxlab.online_answer import OnlineAnswer  # noqa: E402

OPTS = [{"text": "带电作业的工作场所", "letter": "A"}, {"text": "坑穴或密闭场所", "letter": "B"},
        {"text": "高空作业", "letter": "C"}, {"text": "静电", "letter": "D"}]
JOPTS = [{"text": "对", "letter": "TR"}, {"text": "错", "letter": "FA"}]

# ---------------- 1) 选择题：解析语义指向哪个选项 ----------------
ev = "解析：金属梯子导电，在带电作业的工作场所使用会发生触电事故，因此不适于带电作业的工作场所。"
ans, conf = R.decide_choice(ev, OPTS, "single")
assert ans == "A" and conf >= 0.7, (ans, conf)
print(f"[1] 单选按解析语义选中 A（置信 {conf}）OK")

# 证据与所有选项都不贴 → 弃权
ans, conf = R.decide_choice("解析：本题考查一般常识，与题目无关。", OPTS, "single")
assert ans is None and conf == 0, (ans, conf)
print("[2] 证据不足 → 弃权 OK")

# 两个选项分数接近 → 弃权（不硬选）
close = "解析：高空作业和静电都可能有关系，高空作业要注意，静电也要注意。"
ans, _ = R.decide_choice(close, OPTS, "single")
assert ans is None, ans
print("[3] 选项分数接近 → 弃权 OK")

# ---------------- 2) 判断题：判定语 ----------------
cases = [
    ("解析：该说法应改为在通风橱内操作，敞口操作不安全。", 1),
    ("解析：该做法符合实验室安全规范，因此表述正确。", 0),
    ("解析：该说法过于绝对，实际上并非所有金属梯子都不能用。", 1),
    ("解析：该说法不准确，漏掉了必须佩戴护目镜这一条。", 1),
]
for text, expect in cases:
    idx, conf = R.decide_judge(text, "所有金属梯子都禁止使用")
    assert idx == expect, (text, idx, expect)
print("[4] 判断题判定语（应改为/符合/过于绝对/不准确）OK")

# 无判定语 → 弃权
idx, conf = R.decide_judge("解析：本题涉及实验室安全常识。")
assert idx is None
print("[5] 判断题无判定语 → 弃权 OK")

# 判断题 → 映射到这道课的 TR/FA 字母
ans, conf, why = R.decide("要定期清理油污", JOPTS, "解析：该说法符合安全规范，表述正确。", "judge")
assert ans == "TR" and conf >= 0.6, (ans, conf, why)
print(f"[6] 判断题映射到 TR/FA 字母（{ans}）OK")

# ---------------- 3) 端到端：搜索页只有解析、没有"答案：X" ----------------
class FakeOA(OnlineAnswer):
    def __init__(self, cfg, log, text1, text2=""):
        super().__init__(cfg, log)
        self._t1, self._t2 = text1, text2

    def _fetch_text(self, context, query, engine=None):
        return self._t1 if not query.startswith("作业帮") else self._t2

    def can_search(self):
        return True, ""


cfg = Config(load_config(str(CFG_PATH)))
cfg["quiz"]["online_search"]["min_interval_s"] = 0
cfg["quiz"]["online_search"]["skip_when_obfuscated"] = False

# 7a) 场景A：解析里重复了正确选项原文 → 走"选项原文命中"（等价可靠），答案必须是 A
STEM = "金属梯子不适于以下什么工作场所"
page_a = (f"某题库 AI解题 {STEM} A 带电作业的工作场所 B 坑穴或密闭场所 C 高空作业 D 静电 "
          "解析：金属梯子导电，带电作业的工作场所会发生触电，故不适于带电作业的工作场所。")
r = FakeOA(cfg, log, page_a, "无关内容").search_cross(None, STEM, OPTS, "single")
assert r["answer"] in ("A", "带电作业的工作场所"), r
print(f"[7a] 解析复述选项原文 → 答出带电作业（{r['source']}）OK")

# 7b) 场景B：判断题，页面只有解析判定语、没有任何"答案：X" → 必须由本地推理接管
JSTEM = "要定期对压缩空气桶清理油污，以防止油类和空气形成爆炸性气体"
page_b = (f"某题库 AI解题 {JSTEM} （） 解析：该说法符合压力容器的安全维护要求，"
          "油污与压缩空气混合确有爆炸风险，因此表述正确。")
r = FakeOA(cfg, log, page_b, "无关内容").search_cross(None, JSTEM, JOPTS, "judge")
# 判定语可能被 AI 块模式先命中，也可能是推理接管——只要求答案正确
assert r["answer"] == "TR", r
print(f"[7b] 判断题（无答案标记，靠解析判定语）→ TR（{r['source']}）OK")

# 7c) 必须由推理接管的场景：证据里没有任何"答案/因此…正确"类判定语，只有语义描述
page_c = (f"AI解题 {STEM} A 带电作业的工作场所 B 坑穴或密闭场所 C 高空作业 D 静电 "
          "金属梯子导电性良好，在带电作业场合使用会产生触电危险。")
r = FakeOA(cfg, log, page_c, "无关内容").search_cross(None, STEM, OPTS, "single")
# 没有"解析"标记、证据里还混着选项列表 → 分不开就弃权。关键：绝不能给出错的字母
assert r["answer"] in (None, "A", "带电作业的工作场所"), r
print(f"[7c] 无解析标记 → {r['answer'] or '弃权'}（安全行为，不瞎选）OK")

print("\n[OK] 本地推理作答测试全部通过")
