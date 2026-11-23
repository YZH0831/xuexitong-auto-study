# -*- coding: utf-8 -*-
"""2026-11-20 准确率提升回归：① 单选多字母降级 ② 大模型复核仲裁 ③ 配置默认。

全部离线：替身 _chat / 替身 online，不联网、不碰账号。
"""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.CRITICAL)
log = logging.getLogger("acc11120")

from cxlab.config import Config, load_config  # noqa: E402
from cxlab.llm_answer import LLMAnswer  # noqa: E402
from cxlab.online_answer import same_answer  # noqa: E402

CFG_PATH = ROOT / ("config.yaml" if (ROOT / "config.yaml").exists() else "config.example.yaml")
results = []


def case(title, got, expect):
    ok = "OK " if got == expect else "FAIL"
    print(f"[{ok}] {title}: got={got!r} expect={expect!r}")
    results.append(got == expect)
    return got == expect


def make_llm(raw):
    cfg = Config(load_config(str(CFG_PATH)))
    llm = cfg["quiz"]["llm"]
    llm.update(enabled=True, api_key="sk-test")
    solver = LLMAnswer(cfg, log)
    solver._chat = lambda p: raw
    return solver


OPTS = [{"text": "带电作业的工作场所", "letter": "A"},
        {"text": "坑穴或密闭场所", "letter": "B"},
        {"text": "高空作业", "letter": "C"},
        {"text": "静电", "letter": "D"}]

# 1) 单选题模型若吐多个字母 → 降置信到 0.3，避免 fill_answer 连点多个必错
s = make_llm('{"answer":"AB","confidence":0.95,"reason":"test"}')
a, c, r = s.solve("单选", OPTS, "single")
case("单选多字母降置信", (a, c), ("A", 0.3))

# 2) 多选题多字母不受影响，仍全量返回
s = make_llm('{"answer":"A,B,C","confidence":0.9}')
a, c, r = s.solve("多选", OPTS, "multi")
case("多选多字母不变", a, "ABC")

# 3) 单选单字母仍保持模型原置信（不误伤）
s = make_llm('{"answer":"B","confidence":0.88}')
a, c, r = s.solve("单选", OPTS, "single")
case("单选单字母置信不变", (a, c), ("B", 0.88))

# 4) 复核用的 same_answer 能识别"文本 vs 字母"等价（旧实现粗暴截断会误判冲突）
case("复核等价判断（文本=字母）", same_answer("坑穴或密闭场所", "B", OPTS), True)
case("复核等价判断（字母序无关）", same_answer("BD", "DB", OPTS), True)
case("复核等价判断（真冲突）", same_answer("A", "C", OPTS), False)

# 5) 配置默认含 verify_below 字段，且示例/本机配置值合法
cfg = Config(load_config(str(CFG_PATH)))
case("verify_below 已配置", isinstance(cfg.quiz.llm.get("verify_below"), (int, float)), True)
case("min_confidence 合法区间", 0 < cfg.quiz.llm.get("min_confidence", 0) < 1, True)

print()
print("PASS" if all(results) else "FAIL", f"{sum(results)}/{len(results)}")
sys.exit(0 if all(results) else 1)
