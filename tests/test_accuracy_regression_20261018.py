# -*- coding: utf-8 -*-
"""2026-10-18 修复回归：用生产日志里真实出现的失败形态做离线验证。只读项目文件。"""
import logging
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)

from cxlab.config import Config, load_config               # noqa: E402
from cxlab.online_answer import (                          # noqa: E402
    OnlineAnswer, anchor_score, same_answer, resolve_letters,
)

logging.basicConfig(level=logging.CRITICAL)
log = logging.getLogger("regress")
CFG_PATH = Path(ROOT) / "config.yaml"
if not CFG_PATH.exists():
    CFG_PATH = Path(ROOT) / "config.example.yaml"   # CI 里没有个人配置
cfg = Config(load_config(str(CFG_PATH)))
cfg["quiz"]["online_search"]["min_interval_s"] = 0
cfg["quiz"]["online_search"]["skip_when_obfuscated"] = False


def mk(t1, t2):
    oa = OnlineAnswer(cfg, log)
    oa._fetch_text = lambda ctx, q, engine=None: (t2 if q.startswith("作业帮") else t1)
    return oa


def case(title, got, expect):
    ok = "OK " if got == expect else "FAIL"
    print(f"[{ok}] {title}: got={got!r} expect={expect!r}")
    return got == expect


results = []

# 1. 2026-10-18 21:19 真实错答：单源 TR 被 0.78 置信的解析推理改成 FA
JOPTS = [{"text": "对", "letter": "TR"}, {"text": "错", "letter": "FA"}]
JSTEM = "公民履行兵役义务的主要形式有三种:服现役,服预备役,参加民兵组织。"
t1 = (f"某题库 {JSTEM} A 对 B 错 答案：对 解析：该说法不完全正确，"
      f"兵役义务还包括其他形式，因此表述不正确。")
r = mk(t1, "").search_cross(None, JSTEM, JOPTS, "judge")
results.append(case("判断题字面答案不被启发式推理推翻", r["answer"], "TR"))

# 2. 判断题目："通用 A/B" 与 "作业帮 TR/FA" 应判为双源一致（旧实现 False）
t1 = f"百度教育 {JSTEM} A 对 B 错 答案：A 解析：符合兵役法规定。"
t2 = f"作业帮 {JSTEM} A 对 B 错 答案 正确 AI解析 表述正确。"
r = mk(t1, t2).search_cross(None, JSTEM, JOPTS, "judge")
results.append(case("判断题 A/TR 字母空间统一", (r["agree"], r["answer"]), (True, "TR")))

# 3. 多选分隔符答案（旧实现只取 A）："答案：A、B、D"
OPTS4 = [{"text": "甲选项", "letter": "A"}, {"text": "乙选项", "letter": "B"},
         {"text": "丙选项", "letter": "C"}, {"text": "丁选项", "letter": "D"}]
STEM = "金属梯子不适于以下什么工作场所"
page = f"某题库 {STEM} A 甲选项 B 乙选项 C 丙选项 D 丁选项 答案：A、B、D 解析"
oa = mk("", "")
results.append(case("多选 'A、B、D' 不再截断", oa._extract(page, OPTS4, "multi", STEM), "ABD"))

# 4. 多选文本答案（旧实现只取第一个选项 → "A"）
MOPTS = [{"text": "一般化学品", "letter": "A"}, {"text": "易制毒化学品", "letter": "B"},
         {"text": "易燃易爆化学品", "letter": "C"}, {"text": "剧毒化学品", "letter": "D"}]
MSTEM = "化学品分为以下几类？"
mpage = f"某题库 {MSTEM} 答案：一般化学品、易制毒化学品、剧毒化学品 解析：略"
results.append(case("多选文本答案全部命中", oa._extract(mpage, MOPTS, "multi", MSTEM), "ABD"))

# 5. 相似选项的字母对齐（旧实现把源 A 错映射成我们的 A）
SOPTS = [{"text": "高压电", "letter": "A"}, {"text": "高压电缆", "letter": "B"},
         {"text": "低压电", "letter": "C"}]
spage = f"某库 {STEM} A 高压电缆 B 高压电 C 低压电 答案：A"
results.append(case("相似选项按精确文本对齐", oa._extract(spage, SOPTS, "single", STEM), "B"))

# 6. AI 块跨题：上一题答案在前，本题答案在后
ai = (f"AI解题 上一题：ABCD 全选? A 甲选项 B 乙选项 C 丙选项 答案：B "
      f"本题：{STEM} A 甲选项 B 乙选项 C 丙选项 D 丁选项 答案：C")
results.append(case("AI 块取题干之后的答案", oa._extract(ai, OPTS4, "single", STEM), "C"))

# 7. 全角冒号："答案：正确"（旧 AI 正则漏掉全角冒号）
jpage = f"AI解析 {JSTEM} 判断结果：正确"
results.append(case("全角冒号的判断题解析", oa._extract(jpage, JOPTS, "judge", JSTEM), "TR"))

# 8. 全/半角括号不再导致整页答案被丢弃
BSTEM = "火灾蔓延的途径是（）。"
BOPTS = [{"text": "热传导", "letter": "A"}, {"text": "热对流", "letter": "B"},
         {"text": "热辐射", "letter": "C"}, {"text": "以上都是", "letter": "D"}]
bt1 = f"某站 {BSTEM} A 热传导 B 热对流 C 热辐射 D 以上都是 正确答案：C"
bt2 = f"作业帮 火灾蔓延的途径是() A 热传导 B 热对流 C 热辐射 D 以上都是 答案：D"
r = mk(bt1, bt2).search_cross(None, BSTEM, BOPTS, "single")
results.append(case("半角括号源也能抽取", (r["a1"], r["a2"]), ("C", "D")))

# 9. anchor_score 不再被页首 "AI" 锚定（答案 A 应有区分度）
apage = f"AI摘要 " + "无关内容" * 30 + f" {STEM} A 甲选项 B 乙选项 C 丙选项 D 丁选项 答案：A"
results.append(case("anchor_score(pos) 对 A 不再系统性为 0",
                    anchor_score(apage, OPTS4, "A", apage.find("答案") + 3), 4))

# 10. 题库：JSON 里的 source 不再丢失；判断题脏答案 "AC" 自动失效
import json  # noqa: E402
from cxlab.answers import QuestionBank  # noqa: E402

POOL_FILE = Path(ROOT) / "data" / "question_pool.json"
if POOL_FILE.exists():
    bank = QuestionBank(str(Path(ROOT) / "data" / "answers.json"), str(POOL_FILE), log)
    pool = json.loads(POOL_FILE.read_text(encoding="utf-8"))
    if len(pool) > 3:
        bad = pool[3]
        results.append(case("判断题脏答案 AC 不再被当成本地答案",
                            bank.lookup(bad["stem"]), None))
    leaked = [i[:20] for i in pool
              if i.get("source") == "online" and (bank.lookup(i["stem"]) or {}).get("verified")]
    results.append(case("pool 的 online 来源不再被当成已验证", leaked, []))
else:
    # data/question_pool.json 是个人题库，不进仓库（.gitignore），CI 里没有它
    print("[SKIP] 没有 data/question_pool.json（个人题库不入库），跳过题库沉淀相关断言")

print()
print("PASS" if all(results) else "FAIL", f"{sum(results)}/{len(results)}")
sys.exit(0 if all(results) else 1)
