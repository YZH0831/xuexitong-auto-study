# -*- coding: utf-8 -*-
"""本地推理作答（无需 API Key）。

思路（借用推理模型的答题方式）：
1. 先把证据文本（AI 解答块 / 解析段）当作"解题过程"来读，而不是只找"答案：X"这种字面标记；
2. 选择题：算**每个选项与解题过程的语义重合度**（中文双字组 shingle），
   重合度明显最高的才算答案，且要求与第二名拉开差距，否则弃权；
3. 判断题：读解题过程里的判定语（"应改为/不符合/有误" vs "符合/正确/成立"），
   并识别绝对化措辞（所有/一定/必须/任何）与"过于绝对"类提示；
4. 一律输出置信度，低于门槛就弃权（宁缺勿错）。
"""
import re

# 判断题：否定/纠错类判定语（优先于肯定）
_NEG_CUES = (
    "不正确", "不对", "错误", "有误", "不符", "不符合", "应改为", "应该改为",
    "不能", "不得", "不可", "严禁", "禁止", "并非", "而不是", "说法不成立",
    "过于绝对", "以偏概全", "不准确", "不完整",
)
_POS_CUES = (
    "正确", "说法成立", "符合", "可行", "无误", "应当", "是对的", "表述准确",
    "可以", "允许", "必须",
)
# 绝对化措辞：判断题里常暗示表述过强
_ABSOLUTE_WORDS = ("所有", "一切", "任何", "一定", "必须", "只要", "都", "完全", "唯一", "绝不")


def _clean(text):
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]+", "", text or "")


def _bigrams(text):
    s = _clean(text)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


# ---------- 判断题兜底先验（穷尽搜索后仍无答案时使用） ----------
_ABSOLUTE_WORDS = ("所有", "一切", "任何", "一定", "总是", "全部", "唯一",
                   "完全", "绝不", "只要", "凡是", "都")
_RULE_WORDS = ("不能", "不得", "严禁", "禁止", "应当", "必须", "需要", "要注意", "杜绝")


def judge_best_guess(stem):
    """按题库惯例给判断题一个"最可能的判断"。

    线索：
    - 绝对化措辞（所有/任何/一定/只要…就）→ 题库惯例多为"错"
    - 规范性表述（不能/严禁/应当）→ 多为"正确说法"
    返回 (0=对 / 1=错, 置信度, 理由)；这是穷尽搜索后的兜底，置信度如实标注。
    """
    s_txt = stem or ""
    abs_hits = [w for w in _ABSOLUTE_WORDS if w in s_txt]
    rule_hits = [w for w in _RULE_WORDS if w in s_txt]
    if abs_hits and not rule_hits:
        return 1, 0.62, f"含绝对化措辞（{'、'.join(dict.fromkeys(abs_hits[:2]))}）——题库惯例多为错"
    if rule_hits and not abs_hits:
        return 0, 0.58, f"含规范性表述（{'、'.join(rule_hits[:2])}）——多为正确说法"
    if abs_hits and rule_hits:
        return 1, 0.55, "同时含绝对化与规范表述，按惯例倾向错"
    return 0, 0.50, "无线索：按判断题基频倾向（对略多），置信度最低"


def _sentences(text):
    return [x.strip() for x in re.split(r"[。；;！!？?\n]", text or "") if x.strip()]


# ---------------- 选择题：按证据给每个选项打分 ----------------
def score_options(evidence, options):
    """返回每个选项与证据的语义贴合度（0~1）。

    分数 = 0.6×双字组覆盖率 + 0.4×核心词命中（选项前 4 字是否出现在解析里）——
    解析通常只重述关键概念，不会原样复述整句选项，所以不能只看完整覆盖。
    （调用方需保证 evidence 是"解析段"，不含选项列表本身，否则各选项会同分。）
    """
    text = evidence or ""
    ev = _bigrams(text)
    out = []
    for o in options or []:
        txt = (o.get("text") or "").strip()
        og = _bigrams(txt)
        if not og:
            out.append(0.0)
            continue
        cover = len(og & ev) / len(og)
        core = txt[:4]
        core_hit = 1.0 if len(core) >= 2 and core in text else 0.0
        out.append(round(0.6 * cover + 0.4 * core_hit, 3))
    return out


