# -*- coding: utf-8 -*-
"""大模型答题模块测试：JSON 解析 / 题型校验 / 判断题映射 / 多选漏选 / 未配置降级。"""
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
from cxlab.llm_answer import LLMAnswer  # noqa: E402


def make(api_key="sk-test", enabled=True, **kw):
    cfg = Config(load_config(str(CFG_PATH)))
    llm = cfg["quiz"]["llm"]
    llm.update(enabled=enabled, api_key=api_key, **kw)
    return LLMAnswer(cfg, log)


OPTS = [{"text": "带电作业的工作场所", "letter": "A"}, {"text": "坑穴或密闭场所", "letter": "B"},
        {"text": "高空作业", "letter": "C"}, {"text": "静电", "letter": "D"}]
JOPTS = [{"text": "对", "letter": "A"}, {"text": "错", "letter": "B"}]

# 1) 未启用 / 无 Key → 明确降级
oa = make(enabled=False)
ok, why = oa.available()
assert ok is False and "未启用" in why
oa = make(api_key="")
ok, why = oa.available()
assert ok is False and "api_key" in why
print("[1] 未启用/无 Key → 正确降级 OK")

# 2) 标准 JSON
oa = make(); oa._chat = lambda p: '{"answer": "A", "confidence": 0.9, "reason": "导电"}'
ans, conf, reason = oa.solve("金属梯子不适于以下什么场所？", OPTS, "single")
assert ans == "A" and conf == 0.9 and "导电" in reason, (ans, conf, reason)
print("[2] 标准 JSON 解析 OK")

# 3) 带 Markdown 代码块 / 多余文字
oa = make(); oa._chat = lambda p: '好的，答案如下：\n```json\n{"answer":"c","confidence":0.8}\n```\n'
ans, _, _ = oa.solve("x", OPTS, "single")
assert ans == "C", ans
print("[3] 代码块/夹杂文字 OK")

# 4) 多选题：多字母去重排序
oa = make(); oa._chat = lambda p: '{"answer":"b,a,c","confidence":0.85}'
ans, _, _ = oa.solve("多选", OPTS, "multi")
assert ans == "ABC", ans
print("[4] 多选题字母归一化 OK")

# 5) 多选题只给 1 项 → 降置信
oa = make(); oa._chat = lambda p: '{"answer":"A","confidence":0.9}'
ans, conf, reason = oa.solve("多选", OPTS, "multi")
assert ans == "A" and conf <= 0.4 and "漏选" in reason, (ans, conf, reason)
print("[5] 多选题少选 → 降置信并提示 OK")

# 6) 判断题：对/错 → 选项字母
oa = make(); oa._chat = lambda p: '{"answer":"对","confidence":0.95}'
ans, _, _ = oa.solve("压缩空气桶要定期清理油污。", JOPTS, "judge")
assert ans == "A", ans
oa = make(); oa._chat = lambda p: '{"answer":"错误","confidence":0.9}'
ans, _, _ = oa.solve("可以在通电电线上晾衣服。", JOPTS, "judge")
assert ans == "B", ans
print("[6] 判断题映射（对→A / 错误→B）OK")

# 7) 字母超出选项范围 → 拒绝
oa = make(); oa._chat = lambda p: '{"answer":"F","confidence":0.9}'
ans, _, reason = oa.solve("x", OPTS, "single")
assert ans is None and "超出" in reason, (ans, reason)
print("[7] 越界字母被拒绝 OK")

# 8) 返回非 JSON 两次 → 放弃
oa = make(); oa._chat = lambda p: "我觉得答案是 A"
ans, _, why = oa.solve("x", OPTS, "single")
assert ans is None
print("[8] 非 JSON 输出 → 放弃（不瞎猜）OK")

# 9) 调用上限（替身会绕过 _chat 的计数，这里直接模拟已调用次数）
oa = make(max_calls=1)
oa.calls = 1
ok, why = oa.available()
assert ok is False and "上限" in why, (ok, why)
print("[9] 调用上限生效 OK")

# 10) 真实 _chat 会计数（不打网络，只验证计数与报错路径）
oa = make(base_url="http://127.0.0.1:9/v1", timeout_s=3)
ans, conf, why = oa.solve("x", OPTS, "single")
assert ans is None and oa.calls >= 1, (ans, oa.calls, why)
print(f"[10] 网络失败时计数且不误答 OK（calls={oa.calls}）")

print("\n[OK] 大模型答题模块测试全部通过")
