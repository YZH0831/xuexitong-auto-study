# -*- coding: utf-8 -*-
"""对不依赖 playwright 的模块做逻辑自测：题库匹配 + 报告输出。"""
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("test")

from cxlab.answers import QuestionBank  # noqa: E402
from cxlab.report import Report  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp())

# ---- 题库 ----
bank = QuestionBank(str(ROOT / "data/answers.json"), str(tmp / "pool.json"), log)
print("加载题条目数:", bank.size)
assert bank.size >= 1, "题库未加载"

# 精确匹配
hit = bank.lookup("示例：进入实验室必须穿戴的实验服是")
print("精确匹配:", hit)
assert hit["answer"] == "A", hit
assert hit["verified"] is True, "结果页回读的答案应标记为已验证"

# 模糊匹配（题干带空格/标点）
hit2 = bank.lookup("示例：进入实验室 必须穿戴的 实验服是？")
print("模糊匹配:", hit2)
assert hit2["answer"] == "A", hit2

# 未命中
assert bank.lookup("完全不相关的题目xyz") is None

# 沉淀与持久化
bank.record("实验室发生火灾应首先", "切断电源", "single")
bank.save_pool()
assert (tmp / "pool.json").exists()
bank2 = QuestionBank(str(ROOT / "data/answers.json"), str(tmp / "pool.json"), log)
print("沉淀后可查:", bank2.lookup("实验室发生火灾应首先"))
assert bank2.lookup("实验室发生火灾应首先")["answer"] == "切断电源"

# 防污染：联网(未验证)答案不能覆盖已验证答案；已验证可以覆盖未验证
bank3 = QuestionBank(str(ROOT / "data/answers.json"), str(tmp / "pool3.json"), log)
bank3.record("某题", "B", "single", source="online")
assert bank3.lookup("某题")["verified"] is False
bank3.record("某题", "A", "single", source="quiz_result")
assert bank3.lookup("某题")["answer"] == "A", "已验证答案应覆盖未验证答案"
bank3.record("某题", "C", "single", source="online")
assert bank3.lookup("某题")["answer"] == "A", "未验证答案不得覆盖已验证答案"
print("题库防污染（已验证优先）OK")

# ---- 报告 ----
rep = Report(str(tmp / "out"), "实验室安全", log)
rep.add("1.1 概述", "[视频] 安全重要性", "done", "播放完成 rate=2.0x")
rep.add("1.1 概述", "[文档] 安全守则", "done", "翻页 18 次 / 共 18 页")
rep.add("1.2 化学品", "[章节测验] 第一章测验", "done", "已提交，得分 90")
rep.add("1.2 化学品", "[视频] 危化品管理", "failed", "播放超时")
rep.add("作业", "[作业] 心得体会", "manual", "已打开作业页")
md, js = rep.write()
print("报告:", md)
print(md.read_text(encoding="utf-8")[:600])
assert "已完成" in md.read_text(encoding="utf-8")
assert rep.counts()["done"] == 3

print("\n[OK] 核心模块自测通过")