# 解析里的"结论式表述"：AI 解释常用"因此用「X」…最为准确"收尾，X 就是答案
_QUOTE = "[\u201c\u201d\"\'「」『』]"
_CONCLUSION_RES = [
    # 因此用"一体化"来描述…最为准确
    re.compile(r"(?:用|采用|使用)\s*" + _QUOTE + r"?([\u4e00-\u9fa5A-Za-z0-9]{2,12})" + _QUOTE + r"?\s*(?:来|去)?[^。；]{0,12}?(?:最为准确|最准确|最合适|最恰当|才正确|才是对的)"),
    # 正确答案是"X" / 正确选项为X
    re.compile(r"(?:正确(?:答案|选项|表述)|应选|答案)\s*(?:是|为|:|：)\s*" + _QUOTE + r"?([\u4e00-\u9fa5A-Za-z0-9]{2,12})" + _QUOTE + r"?"),
    # …最为准确的描述是X
    re.compile(r"(?:最为准确|最准确|最合适|最恰当)的?[^。；]{0,6}?(?:是|为)\s*" + _QUOTE + r"?([\u4e00-\u9fa5A-Za-z0-9]{2,12})" + _QUOTE + r"?"),
]


def _conclusion_answer(evidence, options):
    """从解析的结论句里直接取答案（比词频打分更硬）。"""
    for pat in _CONCLUSION_RES:
        for m in pat.finditer(evidence or ""):
            term = re.sub(r"\s+", "", m.group(1))
            if len(term) < 2:
                continue
            for i, o in enumerate(options or []):
                t = re.sub(r"\s+", "", o.get("text") or "")
                if not t:
                    continue
                if t == term or (len(t) >= 2 and (t in term or term in t)):
                    return o.get("letter") or chr(65 + i), 0.88
    return None, 0.0


def decide_choice(evidence, options, qtype="single", min_gap=0.15, min_hit=0.45):
    """按证据选答案。返回 (字母, 置信度) 或 (None, 0)。"""
    if not evidence or not options:
        return None, 0.0
    # ① 先看解析的结论句（最可靠）
    ca, cc = _conclusion_answer(evidence, options)
    if ca and (qtype != "multi"):
        return ca, cc
    scores = score_options(evidence, options)
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    best = scores[order[0]]
    second = scores[order[1]] if len(scores) > 1 else 0.0
    letters = [options[i].get("letter") or chr(65 + i) for i in order]

    if qtype == "multi":
        picked = [letters[i] for i, sc in enumerate(scores) if sc >= max(min_hit, best * 0.7)]
        if len(picked) >= 2 and best >= min_hit:
            conf = min(0.9, 0.5 + best * 0.4)
            return "".join(sorted(set(picked))), round(conf, 2)
        return None, 0.0

    if best >= min_hit and (best - second) >= min_gap:
        conf = min(0.92, 0.45 + best * 0.5)
        return letters[0], round(conf, 2)
    return None, 0.0


# ---------------- 判断题：读判定语 ----------------
def decide_judge(evidence, stem=""):
    """返回 (0=对 / 1=错 / None, 置信度)。"""
    if not evidence:
        return None, 0.0
    sents = _sentences(evidence)

    # ① 解析/理由句里的判定语（最可信）
    for sent in sents:
        if not re.search(r"(解析|解答|理由|这是因为|原因|因此|所以|说明)", sent):
            continue
        neg = next((c for c in _NEG_CUES if c in sent), None)
        pos = next((c for c in _POS_CUES if c in sent), None)
        if neg and not re.search(r"不" + re.escape(neg), sent):
            return 1, 0.78
        if pos and not neg:
            return 0, 0.72

    # ② "该说法…" 直接判定
    m = re.search(r"(?:该|此)?(?:说法|表述|描述|题干|观点)\s*(?:是|为)?\s*(不正确|不对|错误|有误|正确|成立|对)", evidence)
    if m:
        word = m.group(1)
        return (1, 0.75) if word in ("不正确", "不对", "错误", "有误") else (0, 0.7)

    # ③ "应改为/改为" 类纠错 → 原表述错
    if re.search(r"(?:应改为|应该改为|正确的说法是|正确表述是)", evidence):
        return 1, 0.7

    # ④ 绝对化措辞 + "过于绝对/不一定/有例外" → 错
    has_abs = any(w in (stem or "") for w in _ABSOLUTE_WORDS)
    if has_abs and re.search(r"(过于绝对|不一定|未必|有例外|太绝对)", evidence):
        return 1, 0.68

    # ⑤ 兜底：证据里否定语明显多于肯定语
    neg_n = sum(evidence.count(c) for c in _NEG_CUES)
    pos_n = sum(evidence.count(c) for c in _POS_CUES)
    if neg_n >= 2 and neg_n > pos_n * 1.6:
        return 1, 0.6
    if pos_n >= 2 and pos_n > neg_n * 1.6:
        return 0, 0.6
    return None, 0.0


