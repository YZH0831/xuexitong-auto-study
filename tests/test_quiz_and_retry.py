# -*- coding: utf-8 -*-
"""回归测试：① 判断题作答归一化 ② 失败知识点重试（不跳过） ③ 题库防污染。"""
import argparse
import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 本地用 config.yaml（含真实 Key），CI 里没有该文件 → 回退到示例配置
CFG_PATH = ROOT / "config.yaml"
if not CFG_PATH.exists():
    CFG_PATH = ROOT / "config.example.yaml"
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("test")

from cxlab.answers import QuestionBank  # noqa: E402
from cxlab.config import Config, load_config  # noqa: E402
from cxlab.course import Chapter  # noqa: E402
from cxlab.quiz import fill_answer, judge_index, Question  # noqa: E402
from cxlab.runner import Runner  # noqa: E402
from cxlab.report import Report  # noqa: E402

# ---------------- 1) 判断题归一化 ----------------
cases = {
    "对": 0, "正确": 0, "√": 0, "是": 0, "T": 0, "true": 0, "1": 0, "A": 0,
    "错": 1, "错误": 1, "×": 1, "否": 1, "F": 1, "false": 1, "0": 1, "B": 1,
    "不正确": 1, "不对": 1,          # 否定表达不能误判成"正确"
    "": None, "abcxyz": None,
}
for text, want in cases.items():
    got = judge_index(text)
    assert got == want, f"judge_index({text!r}) = {got}，期望 {want}"
print(f"[1] 判断题答案归一化 OK（{len(cases)} 条，含「不正确/不对」否定表达）")

# ---------------- 2) 判断题填充：选项是 A/B 字母时也能选中 ----------------
class FakeEl:
    def __init__(self, name):
        self.name = name
        self.clicked = False

    def is_visible(self):
        return True

    def click(self, **kw):
        self.clicked = True

    def query_selector(self, *a, **k):
        return None


def make_judge_q(option_texts):
    opts = [{"text": t, "el": FakeEl(t)} for t in option_texts]
    return Question("《安全生产法》规定：从业人员有权停止作业。（）", "judge", opts, FakeEl("container"))


for opts, ans, want_idx in [
    (["对", "错"], "对", 0),
    (["对", "错"], "错", 1),
    (["A", "B"], "对", 0),          # 选项文本只是字母标签 → 按顺序对应（对=第1个）
    (["A", "B"], "错误", 1),
    (["A", "B"], "T", 0),
    (["A", "B"], "F", 1),
]:
    q = make_judge_q(opts)
    ok = fill_answer(q, ans, log)
    assert ok, f"判断题未作答: 选项={opts} 答案={ans}"
    picked = [i for i, o in enumerate(q.options) if o["el"].clicked]
    assert picked == [want_idx], f"选项={opts} 答案={ans} → 点中 {picked}，期望 [{want_idx}]"
print("[2] 判断题作答 OK（对/错/√/T/F，选项为字母或文字均可）")

# ---------------- 2b) 多选题：不漏选 ----------------
from cxlab.quiz import _parse_letters, split_answer_parts  # noqa: E402

assert _parse_letters("ABD") == ["A", "B", "D"]
assert _parse_letters("A、B、D") == ["A", "B", "D"]
assert _parse_letters("A,B;D") == ["A", "B", "D"]
assert _parse_letters("A和B") == ["A", "B"]
assert _parse_letters("pH值") == [], "文本答案里的字母不能被当成选项字母"
# "Cooling" 语法上是字母串（由 fill_answer 的"选项范围护栏 + 字母必须对应真实选项"拦截，见用例 ③）
assert _parse_letters("Cooling") == ["C", "O", "O", "L", "I", "N", "G"]
assert split_answer_parts("甲、乙、丁") == ["甲", "乙", "丁"]
assert split_answer_parts("火灾性事故 爆炸性事故") == ["火灾性事故", "爆炸性事故"]


def make_multi_q(option_texts):
    opts = [{"text": t, "letter": chr(65 + i), "el": FakeEl(t)} for i, t in enumerate(option_texts)]
    return Question("高校实验室常见事故类型分为（）。", "multi", opts, FakeEl("c"))


# ① 字母答案：全部选中
q = make_multi_q(["火灾性事故", "爆炸性事故", "毒害性事故", "机械伤人事故"])
assert fill_answer(q, "ACD", log)
assert [i for i, o in enumerate(q.options) if o["el"].clicked] == [0, 2, 3], "多选题字母答案应选全"

