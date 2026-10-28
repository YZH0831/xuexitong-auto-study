# -*- coding: utf-8 -*-
"""联网搜题：本地题库未命中时的兜底。

安全限制（全部可配置）：
- 单次运行搜索次数上限（默认 20 次）
- 两次搜索之间的最小间隔（默认 8 秒，随机抖动）
- 题面被平台字体混淆时直接跳过（搜索结果无意义）
- 每一次搜索的题目与结论都会写进日志，便于事后核对
"""
import random
import re
import time
import urllib.parse

from . import reasoning

ENGINES = {
    "bing": "https://cn.bing.com/search?q={q}",
    "baidu": "https://www.baidu.com/s?wd={q}",
    "sogou": "https://www.sogou.com/web?query={q}",
}

# 「答案：A」「正确答案是 AB」等常见表述
ANSWER_PATTERNS = [
    re.compile(r"正确答案\s*[是为：:]\s*([A-Ha-h]{1,8})"),
    re.compile(r"答案\s*[是为：:]\s*([A-Ha-h]{1,8})"),
    re.compile(r"参考答案\s*[是为：:]\s*([A-Ha-h]{1,8})"),
    # "答案选D / 答案应选D / 本题答案选D" —— 中间带"选"字的写法很常见，之前漏了
    re.compile(r"(?:正确答案|参考答案|答案)\s*(?:应该|应|就)?\s*选\s*[:：]?\s*([A-Ha-h]{1,8})"),
    re.compile(r"本题\s*(?:正确)?答案\s*(?:应该|应|就)?\s*选?\s*[:：]?\s*([A-Ha-h]{1,8})"),
]

# 多选题答案常写成 "A、B、D" / "A B D"：上面的正则只会截到第一个字母，
# 必须单独识别"分隔符连接的字母串"，否则多选会被 truncate 成单选（直接丢分）。
MULTI_LETTER_RE = re.compile(
    r"(?:正确答案|参考答案|答案|应选|故选)\s*(?:应该|应|就)?\s*选?\s*[是为：:]?\s*"
    r"([A-Ha-h](?:\s*[、,，/;；]\s*|\s+)[A-Ha-h](?:(?:\s*[、,，/;；]\s*|\s+)[A-Ha-h]){0,6})"
)

# 引擎被限流/验证码页的标记（出现时换引擎）
BLOCK_MARKERS = ("请解决以下难题", "unusual traffic", "captcha", "验证码", "安全验证",
                 "请输入验证", "请点击继续", "访问过于频繁", "系统检测到")

# ---------- AI 解答块（搜狗"AI解题答疑"/百度"AI总结"/作业帮"AI解析"等） ----------
# 这些块里通常包含完整题面 + 明确答案 + 解析，比在整页里盲抓可靠得多
AI_BLOCK_START_RE = re.compile(
    r"(AI\s*解题|AI\s*解答|AI\s*解析|AI\s*总结|AI\s*答题|AI\s*回答|智能解答|解题答疑|AI生成|内容由AI生成)"
)
AI_ANSWER_PATTERNS = [
    # 判断题类（先判否定，避免"不正确"被当成"正确"）
    re.compile(r"判断结果\s*[:：]?\s*(不正确|不对|正确|错误|对|错|√|×)"),
    re.compile(r"(?:该|此)(?:说法|表述|描述|题干|观点)\s*(?:是|为)?\s*(不正确|不对|正确|错误)"),
    re.compile(r"答案\s*[:：是为]?\s*(不正确|不对|正确|错误|对|错|√|×)\b"),
    re.compile(r"(?:因此|所以|故)[^。；\n]{0,14}(不正确|错误|正确|对|错)"),
    re.compile(r"(?:说法|表述|描述)(?:是|为)?\s*(不正确|错误|正确|对)"),
    # 选择题类
    re.compile(r"(?:正确答案|参考答案|答案)\s*[:：是为]?\s*([A-Ha-h]{1,8})\b"),
    re.compile(r"故选\s*[:：]?\s*([A-Ha-h])\b"),
    re.compile(r"应选\s*[:：]?\s*([A-Ha-h])\b"),
    re.compile(r"选择\s*([A-H])\s*[。,，）)]"),
]


# 源页面里的选项串："A 九一八事变 B 卢沟桥事变 C 八一三事变 D 华北事变"
# 注意：必须排除英文单词内部的字母（www.zybang.com 里的 b/a/g 曾被当成选项字母），
# 并要求字母后不是英文字母。
SRC_OPTION_RE = re.compile(r"(?<![A-Za-z])([A-Ha-h])(?![A-Za-z])\s*[\.、．:：)）]?\s*([^A-Ha-h\n]{1,40})")
# 源页选项文本后面常粘着"本题答案/解析/扫码关注"等噪声，先截断再对齐
_SRC_OPT_TAIL_RE = re.compile(
    r"(?:本题|答案|解析|正确|参考|扫码|关注|来源|编辑|下一页|【|（|\(|https?://|www\.|\.com)")


def _opt_text(o):
    """取选项正文：兼容题库/旧解析把字母前缀存进 text 的情况（"A 政府为主"）。"""
    txt = str((o or {}).get("text") or "").strip()
    return re.sub(r"^[A-Ha-h][\.、．:：)）\s]+", "", txt).strip()


def _source_option_map(text, pos, window=650):
    """取源页面 pos 附近的"字母→选项文本"映射。

    关键修复：不同网站的选项顺序不同，源页的"答案 B"指的是**它自己**的 B。
    必须先弄清源页的 B 是什么文本，再按文本对应到我们的选项。
    """
    seg = text[max(0, pos - window): pos + 350]
    # 选项串通常终止于"答案/正确答案"关键字 → 先截断，避免把答案语粘进选项文本
    cut = re.search(r"答案|正确答案|参考答案", seg)
    if cut:
        seg = seg[:cut.start()]
    pairs = {}
    for m in SRC_OPTION_RE.finditer(seg):
        letter = m.group(1).upper()
        txt = m.group(2).strip(" 。;；,，")
        txt = _SRC_OPT_TAIL_RE.split(txt)[0].strip(" 。;；,，、")
        if 1 <= len(txt) <= 38 and letter not in pairs:
            pairs[letter] = txt
    return pairs if len(pairs) >= 2 else {}