def decide(stem, options, evidence, qtype="single"):
    """统一入口：返回 (答案, 置信度, 依据说明)。"""
    if qtype == "judge":
        idx, conf = decide_judge(evidence, stem)
        if idx is None:
            return None, 0.0, ""
        for i, o in enumerate(options or []):
            t = (o.get("text") or "").strip()
            if (idx == 0 and re.search(r"对|正确|√|是|true", t, re.I)) or \
               (idx == 1 and re.search(r"错|不正确|不对|×|否|false", t, re.I)):
                return (o.get("letter") or chr(65 + i)), conf, f"推理判定：{'对' if idx == 0 else '错'}"
        if options:
            o = options[min(idx, len(options) - 1)]
            return (o.get("letter") or chr(65 + min(idx, len(options) - 1))), conf, "推理判定（按顺序）"
        return None, 0.0, ""
    ans, conf = decide_choice(evidence, options, qtype)
    return (ans, conf, "推理选优" if ans else "")
# ---------------- 无证据兜底：禁止留空提交 ----------------
# 留空 = 必然 0 分，而且学习通会用"还有X道题未作答"拦住提交（6.4 本章自测就是这么失败的）。
# 所以穷尽搜索/大模型都拿不到答案时，按命题惯例给一个"最可能"的选择，并如实标注置信度。
_ABS_OPTS = ("所有", "一切", "任何", "一定", "总是", "全部", "唯一", "完全", "绝不", "必须")
_VAGUE_OPTS = ("以上都", "都不对", "都正确", "无法确定", "无关")
# 明显与正确选项对立的措辞（思政/职业素养类题目里几乎必是干扰项）
_NEG_OPTS = ("唯利是图", "损人利己", "自私", "违法", "犯罪", "腐败",
             "可以忽略", "不需要", "完全无关", "拒不", "任意妄为")


def _guess_score(stem, opt_text):
    """没有证据时，靠题型惯例给选项打分（分数不代表正确率，只用来排序）。"""
    t = (opt_text or "").strip()
    if not t:
        return -9.0
    s = 0.0
    sg, og = _bigrams(stem), _bigrams(t)
    if sg and og:                              # ① 与题干共享词越多越可能相关
        s += 1.2 * len(sg & og) / max(1, len(og))
    n = len(_clean(t))                          # ② 过长过短都不像标准答案
    if n <= 2:
        s -= 0.5
    elif 6 <= n <= 30:
        s += 0.35
    elif n > 45:
        s -= 0.2
    if any(w in t for w in _ABS_OPTS):          # ③ 绝对化措辞在选择题里多是干扰项
        s -= 0.6
    if any(w in t for w in _VAGUE_OPTS):        # ④ "以上都对"类兜底项风险高
        s -= 0.4
    if any(w in t for w in _NEG_OPTS):          # ⑤ 明显负面表述基本是干扰项
        s -= 1.2
    return round(s, 3)


def best_guess(stem, options, qtype="single"):
    """无任何证据时的兜底作答，保证不留空。

    返回 (答案, 置信度, 依据) 或 (None, 0, "")（仅当题型不支持或没有选项）。
    """
    opts = [o for o in (options or []) if (o.get("text") or "").strip()]
    if qtype == "judge":
        v, conf, why = judge_best_guess(stem)
        for o in opts:
            t = o.get("text") or ""
            hit = (v == 0 and re.search(r"对|正确|√|是", t)) or \
                  (v == 1 and re.search(r"错|不正确|不对|×|否", t))
            if hit:
                return (o.get("letter") or ("TR" if v == 0 else "FA")), conf, why
        return None, 0.0, ""

    if qtype not in ("single", "multi") or len(opts) < 2:
        return None, 0.0, ""

    scores = [(o, _guess_score(stem, o.get("text"))) for o in opts]
    scores.sort(key=lambda x: x[1], reverse=True)
    top, top_s = scores[0]
    gap = top_s - scores[1][1]

    if qtype == "single":
        conf = round(min(0.5, 0.28 + 0.06 * max(0.0, gap)), 2)
        letter = top.get("letter") or chr(65 + opts.index(top))
        return letter, conf, "搜不到答案，按题型惯例选「%s」" % (top.get("text") or "")[:10]

    avg = sum(s for _, s in scores) / len(scores)
    picked = [o for o, s in scores if s >= max(avg, 0) and s > -0.2]
    if len(picked) < 2:
        picked = [o for o, _ in scores[:2]]
    if len(picked) >= len(opts) and len(opts) > 2:
        picked = picked[:-1]                    # "全选"在题库里极少是正确答案
    picked.sort(key=lambda o: opts.index(o))
    letters = "".join(sorted((o.get("letter") or chr(65 + opts.index(o))) for o in picked))
    return letters, 0.3, "搜不到答案，多选兜底选 %s（未必正确）" % letters