# ② 文本答案（多片段）：不漏选
q = make_multi_q(["火灾性事故", "爆炸性事故", "毒害性事故", "机械伤人事故"])
assert fill_answer(q, "火灾性事故、毒害性事故、机械伤人事故", log)
assert [i for i, o in enumerate(q.options) if o["el"].clicked] == [0, 2, 3], \
    f"多片段文本答案漏选：{[o['el'].clicked for o in q.options]}"

# ③ 答案含英文词（Cooling）不应误点 C
q = make_multi_q(["冷却法", "窒息法", "化学灭火法", "隔离法"])
fn = fill_answer(q, "Cooling method", log)
assert not any(o["el"].clicked for o in q.options), "英文文本不应被当成字母选项"
print("[2b] 多选题选全 OK（字母型 / 多片段文本型 / 英文防误判）")

# ---------------- 3) 题库防污染 ----------------
tmp = Path(tempfile.mkdtemp())
bank = QuestionBank(str(ROOT / "data/answers.json"), str(tmp / "p.json"), log)
bank.record("某道题", "B", "single", source="online")
assert bank.lookup("某道题")["verified"] is False
bank.record("某道题", "A", "single", source="quiz_result")
assert bank.lookup("某道题")["answer"] == "A"
bank.record("某道题", "C", "single", source="online")
assert bank.lookup("某道题")["answer"] == "A", "未验证答案不得覆盖已验证答案"
print("[3] 题库防污染 OK（已验证优先，联网答案不覆盖）")

# ---------------- 4) 失败知识点重试（不直接跳过） ----------------
cfg = Config(load_config(str(CFG_PATH)))
cfg["safety"]["retry_rounds"] = 2
args = argparse.Namespace(select="", pick=False, list_courses=False, dry_run=False,
                          only="quiz", headless=True, course="", config="config.yaml")
runner = Runner(cfg, log, args)
runner.report = Report("某测试课", ROOT / "output", log)
runner.session = type("S", (), {"probe_session": lambda self, *a, **k: "ok", "page": None})()
runner.course = type("C", (), {"name": "某测试课", "course_id": "1", "clazzid": "2", "cpi": "3"})()
runner.humanizer = type("H", (), {"scroll": lambda self, p=None: None,
                                  "task_interval": lambda self, s=0: None})()
runner._refresh_platform_progress = lambda chapters, quiet=False: None
runner._try_local_repair = lambda ch, reason, note="": False

chapters = [Chapter("a", "第1节", None, 1), Chapter("b", "第2节", None, 1), Chapter("c", "第3节", None, 1)]
attempts = {"a": 0, "b": 0, "c": 0}


def fake_process(ch):
    """b 第一次失败、第二次成功；a 一直成功；c 一直失败（验证重试上限）。"""
    attempts[ch.id] += 1
    if ch.id == "c" or (ch.id == "b" and attempts["b"] == 1):
        runner.failed_chapters.append(ch)
        runner.failed_this_run.append(ch.title)
    else:
        runner.done_this_run.append(ch.title)
        runner.failed_chapters = [x for x in runner.failed_chapters if x.id != ch.id]
        runner.failed_this_run = [t for t in runner.failed_this_run if t != ch.title]


runner.process_chapter = fake_process
runner._run_rounds(chapters)

assert attempts["a"] == 1, f"a 应只处理一次，实际 {attempts['a']}"
assert attempts["b"] == 2, f"b 应被重试一次，实际 {attempts['b']}"
assert attempts["c"] == 3, f"c 应重试到上限（1+2 轮），实际 {attempts['c']}"
assert [c.id for c in runner.failed_chapters] == ["c"], "最终仍未完成的应只剩 c"
print(f"[4] 失败重试 OK（处理次数 a={attempts['a']} b={attempts['b']} c={attempts['c']}；"
      f"重试上限 {cfg.safety.retry_rounds} 轮，仍失败的不静默跳过，会在结尾明确列出）")

# ---------------- 5) 小圆门控：绿色才下一个；橙色 0 跳过；橙色 N 停止 ----------------
cfg_gate = Config(load_config(str(CFG_PATH)))
args_gate = argparse.Namespace(select="", pick=False, list_courses=False, dry_run=False,
                               only="", headless=True, course="", config="config.yaml")
runner_gate = Runner(cfg_gate, log, args_gate)
runner_gate.report = Report("某测试课", ROOT / "output", log)
runner_gate.session = type("S", (), {"probe_session": lambda self, *a, **k: "ok", "page": None})()
runner_gate.course = type("C", (), {"name": "某测试课", "course_id": "1", "clazzid": "2", "cpi": "3"})()
runner_gate.humanizer = type("H", (), {"scroll": lambda self, p=None: None,
                                       "task_interval": lambda self, s=0: None})()