def _map_source_letter(src_letter, text, pos, options):
    """把源页字母换算成我们页面的选项字母（经文本对齐）。

    实测坑：选项之间互为子串（"高压电"/"高压电缆"）时，双向包含会先命中短选项，
    把源页的 A 错映射成我们的 A。改为**精确匹配优先**，包含匹配只在唯一且长度接近时采信。
    判断题：源页按 A/B 写（对/错），平台选项字母是 TR/FA → 按判定语映射。
    """
    pairs = _source_option_map(text, pos)
    if not pairs:
        return None
    src_txt = pairs.get(src_letter)
    if not src_txt:
        return None
    src = _norm_answer(src_txt)
    exact, loose = [], []
    for i, o in enumerate(options or []):
        our = _norm_answer(_opt_text(o))
        if not our or not src:
            continue
        letter = o.get("letter") or chr(65 + i)
        if our == src:
            exact.append(letter)
        elif (len(src) >= 2 and src in our) or (len(our) >= 2 and our in src):
            loose.append((letter, abs(len(our) - len(src))))
    if len(exact) == 1:
        return exact[0]
    if not exact and len(loose) == 1:
        # 唯一命中 → 直接采信（源页常写"担当"，我们选项是"青年的担当"，长度差不能当否决理由）
        return loose[0][0]
    if not exact and len(loose) > 1:
        # 多个候选（如"高压电"/"高压电缆"）→ 取长度最接近的，且必须严格优于次优
        loose.sort(key=lambda x: x[1])
        if loose[0][1] < loose[1][1]:
            return loose[0][0]
    # 判断题兜底：源页 A/B（对/错）↔ 平台 TR/FA
    if not exact and not loose:
        idx = _judge_index(src_txt)
        if idx is not None:
            target = _judge_letter(idx, options)
            if target is not None:
                return target
    return None


_TEXT_ANSWER_STOP = {"解析", "分析", "正确", "错误", "解答", "理由", "如下", "见下",
                     "答案", "习题", "试题", "问题", "题目", "考点", "知识点"}


def match_option_by_text(word, options):
    """把一个"文本答案"（如 全民/综合治理）对齐到我们的选项字母。

    文本答案天然免疫"选项顺序不同"的坑，是 AI 解答卡里最可靠的答案形式。
    """
    w = _norm_answer(word)
    if len(w) < 2 or w in _TEXT_ANSWER_STOP:
        return None
    exact = []
    loose = []
    for i, o in enumerate(options or []):
        t = _norm_answer(_opt_text(o))
        if not t:
            continue
        letter = o.get("letter") or chr(65 + i)
        if t == w:
            exact.append(letter)
        elif len(w) >= 2 and (w in t or (len(t) >= 2 and t in w)):
            loose.append(letter)
    if len(exact) == 1:
        return exact[0]
    if not exact and len(loose) == 1:
        return loose[0]
    return None


def _ai_blocks(text):
    """定位页面里的 AI 解答块，返回 [(块起点, 块文本窗口)]（按出现顺序）。"""
    out = []
    for m in AI_BLOCK_START_RE.finditer(text or ""):
        out.append((m.start(), text[m.start(): m.start() + 1500]))
    return out


# 判断题答案的常见写法（字母正则是抓不到的）
JUDGE_ANSWER_RE = re.compile(
    r"(?:正确答案|参考答案|答案|应为|故选)\s*[是为：:]?\s*"
    r"(不正确|不对|正确|错误|对|错|√|×|是|否|[TtFf](?![A-Za-z]))"
)

_JUDGE_FALSE = ("不正确", "不对", "不符", "错误", "错", "×", "否", "非", "false", "no")
_JUDGE_TRUE = ("正确", "对", "√", "是", "true", "yes")


def _judge_index(text):
    """判断题答案归一化：0=对 / 1=错 / None。"""
    t = re.sub(r"\s+", "", str(text or "")).lower()
    if not t:
        return None
    if any(k in t for k in _JUDGE_FALSE):
        return 1
    if any(k in t for k in _JUDGE_TRUE):
        return 0
    if t in ("f", "n", "0", "b", "x"):
        return 1
    if t in ("t", "y", "1", "a"):
        return 0
    return None


def _looks_judge(options):
    for o in options or []:
        if _judge_index(o.get("text") or "") is not None:
            return True
    return False


def _judge_letter(idx, options):
    """把 0/1 映射到选项字母（优先按选项正文匹配）。"""
    for i, o in enumerate(options or []):
        if _judge_index(o.get("text") or "") == idx:
            return o.get("letter") or chr(65 + i)
    if idx < len(options or []):
        o = options[idx]
        return o.get("letter") or chr(65 + idx)
    return None


# 作业帮来源标记（用于判定"命中作业帮站点内容"）
ZYB_MARKERS = ("作业帮", "zybang.com", "zuoyebang.com", "zybang")

# 实测到的字体混淆替换字（学习通会把部分汉字映射成同形生僻字）
OBFUSCATION_MARKERS = "糵孾宎宄宍宊宭宬宱宯宩宐寁寈寉寊寋寏寍寎寔"


def _norm_answer(text):
    return re.sub(r"[\s，。、,.;；:：()（）\[\]【】]+", "", str(text or "")).upper()


