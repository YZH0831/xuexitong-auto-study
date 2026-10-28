# -*- coding: utf-8 -*-
"""题库：本地 JSON 题库 + 模糊匹配 + 结果页自动沉淀。

可信度分级（RANK）——只允许高等级覆盖低等级：
  5 quiz_result    平台结果页回读（最可信）
  4 llm_verified   大模型 + 搜索一致
  4 tiku           第三方题库接口命中（结构化答案，可信）
  3 cross_checked  双源一致
  2 llm            大模型单独作答
  1 online_conflict 冲突后按规则采信（存疑）
  0 online         单源未验证

重要修复（2026-10-18）：
- 旧实现 _load→_add 会丢掉 JSON 里的 source 字段，lookup 又默认 quiz_result，
  导致所有历史条目（含联网单源答案）重启后都被当成"已验证"，错答案被永久锁死；
- record() 的 old_rank 旧实现默认 5，错答案写入后任何新证据都覆盖不了；
- save_pool() 只 append 不去重，文件里旧错答案先写入 → 重启后复活。
以上三点是"本地题库优先"反而拉低正确率的根因，本次一并修掉。
"""
import difflib
import json
import re
import time
from pathlib import Path


_PUNCT = "，。？！、；：“”‘’（）()[]【】.,?!/-—_·\\"
RANK = {"quiz_result": 5, "llm_verified": 4, "tiku": 4, "cross_checked": 3,
        "llm": 2, "online_conflict": 1, "online": 0}
TRUSTED = ("quiz_result", "llm_verified", "tiku", "cross_checked")
_NEG_WORDS = ("不", "无", "未", "禁止", "不得", "不能", "错误", "非")


def _norm(text):
    """归一化：统一小写、去空白与标点，便于题库匹配。"""
    if not text:
        return ""
    s = str(text).lower()
    s = re.sub(r"[\s\u2002\u200b\u3000\u00a0]+", "", s)
    s = re.sub(f"[{re.escape(_PUNCT)}]", "", s)
    return s


def _compatible(a, b):
    """两个题干是否真的同一道题（避免"有权/无权""第五条/第六条"这类 0.9+ 相似题错配）。

    要求：长度比 ≥0.9 + 数字集合完全一致 + 否定词出现情况一致。
    """
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) / max(len(a), len(b)) < 0.9:
        return False
    if sorted(re.findall(r"\d+", a)) != sorted(re.findall(r"\d+", b)):
        return False
    for w in _NEG_WORDS:
        if (w in a) != (w in b):
            return False
    return True


def _plausible(entry):
    """按题型做最基本的合法性校验，挡住明显脏数据（如判断题答案 "AC"）。"""
    ans = str(entry.get("answer") or "").strip()
    qtype = entry.get("type") or ""
    if not ans:
        return False
    if qtype == "judge":
        return bool(re.search(
            r"对|错|正确|错误|√|×|是|否|^[TtFf]$|^TR$|^FA$|^[AB]$", ans))
    return True


