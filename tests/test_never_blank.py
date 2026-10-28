# -*- coding: utf-8 -*-
"""禁止留空提交 测试。

背景：6.4 本章自测 10 道题，联网只搜到 6 道，剩下 4 道留空就点提交，
结果平台用"还有X道题未作答"拦住，重试 3 次后判 failed。

本测试验证：
1. reasoning.best_guess 对单选/多选/判断都能给出答案（填空题不受支持，返回 None）；
2. run_quiz 在"题库空 + 无联网 + 未开大模型"的极端情况下，10 道题依然全部作答，
   待人工 = 0；
3. 平台拦截提示能被读出来。
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
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("t")

from cxlab import reasoning as R          # noqa: E402
import cxlab.quiz as Q                    # noqa: E402
from cxlab.config import Config, load_config  # noqa: E402

# ---------- 1) best_guess 覆盖各类题型 ----------
sing = [{"text": "为人民服务", "letter": "A"},
        {"text": "为个人利益", "letter": "B"},
        {"text": "以上都不对", "letter": "C"}]
a, c, why = R.best_guess("中国共产党的根本宗旨是什么", sing, "single")
assert a == "A" and 0.2 <= c <= 0.5, (a, c, why)

multi = [{"text": "爱岗敬业", "letter": "A"}, {"text": "诚实守信", "letter": "B"},
         {"text": "唯利是图", "letter": "C"}, {"text": "办事公道", "letter": "D"}]
m, mc, mwhy = R.best_guess("职业道德的基本要求包括", multi, "multi")
assert m and 2 <= len(m) <= 3, (m, mwhy)

judge = [{"text": "对", "letter": "TR"}, {"text": "错", "letter": "FA"}]
j, jc, _ = R.best_guess("所有同学都必须参加这次活动", judge, "judge")
assert j == "FA", j

assert R.best_guess("填空", [], "text") == (None, 0.0, "")
print(f"[1] best_guess：单选→{a} / 多选→{m} / 判断→{j} / 填空→不支持  OK")


# ---------- 2) run_quiz 极端场景：全部作答，不留空 ----------
class El:
    def __init__(self, text, letter=None):
        self.text, self.letter, self.clicked = text, letter, 0

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def click(self, **k):
        self.clicked += 1

    def query_selector(self, *a, **k):
        return None

    def query_selector_all(self, *a, **k):
        return []

    def inner_text(self, *a, **k):
        return self.text

    def get_attribute(self, name, *a, **k):
        return self.letter if name in ("data", "value") else None

    def evaluate(self, *a, **k):
        return self.text

    def scroll_into_view_if_needed(self, *a, **k):
        return None

    def bounding_box(self):
        return {"x": 0, "y": 0, "width": 20, "height": 20}


def mk_q(stem, qtype, texts, letters=None):
    letters = letters or [chr(65 + i) for i in range(len(texts))]
    opts = [{"text": t, "letter": l, "el": El(t, l)} for t, l in zip(texts, letters)]
    return Q.Question(stem, qtype, opts, El(stem))


QS = [
    mk_q("社会主义核心价值观在国家层面的要求是", "single",
         ["富强、民主、文明、和谐", "自由、平等、公正、法治", "爱国、敬业、诚信、友善", "以上都不对"]),
    mk_q("中国共产党的根本宗旨是", "single", ["为人民服务", "为个人利益", "为少数人服务", "无关"]),
    mk_q("道德的本质是", "single",
         ["一种社会意识形态", "天生的本能", "所有情况都一样的规矩", "无法确定"]),
    mk_q("我国公民基本道德规范包括", "single",
         ["爱国守法", "违法犯罪", "所有行为都合法", "什么都不用做"]),
    mk_q("社会主义道德建设的核心是", "single", ["为人民服务", "个人利益", "金钱", "权力"]),
    mk_q("职业生活中的基本道德要求是", "single",
         ["爱岗敬业、诚实守信、办事公道、服务群众、奉献社会", "赚钱", "听领导的话", "不出错"]),
    mk_q("下列属于职业道德基本规范的有", "multi",
         ["爱岗敬业", "诚实守信", "唯利是图", "办事公道"]),
    mk_q("社会主义道德建设要坚持的原则有", "multi",
         ["集体主义", "为人民服务", "损人利己", "公平正义"]),
    mk_q("加强道德修养的途径包括", "multi",
         ["学习", "实践", "内省", "完全不反思"]),
    mk_q("所有道德规范在任何时代都完全相同", "judge", ["对", "错"], ["TR", "FA"]),
]

# 屏蔽外部依赖
Q.extract_questions = lambda page, log: QS
Q.decode_font_texts = lambda *a, **k: None
Q.submit_quiz = lambda page, log, humanizer=None: True
Q.read_result = lambda *a, **k: 60.0


class FakeBank:
    def lookup(self, stem):
        return None

    def record(self, *a, **k):
        pass


class FakePopup:
    def tick(self, page=None):
        pass


class FakePage:
    def wait_for_timeout(self, ms):
        return None

    def query_selector(self, *a, **k):
        return None

    def query_selector_all(self, *a, **k):
        return []

    def evaluate(self, *a, **k):
        return ""


cfg = Config(load_config(str(CFG_PATH)))
cfg["quiz"]["llm"]["enabled"] = False           # 不联网、不调大模型
cfg["quiz"]["online_search"]["enabled"] = False
cfg["quiz"]["on_unknown"] = "submit_blank"

status, note = Q.run_quiz(None, cfg, log, FakePage(), FakePopup(), FakeBank(), online=None)
assert status == "done", (status, note)
filled = sum(1 for q in QS for o in q.options if o["el"].clicked)
blank = [q.stem for q in QS if not any(o["el"].clicked for o in q.options)]
assert not blank, f"仍有留空题目：{blank}"
print(f"[2] 10 题全部作答（点击 {filled} 次），无留空  OK  ← {note[:40]}")


# ---------- 3) 平台"还有X道题未作答"提示能被读到 ----------
class HintPage(FakePage):
    def evaluate(self, *a, **k):
        return "提交 温馨提示 还有 3 道题未作答，请答完再提交"


assert "未作答" in Q._unanswered_hint(HintPage())
print("[3] 拦截提示解析  OK →", Q._unanswered_hint(HintPage()))

print("\n全部通过。")
