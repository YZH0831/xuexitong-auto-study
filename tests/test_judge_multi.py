# -*- coding: utf-8 -*-
"""判断题必答 + 多选补全 测试。"""
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
log = logging.getLogger("t")

from cxlab import reasoning as R  # noqa: E402
from cxlab.config import Config, load_config  # noqa: E402
from cxlab.online_answer import OnlineAnswer  # noqa: E402
from cxlab.quiz import fill_answer, Question  # noqa: E402


# ---------- 1) 判断题兜底先验 ----------
g1 = R.judge_best_guess("所有金属梯子都可以在带电作业场所使用")
assert g1[0] == 1 and g1[1] >= 0.6, g1          # 绝对化 → 错
g2 = R.judge_best_guess("实验结束后必须关闭电源和气源")
assert g2[0] == 0 and g2[1] >= 0.55, g2         # 规范表述 → 对
g3 = R.judge_best_guess("苹果是一种常见水果")
assert g3[0] in (0, 1) and g3[1] >= 0.5, g3     # 无线索也要给出判断+置信度
print("[1] 判断题先验（绝对化→错 / 规范→对 / 无线索→仍给判断）OK")


# ---------- 2) 判断题兜底接进 run_quiz 的形态：能选中 TR/FA ----------
class E:
    def __init__(s, t): s.t = t; s.clicked = False
    def is_visible(s): return True
    def click(s, **k): s.clicked = True
    def query_selector(s, *a, **k): return None


opts = [{"text": "对", "letter": "TR", "el": E("TR")}, {"text": "错", "letter": "FA", "el": E("FA")}]
q = Question("所有梯子都可用于带电作业", "judge", opts, E("c"))
v, conf, why = R.judge_best_guess(q.stem)
hit = next((o for o in opts if (v == 0 and "对" in o["text"]) or (v == 1 and "错" in o["text"])), None)
assert hit is not None and fill_answer(q, hit["letter"], log) and hit["el"].clicked
print(f"[2] 兜底答案能真正点上去（{hit['letter']}，置信 {conf}）OK")


# ---------- 3) 判断题多路搜索：两源失败 → 变体命中 ----------
class FakeOA(OnlineAnswer):
    def __init__(self, cfg, log):
        super().__init__(cfg, log)
        self.calls = []

    def _fetch_text(self, context, query, engine=None):
        self.calls.append(query[:14])
        if "判断" in query or "对吗" in query:        # 只有变体查询能搜到
            return "某站 判断题 所有梯子都可用于带电作业 答案：错 解析：带电作业需绝缘梯"
        return "无关内容"


cfg = Config(load_config(str(CFG_PATH)))
cfg["quiz"]["online_search"]["min_interval_s"] = 0
cfg["quiz"]["online_search"]["skip_when_obfuscated"] = False
oa = FakeOA(cfg, log)
JO = [{"text": "对", "letter": "TR"}, {"text": "错", "letter": "FA"}]
r = oa.search_cross(None, "所有梯子都可用于带电作业", JO, "judge")
assert r["answer"] == "FA", r
assert any("判断" in c or "对吗" in c for c in oa.calls), oa.calls
print(f"[3] 判断题变体搜索命中（用了 {len(oa.calls)} 次查询）OK")


# ---------- 4) 多选补全：常规查询失败 → 专搜答案列表 ----------
class FakeOA2(OnlineAnswer):
    def __init__(self, cfg, log):
        super().__init__(cfg, log)

    def _fetch_text(self, context, query, engine=None):
        if "多选题答案" in query or ("答案" in query and "金属梯" in query):
            return ("某题库 金属梯子不适于以下什么工作场所 多选题答案：答案 带电作业的工作场所、"
                    "坑穴或密闭场所、静电危险场所 解析：……")
        return "无关内容"


oa2 = FakeOA2(cfg, log)
OPTS = [{"text": "带电作业的工作场所", "letter": "A"}, {"text": "坑穴或密闭场所", "letter": "B"},
        {"text": "高空作业", "letter": "C"}, {"text": "静电危险场所", "letter": "D"}]
full, ev = oa2.search_multi_full(None, "金属梯子不适于以下什么工作场所", OPTS)
assert full == "ABD", (full, ev)
print(f"[4] 多选补全（A/B/D 全选，无漏选）OK｜依据: {ev}")


# ---------- 5) 变体生成不重复 ----------
oa3 = OnlineAnswer(cfg, log)
vs = oa3._judge_query_variants("要定期对压缩空气桶清理油污，以防止爆炸")
assert len(vs) == len(set(vs)) and 2 <= len(vs) <= 4
print(f"[5] 查询变体去重（{len(vs)} 个）OK")

print("\n[OK] 判断题必答 / 多选补全 测试全部通过")
