# -*- coding: utf-8 -*-
"""第三方题库接口测试（不联网，全部用假 HTTP）。"""
import json
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

from cxlab.config import Config, load_config        # noqa: E402
from cxlab.tiku import Tiku, _clean_answer          # noqa: E402
from cxlab.quiz import _tiku_text_to_letters        # noqa: E402

OPTS = [{"text": "公平责任", "letter": "A"},
        {"text": "特殊侵权责任", "letter": "B"},
        {"text": "过错推定责任", "letter": "C"},
        {"text": "连带责任", "letter": "D"}]


def cfg_with(providers, enabled=True):
    cfg = Config(load_config(str(CFG_PATH)))
    cfg["quiz"]["tiku"] = {"enabled": enabled, "timeout_s": 3, "max_calls": 10,
                           "min_stem_len": 4, "providers": providers}
    return cfg


# ---------- 1) 答案归一化 ----------
assert _clean_answer("答案：A") == ("A", "letter")
assert _clean_answer("bc") == ("BC", "letter")
assert _clean_answer("正确答案是 特殊侵权责任") == ("特殊侵权责任", "text")
assert _clean_answer("") == (None, "")
print("[1] 答案归一化（字母/文本/带前缀） OK")


# ---------- 2) icodef：返回字母 ----------
class FakeIcodef(Tiku):
    def _http(self, url, **kw):
        assert "icodef" in url, url
        return json.dumps({"code": 1, "data": {"answer": "C"}})


t = FakeIcodef(cfg_with([{"type": "icodef", "token": "tk"}]), log)
ans, kind, prov, _ = t.query("违反安全保障义务责任属于（）", OPTS, "single")
assert (ans, kind, prov) == ("C", "letter", "icodef"), (ans, kind, prov)
print("[2] icodef 命中并返回字母 OK")


# ---------- 3) enncy：返回文本 → 对齐成本页选项字母 ----------
class FakeEnncy(Tiku):
    def _http(self, url, **kw):
        return json.dumps({"code": 0, "data": {"answer": "过错推定责任"}})


t = FakeEnncy(cfg_with([{"type": "enncy", "token": "tk"}]), log)
ans, kind, prov, _ = t.query("违反安全保障义务责任属于（）", OPTS, "single")
assert kind == "text" and prov == "enncy"
assert _tiku_text_to_letters(ans, OPTS, "single") == "C"
print("[3] enncy 文本答案 → 选项对齐 OK")


# ---------- 4) tikuAdapter：answerKeyText 多选 ----------
class FakeAdapter(Tiku):
    def _http(self, url, **kw):
        assert url.endswith("/adapter-service/search"), url
        return json.dumps({"answer": {"answerKeyText": "BD",
                                      "answerText": "特殊侵权责任#连带责任"}})


t = FakeAdapter(cfg_with([{"type": "tikuadapter", "url": "http://127.0.0.1:8060"}]), log)
ans, kind, prov, _ = t.query("下列哪些属于侵权责任类型（）", OPTS, "multi")
assert (ans, kind, prov) == ("BD", "letter", "tikuadapter"), (ans, kind, prov)
print("[4] tikuAdapter 多选答案 OK")


# ---------- 5) 多选文本片段拆分对齐 ----------
m = _tiku_text_to_letters("公平责任#连带责任", OPTS, "multi")
assert m == "AD", m
m2 = _tiku_text_to_letters("特殊侵权责任、过错推定责任", OPTS, "multi")
assert m2 == "BC", m2
print("[5] 多选文本片段 → 字母 OK")


# ---------- 6) provider 失败熔断 ----------
class Boom(Tiku):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.n = 0

    def _http(self, url, **kw):
        self.n += 1
        raise TimeoutError("timeout")


t = Boom(cfg_with([{"type": "icodef", "token": "tk"}]), log)
for _ in range(5):
    t.query("违反安全保障义务责任属于（）", OPTS, "single")
assert t.n == 3, f"连续失败 3 次后应熔断，实际请求 {t.n} 次"
print("[6] 连续失败 3 次 → 熔断 OK")


# ---------- 7) 多 provider 回退 + 未启用 ----------
class FirstDead(Tiku):
    def _http(self, url, **kw):
        if "icodef" in url:
            raise TimeoutError("dead")
        return json.dumps({"code": 0, "data": {"answer": "连带责任"}})


t = FirstDead(cfg_with([{"type": "icodef", "token": "a"}, {"type": "enncy", "token": "b"}]), log)
ans, kind, prov, _ = t.query("违反安全保障义务责任属于（）", OPTS, "single")
assert prov == "enncy" and ans == "连带责任", (ans, prov)
off = Tiku(cfg_with([{"type": "icodef"}], enabled=False), log)
assert off.query("任意题干内容足够长", OPTS)[0] is None
print("[7] provider 回退 + 未启用跳过 OK")

print("\n全部通过。")