runner_gate._refresh_platform_progress = lambda chapters, quiet=False: None
runner_gate._try_local_repair = lambda ch, reason, note="": False

gate_chapters = [
    Chapter("skip0", "橙色0", None, -1),
    Chapter("done", "绿色", None, 0),
    Chapter("pending", "橙色1", None, 1),
]
gate_attempts = {"skip0": 0, "done": 0, "pending": 0}
zero_calls = {"n": 0}


def gate_advance_zero(ch):
    zero_calls["n"] += 1
    ch.hint = -1
    return "done"


def gate_process(ch):
    gate_attempts[ch.id] += 1
    if ch.id == "pending":
        ch.hint = 1
        runner_gate.failed_chapters.append(ch)
        runner_gate.failed_this_run.append(ch.title)
        return "failed"
    return "done"


runner_gate.process_chapter = gate_process
runner_gate._advance_orange_zero = gate_advance_zero
runner_gate._run_rounds(gate_chapters)

assert gate_attempts == {"skip0": 0, "done": 0, "pending": 1}, gate_attempts
assert zero_calls == {"n": 1}, zero_calls
assert [c.id for c in runner_gate.failed_chapters] == ["pending"]
print("[5] 小圆门控 OK（橙色0点进去点下一节 / 绿色通过 / 橙色1停止，不进入下一个）")

# ---------------- 5b) 占位章节例外：本次跳过，不因橙色1卡住 ----------------
cfg_ph = Config(load_config(str(CFG_PATH)))
args_ph = argparse.Namespace(select="", pick=False, list_courses=False, dry_run=False,
                             only="", headless=True, course="", config="config.yaml")
runner_ph = Runner(cfg_ph, log, args_ph)
runner_ph.report = Report("某测试课", ROOT / "output", log)
runner_ph.session = type("S", (), {"probe_session": lambda self, *a, **k: "ok", "page": None})()
runner_ph.course = type("C", (), {"name": "某测试课", "course_id": "1", "clazzid": "2", "cpi": "3"})()
runner_ph.humanizer = type("H", (), {"scroll": lambda self, p=None: None,
                                     "task_interval": lambda self, s=0: None})()
runner_ph._refresh_platform_progress = lambda chapters, quiet=False: None
runner_ph._try_local_repair = lambda ch, reason, note="": False
ph_chapters = [Chapter("ph", "敬请期待", None, 1)]
ph_calls = {"n": 0}


def ph_process(ch):
    ph_calls["n"] += 1
    ch.hint = 1
    runner_ph._placeholder_skip = True
    return "skipped"


runner_ph.process_chapter = ph_process
runner_ph._run_rounds(ph_chapters)
assert ph_calls == {"n": 1}, ph_calls
assert runner_ph.failed_chapters == [], runner_ph.failed_chapters
print("[5b] 占位章节例外 OK（本次跳过，不卡门控；下次运行仍重新探测）")

# ---------------- 5c) 平台拦截提交：本 run 不再自动重提 ----------------
cfg_block = Config(load_config(str(CFG_PATH)))
args_block = argparse.Namespace(select="", pick=False, list_courses=False, dry_run=False,
                                only="", headless=True, course="", config="config.yaml")
runner_block = Runner(cfg_block, log, args_block)
runner_block.report = Report("某测试课", ROOT / "output", log)
runner_block.session = type("S", (), {"probe_session": lambda self, *a, **k: "ok", "page": None})()
runner_block.course = type("C", (), {"name": "某测试课", "course_id": "1", "clazzid": "2", "cpi": "3"})()
runner_block.humanizer = type("H", (), {"scroll": lambda self, p=None: None,
                                        "task_interval": lambda self, s=0: None})()
runner_block._refresh_platform_progress = lambda chapters, quiet=False: None
runner_block._try_local_repair = lambda ch, reason, note="": False
block_ch = Chapter("blocked", "被拦截题目", None, 1)
block_calls = {"n": 0}


def blocked_process(ch):
    block_calls["n"] += 1
    ch.hint = 1
    runner_block._submit_blocked_ids.add(str(ch.id))
    runner_block.failed_chapters.append(ch)
    runner_block.failed_this_run.append(ch.title)
    return "manual"


runner_block.process_chapter = blocked_process
runner_block._run_rounds([block_ch])
assert block_calls == {"n": 1}, f"提交被拦截后不应自动重试，实际处理 {block_calls['n']} 次"
assert runner_block.failed_chapters and runner_block.failed_chapters[0].id == "blocked"
print("[5c] 防重复提交 OK（平台拦截后本 run 不再自动重提，结尾仍明确列出待人工）")

print("\n[OK] 判断题 / 重试 / 题库防污染 测试全部通过")