def resolve_letters(answer, options):
    """把答案换算成"选项字母集合"：字母型直接取，文本型按选项正文匹配。

    这样 "以上都是"（选项 D 的正文）与 "D" 就能被判为同一答案。
    判断题特殊处理：源答案常写 A/B（对/错），而平台选项字母是 TR/FA，
    必须按位置/判定语映射，否则 "A" 与 "TR" 会被误判成"双源不一致"。
    """
    ans = str(answer or "").strip()
    if not ans or not options:
        return set()
    up = ans.upper().replace(" ", "")
    if up in ("TR", "FA"):
        idx = 0 if up == "TR" else 1
        if idx < len(options):
            return {str(options[idx].get("letter") or chr(65 + idx)).upper()}
    # 字母型（不含中文且全为字母/分隔符）
    if not re.search(r"[\u4e00-\u9fff]", ans) and re.fullmatch(r"[A-Za-z\s、,，;；/和与]*", ans):
        letters = {c for c in ans.upper() if "A" <= c <= "Z"}
        final = set()
        for c in letters:
            real = next((o for o in options if str(o.get("letter") or "").upper() == c), None)
            if real is not None:
                final.add(str(real.get("letter") or c).upper())
            elif 0 <= ord(c) - 64 - 1 < len(options):
                # 源页按 A/B 写，但平台选项字母不是 A/B（如判断题 TR/FA）→ 按位置映射
                o = options[ord(c) - 64 - 1]
                oletter = str(o.get("letter") or "").upper()
                final.add(oletter if oletter and oletter not in ("A", "B") else c)
            else:
                final = set()
                break
        if final:
            return final
    # 文本型：与选项正文做包含匹配
    na = _norm_answer(ans)
    hit = set()
    for i, o in enumerate(options):
        no = _norm_answer(o.get("text") or "")
        if not no:
            continue
        if no == na or (len(no) >= 3 and no in na) or (len(na) >= 3 and na in no):
            hit.add(chr(65 + i))
    return hit


def same_answer(a, b, options=None):
    """两个答案是否等价（优先换算成选项字母集合再比较）。"""
    na, nb = _norm_answer(a), _norm_answer(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if options:
        la, lb = resolve_letters(a, options), resolve_letters(b, options)
        if la and lb:
            return la == lb
    if re.fullmatch(r"[A-Z]{1,8}", na) and re.fullmatch(r"[A-Z]{1,8}", nb):
        return set(na) == set(nb)
    return na in nb or nb in na


def anchor_score(text, options, answer, pos=None):
    """答案块上下文评分：答案附近窗口内出现了几个本题选项正文。

    搜索结果常混入其他题目的答案；分数高说明该答案确实属于本题的答案块。
    pos 为 _extract 给出的**实际命中位置**；缺省时按文本查找。

    注意（实测坑）：单字母答案（A/B/C/D）不能直接 text.find——页面里的 "AI"
    "A." 会命中错误位置，导致候选答案全部同分（实测 A 得 0、B/C/D 得 4 的系统性偏差，
    冲突采信会一直偏向 B/C/D）。缺省时改为取"选项命中数最多"的窗口。
    """
    if not text or not answer:
        return 0
    ans = str(answer)

    def score_at(idx):
        win = text[max(0, idx - 280): idx + 140]
        s = 0
        for o in options or []:
            txt = (o.get("text") or "").strip()[:6]
            if len(txt) >= 2 and txt in win:
                s += 1
        return s

    if pos is not None and pos >= 0:
        return score_at(pos)
    idx = text.find(ans)
    if idx < 0:
        return 0
    if len(ans) <= 1:
        best = 0
        start = 0
        while True:
            j = text.find(ans, start)
            if j < 0:
                break
            best = max(best, score_at(j))
            start = j + 1
        return best
    return score_at(idx)


def evidence_snippet(text, answer, width=48, pos=None):
    """取答案附近的原文片段作为"来源依据"。

    pos 为答案在页面中的**实际命中位置**（由 _extract 给出）；
    不给 pos 时退化为查找首次出现位置（可能不是命中处，仅作兜底）。
    """
    if not text or not answer:
        return ""
    ans = str(answer).strip()
    idx = pos if (pos is not None and pos >= 0) else text.find(ans)
    if idx < 0:
        return ""
    return re.sub(r"\s+", " ", text[max(0, idx - width):idx + len(ans) + width]).strip()


def looks_obfuscated(text, threshold=2):
    """粗判题面是否被字体混淆（如 环境保护法 → 环糵保护孾）。"""
    if not text:
        return False
    hits = sum(1 for ch in text if ch in OBFUSCATION_MARKERS)
    return hits >= threshold


def _judge_pos_letter(src_letter, options):
    """判断题源页只写 A/B（有时 T/F）且没有选项表时的位置映射。"""
    idx = {"A": 0, "B": 1, "T": 0, "F": 1, "TR": 0, "FA": 1}.get(str(src_letter).upper())
    if idx is None or idx >= len(options or []):
        return None
    return options[idx].get("letter") or chr(65 + idx)


_CLEAN_RE = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9]")


def _find_stem_pos(text, stem):
    """在页面文本里找题干位置；忽略全/半角标点与空格差异。

    实测：源页把题干里的（ ）写成 ( ) 时，旧实现 text.find 失败 → 整页答案被丢弃，
    白白损失一个独立来源。这里先精确匹配，失败再用"去标点"文本匹配并映射回原位置。
    """
    if not text or not stem:
        return -1
    raw = str(stem).strip()
    for k in (raw[:12], raw[:8]):
        if k:
            p = text.find(k)
            if p >= 0:
                return p
    clean = _CLEAN_RE.sub("", text)
    stem_clean = _CLEAN_RE.sub("", raw)
    for key in (stem_clean[:12], stem_clean[:8]):
        if not key:
            continue
        cp = clean.find(key)
        if cp >= 0:
            cnt = 0
            for i, ch in enumerate(text):
                if not _CLEAN_RE.match(ch):
                    if cnt == cp:
                        return i
                    cnt += 1
            return -1
    return -1


