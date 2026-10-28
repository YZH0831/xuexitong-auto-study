# -*- coding: utf-8 -*-
"""大模型答题自检：用 3 道真实样例题验证配置是否可用（不碰学习通）。

用法：
    .venv\\Scripts\\python.exe tools/test_llm.py
"""
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cxlab.config import Config, load_config  # noqa: E402
from cxlab.llm_answer import LLMAnswer  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("test-llm")

CASES = [
    ("single", "金属梯子不适于以下什么工作场所？",
     [{"text": "带电作业的工作场所", "letter": "A"}, {"text": "坑穴或密闭场所", "letter": "B"},
      {"text": "高空作业", "letter": "C"}, {"text": "静电", "letter": "D"}], "A"),
    ("multi", "内照射防护的一般措施有（）。",
     [{"text": "包容", "letter": "A"}, {"text": "隔离", "letter": "B"},
      {"text": "净化", "letter": "C"}, {"text": "稀释", "letter": "D"}], "ABCD"),
    ("judge", "要定期对压缩空气桶清理油污，以防止油类和空气形成爆炸性气体。",
     [{"text": "对", "letter": "A"}, {"text": "错", "letter": "B"}], "A"),
]


def main():
    cfg = Config(load_config(str(ROOT / "config.yaml")))
    solver = LLMAnswer(cfg, log)
    ok, why = solver.available()
    print(f"\n配置：enabled={solver.enabled} model={solver.model} base_url={solver.base_url}")
    print(f"状态：{'✅ 可用' if ok else '❌ 不可用 — ' + why}\n")
    if not ok:
        print("修好后重跑本脚本即可。常见配置：")
        print("  Kimi K3 ：base_url: https://api.moonshot.cn/v1 / model: kimi-k3 / api_key: sk-... "
              "(reasoning_effort: low)")
        print("  Kimi 低价：model 换成 kimi-k2.6（同端点，便宜很多）")
        print("  DeepSeek：base_url: https://api.deepseek.com/v1 / model: deepseek-flash / api_key: sk-...")
        print("  Ollama  ：base_url: http://localhost:11434/v1 / model: qwen2.5:7b / api_key: ollama")
        return 1
    passed = 0
    for qtype, stem, opts, expect in CASES:
        t0 = time.time()
        ans, conf, reason = solver.solve(stem, opts, qtype)
        dt = time.time() - t0
        good = (ans == expect) or (qtype != "multi" and ans and expect.startswith(ans or "x"))
        passed += 1 if good else 0
        print(f"[{qtype}] {stem[:26]}…")
        print(f"   答案={ans} 置信={conf:.2f} 耗时={dt:.1f}s 期望={expect} "
              f"{'✔' if good else '✘'}  {reason}")
    print(f"\n结果：{passed}/{len(CASES)} 与参考答案一致；本次调用 {solver.calls} 次")
    if passed < len(CASES):
        print("提示：判断题/多选题若答错，多半是模型太小 —— 7B 以下建议换 14B+ 或直接用 DeepSeek。")
    return 0 if passed == len(CASES) else 2


if __name__ == "__main__":
    sys.exit(main())