class QuestionBank:
    def __init__(self, answers_file, pool_file, log):
        self.log = log
        self.answers_file = Path(answers_file)
        self.pool_file = Path(pool_file)
        self.entries = {}     # 归一化题干 -> {"answer","type","source"}
        self._new_records = []
        self._dirty = False
        self._load()

    # ---------------- 加载 ----------------
    def _load(self):
        for path in (self.answers_file, self.pool_file):
            if not path.exists():
                continue
            # 手工题库 answers.json 视为已验证；自动沉淀的 pool 默认未验证。
            # 旧文件里的 source 字段必须保留，不能再被丢掉。
            default_source = "quiz_result" if path == self.answers_file else "online"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"题库文件解析失败 {path}: {exc}")
                continue
            count = 0
            if isinstance(data, dict):
                items = data.get("questions")
                if isinstance(items, list):
                    for item in items:
                        if self._add(item.get("stem", ""), item.get("answer", ""),
                                      item.get("type", ""), item.get("source") or default_source):
                            count += 1
                else:
                    for stem, answer in data.items():
                        if stem == "questions":
                            continue
                        if isinstance(answer, dict):
                            if self._add(stem, answer.get("answer", ""),
                                         answer.get("type", ""),
                                         answer.get("source") or default_source):
                                count += 1
                        else:
                            if self._add(stem, answer, "", default_source):
                                count += 1
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        if self._add(item.get("stem", ""), item.get("answer", ""),
                                     item.get("type", ""),
                                     item.get("source") or default_source):
                            count += 1
            self.log.info(f"已加载题库 {path.name}: {count} 条")

    def _add(self, stem, answer, qtype="", source="online"):
        key = _norm(stem)
        if not key or answer in (None, ""):
            return False
        new = {"answer": str(answer), "type": qtype or "", "source": source or "online"}
        entry = self.entries.get(key)
        if entry is None:
            self.entries[key] = new
            return True
        old_rank = RANK.get(entry.get("source", "online"), 0)
        new_rank = RANK.get(new["source"], 0)
        if new_rank > old_rank:
            entry.update(new)
        elif new_rank == old_rank and not entry.get("type") and new["type"]:
            entry["type"] = new["type"]
        return True

    # ---------------- 查询 ----------------
    def lookup(self, stem):
        """查答案。优先返回"已验证"的条目（来自结果页回读等），
        未验证答案只在没有更好来源时使用（调用方应据此再核对）。

        匹配规则收紧：候选必须唯一、长度接近、数字与否定词一致；答案冲突时不猜（返回 None）。
        """
        key = _norm(stem)

        def pick(entry):
            src = entry.get("source", "online")
            return {"answer": entry.get("answer"), "source": src,
                    "type": entry.get("type", ""),
                    "verified": src in TRUSTED}

        def choose(cands):
            """候选中选可信度最高的一条；若候选答案互相冲突则不猜。"""
            cands = [c for c in cands if _plausible(c)]
            if not cands:
                return None
            cands.sort(key=lambda v: RANK.get(v.get("source", "online"), 0), reverse=True)
            best = cands[0]
            best_ans = _norm(best.get("answer"))
            for c in cands[1:]:
                if _norm(c.get("answer")) != best_ans:
                    self.log.warning("题库候选答案冲突 → 不猜，转大模型/联网：" + key[:18])
                    return None
            return pick(best)

        if not key:
            return None
        if key in self.entries:
            entry = self.entries[key]
            return pick(entry) if _plausible(entry) else None
        # 双向包含匹配（题干较长时有效）→ 必须"真的同一题"
        cands = [v for k, v in self.entries.items()
                 if len(k) >= 12 and (k in key or key in k) and _compatible(k, key)]
        if cands:
            got = choose(cands)
            if got is not None:
                return got
        # 模糊匹配：0.86 → 0.93，且要求数字/否定词一致
        close = difflib.get_close_matches(key, list(self.entries.keys()), n=3, cutoff=0.93)
        close = [k for k in close if _compatible(k, key)]
        if close:
            got = choose([self.entries[k] for k in close])
            if got is not None:
                return got
        return None

    # ---------------- 沉淀 ----------------
    def record(self, stem, answer, qtype="", source="quiz_result"):
        """沉淀题目。source 用于区分来源：quiz_result(结果页回读=已验证) / online(联网搜索,未验证)。

        已存在同题时：仅当新来源可信度更高才覆盖（防止网上错答案污染题库）；
        低可信度不能覆盖高可信度，高可信度可以覆盖低可信度。
        """
        key = _norm(stem)
        if not key or not answer:
            return
        entry = self.entries.get(key)
        new_rank = RANK.get(source, 0)
        old_rank = RANK.get(entry.get("source", "online"), 0) if entry is not None else -1
        if entry is not None and new_rank <= old_rank:
            return
        rec = {"answer": str(answer), "type": qtype or (entry or {}).get("type", ""),
               "source": source}
        if entry is None:
            self.entries[key] = rec
        else:
            entry.update(rec)
        self._new_records.append({
            "stem": stem[:300],
            "answer": str(answer)[:300],
            "type": qtype or "",
            "source": source,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self._dirty = True

    def save_pool(self):
        if not self._new_records:
            return
        self.pool_file.parent.mkdir(parents=True, exist_ok=True)
        existing = []
        if self.pool_file.exists():
            try:
                data = json.loads(self.pool_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    existing = data
                elif isinstance(data, dict):
                    existing = data.get("questions", [])
            except Exception:  # noqa: BLE001
                existing = []
        # 合并 + 按题干去重（同一题保留可信度最高、其次最新的一条），
        # 否则旧错答案会因"文件里先出现"而在重启后复活。
        merged = {}
        for item in list(existing) + list(self._new_records):
            if not isinstance(item, dict):
                continue
            k = _norm(item.get("stem"))
            if not k or item.get("answer") in (None, ""):
                continue
            item.setdefault("source", "online")
            old = merged.get(k)
            if old is None:
                merged[k] = item
                continue
            old_rank = RANK.get(old.get("source", "online"), 0)
            new_rank = RANK.get(item.get("source", "online"), 0)
            if new_rank > old_rank or (new_rank == old_rank
                                       and str(item.get("ts") or "") > str(old.get("ts") or "")):
                merged[k] = item
        self.pool_file.write_text(
            json.dumps(list(merged.values()), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.log.info(f"题库已沉淀 {len(self._new_records)} 条 -> {self.pool_file.name}"
                      f"（去重后共 {len(merged)} 条）")
        self._new_records = []
        self._dirty = False

    @property
    def size(self):
        return len(self.entries)