class OnlineAnswer:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        oc = cfg.quiz.online_search
        self.enabled = bool(oc.enabled)
        self.engine = str(oc.engine or "bing").lower()
        self.max_searches = int(oc.max_searches or 0)
        self.min_interval = float(oc.min_interval_s or 0)
        self.timeout_ms = int(float(oc.timeout_s or 20) * 1000)
        self.skip_when_obfuscated = bool(oc.skip_when_obfuscated)
        self.cross_check = bool(oc.get("cross_check", True))
        self.conflict_policy = str(oc.get("conflict_policy", "prefer_zuoyebang")).lower()
        # 作业帮通道默认用另一个引擎，增强两源独立性
        self.zyb_engine = str(oc.get("zuoyebang_engine", "sogou") or self.engine).lower()
        order = oc.get("engine_order") or [self.engine, "baidu", "sogou"]
        self.engine_order = [str(e).lower() for e in order]
        self._blocked = {}          # 引擎 -> 冷却截止时间戳
        self.count = 0
        self.hits = 0
        self._last = 0.0

    # ---------------- 限额检查 ----------------
    def can_search(self):
        if not self.enabled:
            return False, "联网搜题未启用"
        if self.max_searches and self.count >= self.max_searches:
            return False, f"已达单次运行搜索上限（{self.max_searches} 次）"
        return True, ""

    def _throttle(self):
        if self.min_interval <= 0:
            return
        wait = self._last + self.min_interval - time.time()
        if wait > 0:
            jitter = wait * random.uniform(0.9, 1.2)
            self.log.info(f"  联网搜题频率控制：等待 {jitter:.0f} 秒")
            time.sleep(jitter)

    # ---------------- 主流程 ----------------
    def search(self, context, stem, options, qtype=""):
        """联网搜题，返回答案串（选项字母或选项原文）；找不到返回 None。"""
        ok, why = self.can_search()
        if not ok:
            self.log.warning(f"联网搜题跳过：{why}")
            return None
        if self.skip_when_obfuscated and looks_obfuscated(stem):
            self.log.warning("检测到题面被字体混淆，联网搜索无意义 → 转人工作答")
            return None

        text = self._fetch_text(context, self._build_query(stem, options))
        answer = self._extract(text, options, qtype, stem)
        if answer:
            self.hits += 1
        self.log.info(
            f"  联网搜题[{self.count}/{self.max_searches or '∞'}] {stem[:26]}… → {answer or '未找到'}"
        )
        return answer

    @staticmethod
    def _looks_blocked(text):
        """判断是否为验证码/限流页（内容极短或含验证提示）。"""
        t = (text or "").strip()
        if len(t) < 120:
            return True
        return any(m in t for m in BLOCK_MARKERS)

    def _engine_chain(self, preferred=None):
        """按配置顺序给出可用引擎（被限流的进入冷却，优先用首选）。"""
        order = list(self.engine_order)
        if preferred:
            pref = str(preferred).lower()
            order = [pref] + [e for e in order if e != pref]
        now = time.time()
        alive = [e for e in order if self._blocked.get(e, 0) < now]
        return alive or order

    def _fetch_text(self, context, query, engine=None):
        """打开搜索引擎取回页面文本；被限流/验证码时自动降级到下一个引擎。"""
        for eng in self._engine_chain(engine):
            self._throttle()
            url = ENGINES.get(eng, ENGINES["bing"]).format(q=urllib.parse.quote(query))
            text = ""
            try:
                tab = context.new_page()
                try:
                    tab.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    tab.wait_for_timeout(random.randint(1500, 3200))
                    text = tab.evaluate("() => (document.body ? document.body.innerText : '')") or ""
                finally:
                    try:
                        tab.close()
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"联网搜题失败（{eng}）: {str(exc)[:80]}")
            self.count += 1
            self._last = time.time()
            if text and not self._looks_blocked(text):
                return text
            # 被限流：冷却 10 分钟后换下一个引擎
            self._blocked[eng] = time.time() + 600
            self.log.warning(f"  {eng} 返回验证码/空页（疑似被限流）→ 冷却 10 分钟，改用其它引擎")
        return ""

    def search_cross(self, context, stem, options, qtype=""):
        """双源核对：通用搜索 + 作业帮交叉验证。

        规则：① 两源一致 → 判定"双源有效"；② 不一致 → 打印双方答案与依据片段，
        并按 `conflict_policy` 给出采信结果及理由（prefer_zuoyebang / prefer_search / manual）。
        ③ 仅单源有结果 → 标记为未验证（不满足双源一致）。
        返回 {"answer","source","agree","note","a1","a2","ev1","ev2","zyb_hit"}
        """
        res = {"answer": None, "source": "", "agree": False, "note": "",
               "a1": None, "a2": None, "ev1": "", "ev2": "", "zyb_hit": False}
        ok, why = self.can_search()
        if not ok:
            self.log.warning(f"联网搜题跳过：{why}")
            return res
        if self.skip_when_obfuscated and looks_obfuscated(stem):
            self.log.warning("检测到题面被字体混淆，联网搜索无意义 → 转人工作答")
            return res

        base = self._build_query(stem, options)
        text1 = self._fetch_text(context, base)
        a1 = self._extract(text1, options, qtype, stem)
        pos1 = getattr(self, "_last_pos", -1)
        kind1 = getattr(self, "_last_kind", "")
        strong1 = getattr(self, "_last_strong", False)
        if not a1:      # 兜底：简化查询再试一次
            text1b = self._fetch_text(context, self._fallback_query(stem))
            a1b = self._extract(text1b, options, qtype, stem)
            if a1b:
                a1, text1 = a1b, text1b
                pos1 = getattr(self, "_last_pos", -1)
                kind1 = getattr(self, "_last_kind", "")
                strong1 = getattr(self, "_last_strong", False)
                self.log.info(f"  源① 简化查询命中：{a1b}")
        self.log.info(f"  源① 通用搜索({self.engine})[{self.count}] {stem[:22]}… → {a1 or '未找到'}")

        text2 = self._fetch_text(context, f"作业帮 {base}", engine=self.zyb_engine)
        a2 = self._extract(text2, options, qtype, stem)
        pos2 = getattr(self, "_last_pos", -1)
        kind2 = getattr(self, "_last_kind", "")
        strong2 = getattr(self, "_last_strong", False)
        if not a2:      # 兜底：简化查询
            text2b = self._fetch_text(context, f"作业帮 {self._fallback_query(stem)}",
                                      engine=self.zyb_engine)
            a2b = self._extract(text2b, options, qtype, stem)
            if a2b:
                a2, text2 = a2b, text2b
                pos2 = getattr(self, "_last_pos", -1)
                self.log.info(f"  源② 简化查询命中：{a2b}")
        # 作业帮来源判定：只认域名，不能只靠页面里出现"作业帮"三个字
        # （实测踩坑：题库站友情链接里带"作业帮"就被误判为作业帮内容，冲突时会错误采信）
        zyb_hit = any(m in (text2 or "") for m in ("zybang.com", "zuoyebang.com"))
        self.log.info(f"  源② 作业帮({self.zyb_engine})[{self.count}] {stem[:22]}… → {a2 or '未找到'}"
                      + ("（命中作业帮站点内容）" if zyb_hit else "（页面未见作业帮站点内容）"))
        ev1 = evidence_snippet(text1, a1, pos=pos1)
        ev2 = evidence_snippet(text2, a2, pos=pos2)
        res.update(a1=a1, a2=a2, ev1=ev1, ev2=ev2, zyb_hit=zyb_hit)

        if a1 and a2 and same_answer(a1, a2, options):
            res.update(answer=a1, source="cross_checked", agree=True,
                       note=f"双源一致（通用={a1}｜作业帮={a2}）→ 判定有效")
            self.hits += 1
            self.log.info(f"  ✔ 双源一致，判定有效：{a1}")
        elif a1 or a2:
            if a1 and a2:
                self.log.warning("  ⚠ 双源不一致：")
                self.log.warning(f"     通用搜索：{a1}｜依据：{ev1 or '（无直接依据片段）'}")
                self.log.warning(f"     作业帮　：{a2}｜依据：{ev2 or '（无直接依据片段）'}"
                                 + ("，命中作业帮站点内容" if zyb_hit else "，未确认站点来源"))
                policy = self.conflict_policy
                # ★ 第一判据：证据里到底有没有**本题题干**
                #   （实测踩坑：作业帮那份证据其实是隔壁题的答案块，选项多所以评分虚高）
                def _stem_near(text, pos, stem, span=500):
                    if not text or not stem:
                        return False
                    # 归一化：去掉空白与全/半角标点（实测：源页把（）写成()会导致题干命中翻转）
                    norm = lambda s: re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", str(s or ""))
                    key = norm(stem)[:10]
                    if not key:
                        return False
                    seg = norm(text[max(0, pos - span): pos + span])
                    return key in seg

                hit1 = _stem_near(text1, pos1 if pos1 >= 0 else 0, stem)
                hit2 = _stem_near(text2, pos2 if pos2 >= 0 else 0, stem)
                s1 = anchor_score(text1, options, a1, pos1)
                s2 = anchor_score(text2, options, a2, pos2)
                self.log.warning(f"     答案块上下文评分：通用 {s1} / 作业帮 {s2}"
                                 f"（窗口内命中本题选项正文的个数）")
                if policy == "manual":
                    adopted, reason = None, "配置要求：冲突时转人工核对"
                else:
                    adopted, reason = None, ""
                    # ★ 多选：一方答案是另一方的**严格超集**时优先超集。
                    #   实测 2026-10-19：通用只抽出第 1 个选项、作业帮抽出全部 4 个，
                    #   旧逻辑却因"通用证据含题干"采信单项 → 多选只选 1 项。
                    if qtype == "multi":
                        set1, set2 = resolve_letters(a1, options), resolve_letters(a2, options)
                        if set1 and set2 and set1 < set2:
                            adopted = a2
                            reason = (f"多选：作业帮答案更完整（{''.join(sorted(set2))} ⊃ "
                                      f"{''.join(sorted(set1))}）")
                        elif set1 and set2 and set2 < set1:
                            adopted = a1
                            reason = (f"多选：通用搜索答案更完整（{''.join(sorted(set1))} ⊃ "
                                      f"{''.join(sorted(set2))}）")
                    if adopted is None and strong1 != strong2:
                        # ★ 有"答案/故选/正确答案"明确标记的一方优先：
                        #   实测 2026-10-24 某思政课：作业帮证据写着「答案选D」，
                        #   通用那边只是正文里出现了某个选项词，评分相同就被采信 → 答错。
                        if strong1:
                            adopted, reason = a1, "通用搜索的证据带明确答案标记（答案/故选），比另一方的正文推测可靠"
                        else:
                            adopted, reason = a2, "作业帮的证据带明确答案标记（答案/故选），比另一方的正文推测可靠"
                        self.log.warning(f"     明确标记：通用={'有' if strong1 else '无'} / "
                                         f"作业帮={'有' if strong2 else '无'} → 采信有标记方")
                    if adopted is None and kind1 != kind2 and "text" in (kind1, kind2):
                        # ★ 文本答案（按选项正文对齐）比裸字母可靠：字母依赖对方页面的选项顺序
                        if kind1 == "text":
                            adopted, reason = a1, "通用搜索给的是文本答案（按选项正文对齐），比字母更可靠"
                        else:
                            adopted, reason = a2, "作业帮给的是文本答案（按选项正文对齐），比字母更可靠"
                        self.log.warning(f"     答案形态：通用={kind1 or '字母'} / 作业帮={kind2 or '字母'} "
                                         f"→ 采信文本答案")
                    elif adopted is None and hit1 != hit2:
                        # 只有一方的证据里出现了本题题干 → 采信它；另一方多半抓的是别的题
                        if hit1:
                            adopted, reason = a1, "通用搜索的证据含本题题干，作业帮证据对应的是别的题"
                        else:
                            adopted, reason = a2, "作业帮的证据含本题题干，通用搜索证据对应的是别的题"
                        self.log.warning(f"     题干命中：通用={'是' if hit1 else '否'} / "
                                         f"作业帮={'是' if hit2 else '否'} → 按题干命中采信")
                    elif adopted is None and s1 != s2:
                        if s1 > s2:
                            adopted = a1
                            reason = f"答案块评分更高（通用 {s1} > 作业帮 {s2}），该答案更可能属于本题"
                        else:
                            adopted = a2
                            reason = f"答案块评分更高（作业帮 {s2} > 通用 {s1}），该答案更可能属于本题"
                    elif adopted is None and policy == "prefer_search":
                        adopted, reason = a1, "评分相同，按配置优先采信通用搜索"
                    elif adopted is None and zyb_hit:
                        adopted, reason = a2, "评分相同，作业帮为题目-答案库且有站点内容命中"
                    elif adopted is None:
                        adopted, reason = a1, "评分相同且作业帮来源未确认，暂采信通用搜索"
                res.update(answer=adopted, agree=False,
                           source="online_conflict" if adopted else "",
                           note=f"双源冲突（通用={a1} / 作业帮={a2}）→ "
                                f"{'采信 ' + str(adopted) if adopted else '不采信'}")
                if adopted:
                    self.hits += 1
                self.log.warning(f"  → 最终采信：{adopted or '不采信（转人工）'}；理由：{reason}")
            else:
                one, which = (a1, "通用搜索") if a1 else (a2, "作业帮")
                src_text = text1 if a1 else text2
                # ★ 单源答案再用"解析结论"复核一次：解析里的结论句是文本对文本，
                #   不受对方页面选项顺序影响，比孤零零一个字母可靠
                ev = self.analysis_text(src_text, stem)
                r_ans, r_conf, r_why = reasoning.decide(stem, options, ev, qtype) if ev else (None, 0, "")
                # 判断题：字面答案（答案：对/错）比启发式"解析结论"可靠得多。
                # 实测 2026-10-18：单源 TR 被 0.78 置信的推理改成 FA，直接答错。
                # → 判断题要求 ≥0.95 才允许推理推翻；当前 decide_judge 上限 0.78，
                #   等价于"字面答案优先"。
                judge_q = (qtype == "judge" or _looks_judge(options))
                need_conf = 0.95 if judge_q else 0.75
                if r_ans and r_conf >= need_conf:
                    if str(r_ans).upper() == str(one).upper():
                        res.update(answer=one, source="online_verified", agree=False,
                                   note=f"{which}答案 {one} 与解析结论一致（{r_why}，置信 {r_conf:.2f}）")
                        self.log.info(f"  单源 {one} 经解析复核一致 ✔（置信 {r_conf:.2f}）")
                    else:
                        res.update(answer=r_ans, source="reasoning", agree=False,
                                   note=f"单源{which}给 {one}，但解析结论指向 {r_ans} → 采信解析")
                        self.log.warning(f"  ⚠ 单源 {one} 与解析结论 {r_ans} 不一致 "
                                         f"（置信 {r_conf:.2f}）→ 采信解析结论：{r_why}")
                        res["ev1" if which == "通用搜索" else "ev2"] = ev[:200]
                else:
                    res.update(answer=one, source="online", agree=False,
                               note=f"仅 {which} 有结果（{one}），未达成双源一致")
                    self.log.info(f"  仅单源有结果（{which}）：{one} — 未满足「双源一致」，记为未验证")
                self.hits += 1
        else:
            # ③ 两源都没吐出字面答案 → 本地推理：把"解析文本"当解题过程，选语义最贴合的选项
            for label, src in (("通用", text1), ("作业帮", text2)):
                ev = self.analysis_text(src, stem)
                if not ev:
                    continue
                ans, conf, why = reasoning.decide(stem, options, ev, qtype)
                if ans and conf >= 0.55:
                    res.update(answer=ans, source="reasoning", agree=False,
                               note=f"{label}解析推理作答（置信 {conf:.2f}）：{why}")
                    res["ev1" if label == "通用" else "ev2"] = ev[:200]
                    self.hits += 1
                    self.log.info(f"  🧠 本地推理（{label}解析）→ {ans}（置信 {conf:.2f}，{why}）")
                    self.log.info(f"     依据：{ev[:90]}")
                    break
            if not res.get("answer") and qtype == "judge":
                # ★ 判断题：多查询变体再搜（核心句/判断句式），穷尽合理手段
                base_v = self._build_query(stem, options)
                tried = {base_v, self._fallback_query(stem)}
                for vq in self._judge_query_variants(stem):
                    if vq in tried or not self.can_search()[0]:
                        continue
                    tried.add(vq)
                    t_v = self._fetch_text(context, vq)
                    if not t_v:
                        continue
                    a_v = self._extract(t_v, options, "judge", stem)
                    if a_v:
                        res.update(answer=a_v, source="online",
                                    agree=False, note="多路搜索命中（查询变体）")
                        res["ev1"] = t_v[:200]
                        self.hits += 1
                        self.log.info(f"  ⚖ 判断题变体搜索命中 → {a_v}")
                        break
                    ev_v = self.analysis_text(t_v, stem)
                    r_a, r_c, _why = reasoning.decide(stem, options, ev_v, "judge") if ev_v else (None, 0, "")
                    if r_a and r_c >= 0.55:
                        res.update(answer=r_a, source="reasoning",
                                    note=f"变体页面解析推理（置信 {r_c:.2f}）")
                        self.hits += 1
                        self.log.info(f"  ⚖ 判断题变体+解析推理 → {r_a}（置信 {r_c:.2f}）")
                        break
            if not res.get("answer"):
                self.log.info("  双源均未找到答案 → 转推理兜底/人工作答")
        return res

    @staticmethod
    def _fallback_query(stem):
        """兜底查询：去掉括号补充说明与题型词，取前半段 + "答案"。

        主查询（题干+选项）常因题干过长/含书名号等搜不到结果，退一步用核心题干再试一次。
        """
        s = re.sub(r"[（(][^）)]{0,40}[）)]", " ", str(stem or ""))
        s = re.sub(r"\s+", " ", s).strip()
        return f"{s[:36]} 答案"

    def _judge_query_variants(self, stem):
        """判断题的查询变体：原文 → 核心句 → 判断句式（多路搜索，穷尽合理手段）。"""
        core = re.sub(r"[（(].*?[)）]", "", stem or "")
        core = re.sub(r"[，。？！、；:\s]", "", core)
        seen, out = set(), []
        for q in (stem, core[:18], f"判断 {core[:16]}", f"{core[:14]} 对吗"):
            k = (q or "").strip()
            if k and k not in seen:
                seen.add(k)
                out.append(k)
        return out[:4]

    def search_multi_full(self, context, stem, options):
        """多选题完整性补搜：专搜"答案列表"，逐个选项匹配，避免漏选。

        返回 (字母串, 依据片段)；找不到返回 (None, "")。
        """
        core = re.sub(r"[（(].*?[)）]", "", stem or "")
        core = re.sub(r"[，。？！、；:]", "", core)[:18]
        for vq in (f"{core} 多选题答案", f"{core} 答案", self._fallback_query(stem)):
            try:
                if not self.can_search()[0]:
                    break
            except Exception:  # noqa: BLE001
                pass
            text = self._fetch_text(context, vq)
            if not text:
                continue
            m = re.search(r"答案\s*[:是为]?\s*([^\n。；]{2,110})", text)
            if not m:
                continue
            seg = m.group(1)
            picked = []
            for i, o in enumerate(options or []):
                txt = (o.get("text") or "").strip()
                if len(txt) >= 2 and txt[:8] in seg:
                    picked.append(o.get("letter") or chr(65 + i))
            if len(picked) >= 2:
                self.hits += 1
                return "".join(sorted(set(picked))), seg[:60]
        return None, ""

    def analysis_text(self, text, stem, limit=1800):
        """取页面的"解题过程"文本（**尽量只保留解析段**，去掉选项列表）。

        推理打分最怕证据里混着选项列表——那样每个选项都会命中，无法区分。
        """
        if not text:
            return ""
        key = (stem or "").strip()[:12]
        key_pos = text.find(key) if key else -1
        block = ""
        for pos, blk in _ai_blocks(text):
            if key_pos >= 0 and abs(pos - key_pos) > 2500:
                continue
            block = blk
            break
        if not block and key_pos >= 0:
            block = text[key_pos: key_pos + limit]
        if not block:
            block = text[:limit]
        # 从"解析/分析/解答/理由"处开始截取——那之后才是真正的推理内容
        m = re.search(r"(AI\s*解析|答案解析|解析|分析|解答|理由)[：: ]", block)
        if m:
            return block[m.start():][:limit]
        return block[:limit]

    def _build_query(self, stem, options):
        q = re.sub(r"\s+", " ", stem or "").strip()[:70]
        opt = " ".join((o.get("text") or "") for o in (options or [])[:4])[:120]
        return f"{q} {opt} 答案"

    def _extract(self, text, options, qtype="", stem=""):
        """从搜索结果抽取答案：优先「答案：X」，其次命中选项原文。

        多选题要"选全"：① 字母型答案取**最长**的字母串（ABD 优于 A）
        ② 文本型答案返回**所有**命中的选项原文（用、连接），避免只取一个导致漏选。
        """
        if not text:
            return None
        n_opts = max(1, len(options or []))
        limit_letter = chr(64 + min(8, n_opts))
        # 候选答案带"与题干关键词的距离"：搜索结果里常混有其他题目的答案，
        # 取离本题最近的那个（距离相同则取字母更长的，利于多选）
        self._last_pos = -1
        self._last_kind = ""          # letter / text / judge
        self._last_strong = False     # 答案是否来自"答案/故选/正确答案"这类明确标记
        key_pos = _find_stem_pos(text, stem)
        # ⓪-multi 多选字母答案的"分隔符写法"："答案：A、B、D" / "答案：A B D"。
        #   现有多数字母正则会截成 "A"，这里整体识别后逐个对齐到本题选项。
        if qtype == "multi":
            for m in MULTI_LETTER_RE.finditer(text):
                if key_pos >= 0 and abs(key_pos - m.start()) > 1500:
                    continue
                letters = re.sub(r"[^A-Ha-h]", "", m.group(1)).upper()
                if len(letters) < 2:
                    continue
                mapped = []
                for c in letters:
                    mc = _map_source_letter(c, text, m.start(), options)
                    if mc is None:
                        mapped = []
                        break
                    mapped.append(mc)
                if mapped:
                    self._last_pos = m.start()
                    self._last_kind = "letter"
                    return "".join(sorted(set(mapped)))
        # ⓪ AI 解答块优先：搜狗/百度/作业帮的结果常自带"AI解题/AI解析"块，
        #    块内含完整题面与明确答案，比在整页里盲抓可靠得多
        for pos, block in _ai_blocks(text):
            if key_pos >= 0 and abs(pos - key_pos) > 2000:
                continue                      # 离本题太远的 AI 块不作数
            matches = []
            for pat in AI_ANSWER_PATTERNS:
                for am in pat.finditer(block):
                    matches.append((am.start(), am.group(1)))
            # ★ 先看"答案 <文本>"（如「答案 全民」）——AI 卡片最常见的写法，
            #   也是唯一不怕"选项顺序不同"的答案形式
            if qtype == "multi":
                # 多选文本答案："答案 一般化学品、易制毒化学品、剧毒化学品"
                # 必须逐段对齐并全部选中，只取第一个会漏选（多选少选只得部分分/0 分）。
                for tm in re.finditer(r"答案\s*[:：是为]?\s*([^\n。；;]{2,80})", block):
                    seg = tm.group(1)
                    picked = []
                    for part in re.split(r"[、,，;；/\s]+", seg):
                        part = part.strip()
                        if len(part) < 2:
                            continue
                        hit = match_option_by_text(part, options)
                        if hit and hit not in picked:
                            picked.append(hit)
                    if len(picked) >= 2:
                        self._last_pos = pos + tm.start()
                        self._last_kind = "text"
                        return "".join(sorted(set(picked)))
            else:
                for tm in re.finditer(r"答案\s*[:：是为]?\s*([\u4e00-\u9fa5A-Za-z0-9]{2,12})", block):
                    hit = match_option_by_text(tm.group(1), options)
                    if hit:
                        self._last_pos = pos + tm.start()
                        self._last_kind = "text"
                        return hit
            if not matches:
                continue
            # 块内可能同时含"上一题答案"与"本题答案"：优先取题干之后的第一处
            # （实测："上一题答案 B" 在前时，旧逻辑会把 B 当成本题答案）
            rel = key_pos - pos if key_pos >= 0 else -1
            if rel >= 0:
                after = [x for x in matches if x[0] >= rel]
                if after:
                    matches = after
            matches.sort(key=lambda x: x[0])
            for start, cap in matches[:4]:
                if qtype == "judge" or _looks_judge(options):
                    idx = _judge_index(cap)
                    if idx is None:
                        continue
                    letter = _judge_letter(idx, options)
                    if letter:
                        self._last_pos = pos + start
                        return letter
                else:
                    letters = re.sub(r"[^A-Ha-h]", "", cap).upper()
                    letters = "".join(sorted(set(letters)))
                    if not letters or len(letters) > 6:
                        continue
                    if not all("A" <= c <= limit_letter for c in letters):
                        continue
                    # 源页字母 ≠ 我们的字母（选项顺序不同）→ 先经文本对齐换算；
                    # 对不齐的字母不采信（答错=空答=0 分，瞎选无收益）
                    mapped = []
                    for c in letters:
                        mc = _map_source_letter(c, block, start, options)
                        if mc is None:
                            mapped = []
                            break
                        mapped.append(mc)
                    if mapped:
                        self._last_pos = pos + start
                        return "".join(sorted(set(mapped)))

        # ① 判断题优先：答案写作"对/错/正确/错误/√/×"时字母正则匹配不到
        if qtype == "judge" or _looks_judge(options):
            for jm in JUDGE_ANSWER_RE.finditer(text):
                idx = _judge_index(jm.group(1))
                if idx is None:
                    continue
                if key_pos >= 0 and abs(key_pos - jm.start()) > 600:
                    continue        # 离本题太远，视为别的题
                letter = _judge_letter(idx, options)
                if letter:
                    self._last_pos = jm.start()
                    self._last_kind = "judge"
                    return letter

        cands = []
        for pat in ANSWER_PATTERNS:
            for m in pat.finditer(text):
                letters = re.sub(r"[^A-Ha-h]", "", m.group(1)).upper()
                if not letters or not all("A" <= c <= limit_letter for c in letters):
                    continue
                dist = abs(key_pos - m.start()) if key_pos >= 0 else 10 ** 9
                cands.append((dist, len(letters), letters, m.start()))
        if cands:
            if key_pos >= 0:
                # 硬约束：答案必须出现在题干附近，否则是页面里别的题/热搜里的噪声
                near = [c for c in cands if c[0] <= 600]
            else:
                # 页面上找不到本题题干 → 裸字母答案一律不采信（噪声太多）
                near = []
            if near:
                near.sort(key=lambda x: (x[0], -x[1]))
                picked = near[0] if qtype != "multi" else max(near[:3], key=lambda x: len(x[2]))
                best = picked[2]
                abs_pos = picked[3]          # 真实命中位置（不能再用 dist 反推）
                mapped = []
                for c in best:
                    mc = _map_source_letter(c, text, abs_pos, options)
                    if mc is None:
                        mapped = []
                        break
                    mapped.append(mc)
                # 判断题兜底：源页只写 A/B、且页面没有选项表时，按位置映射到对/错
                if not mapped and len(best) == 1 and (qtype == "judge" or _looks_judge(options)):
                    fallback = _judge_pos_letter(best, options)
                    if fallback:
                        mapped = [fallback]
                if mapped:
                    self._last_pos = abs_pos
                    self._last_kind = "letter"
                    # 候选里若带 答案/故选/正确答案 前缀，视为明确标记
                    _cand_text = text[max(0, abs_pos - 24): abs_pos + 24]
                    self._last_strong = bool(re.search(r"(答案|故选|应选|正确选项)", _cand_text))
                    return "".join(sorted(set(mapped)))

        # ② "答案 <选项正文>"型（含无冒号的 AI 解答块，如"答案 电流"）
        #    多选必须把所有命中的选项都返回，只返回第一个 = 漏选。
        for m_txt in re.finditer(r"答案\s*[是为：:]?\s*([^\n。；;]{2,80})", text):
            seg = m_txt.group(1)
            picked = []
            for i, o in enumerate(options or []):
                txt = (o.get("text") or "").strip()
                if len(txt) < 2:
                    continue
                if txt[:12] in seg:
                    picked.append(o.get("letter") or chr(65 + i))
            if not picked:
                continue
            if qtype == "multi":
                # 仅在"答案后跟着用分隔符连起来的多个选项正文"时采信，
                # 避免把"答案 A 甲选项 B 乙选项…"这种选项列表误当答案。
                if len(picked) >= 2 and re.search(r"[、,，;；/]", seg):
                    self._last_pos = m_txt.start()
                    self._last_kind = "text"
                    return "".join(sorted(set(picked)))
                continue
            self._last_pos = m_txt.start()
            self._last_kind = "text"
            return picked[0]

        hits = []
        opt_texts = []
        for o in options or []:
            raw = _opt_text(o)
            opt_texts.append(raw)
            if len(raw) >= 4 and text.count(raw) >= 2 and raw not in hits:
                hits.append(raw)
        if hits:
            pos_hit = text.find(hits[0])
            self._last_pos = pos_hit if pos_hit >= 0 else -1
            self._last_kind = "text"          # 选项原文命中 = 文本答案（按内容对齐）
            if len(hits) > 1 and qtype in ("single", "judge"):
                # 单选题不该有多个答案：取最长的那个（信息最完整，最可能是本题考点）
                hits.sort(key=len, reverse=True)
                return hits[0]
            if qtype == "multi":
                # 多选答案常是一串相邻选项正文（"X、Y、Z"），但每个选项在页面里的
                # 出现次数不一定都 ≥2 → 只用"≥2 次"会漏掉后面的选项。
                # 实测 2026-10-19：通用搜索只命中第 1 项，导致多选只选 1 个。
                best, best_pos = list(hits), self._last_pos
                for h in hits:
                    start = 0
                    while True:
                        p = text.find(h, start)
                        if p < 0:
                            break
                        start = p + 1
                        tail = text[p + len(h): p + len(h) + 300]
                        # 只在同一句里补全，避免把下一题/无关段落的选项正文也算进来
                        end = re.search(r"[。；;！!？?\n]", tail)
                        seg = tail[:end.start()] if end else tail
                        ext = list(hits)
                        for t in opt_texts:
                            if len(t) >= 4 and t not in ext and t in seg:
                                ext.append(t)
                        if len(ext) > len(best):
                            best, best_pos = ext, p
                if len(best) > len(hits):
                    self._last_pos = best_pos
                    ordered = [t for t in opt_texts if t in best]
                    self._last_kind = "text"
                    return "、".join(ordered)
            return "、".join(hits) if len(hits) > 1 else hits[0]
        return None

    def stats(self):
        return {"searches": self.count, "hits": self.hits, "limit": self.max_searches}
