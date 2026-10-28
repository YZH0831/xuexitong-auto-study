# -*- coding: utf-8 -*-
"""章节测验：题目解析 -> 题库/大模型/联网作答 -> 提交 -> 结果回读沉淀。"""
import difflib
import re

from . import reasoning
import threading
import time

from .llm_answer import LLMAnswer
from .tiku import Tiku

# 题目容器选择器（按优先级）：注意不要放 .Zy_TItle（那是题目标题 div，
# 单独匹配会产出"没有选项的伪题目"并污染去重结果）
QUESTION_SELECTORS = [
    "div.TiMu",
    "div.newTiMu",
    "[class*='TiMu']",
    ".singleQuesId",
    ".duoTi",
    ".judgeTi",
    ".singleQuesDot",
    ".TiM",
    '[class*="question"]',
]

SUBMIT_SELECTORS = [
    'button:has-text("提交")',
    'a:has-text("提交")',
    "#submitBtn",
    ".submitBtn",
    'input[value*="提交"]',
    'button:has-text("交卷")',
]

RETRY_SELECTORS = [
    'button:has-text("重新作答")',
    'a:has-text("重新作答")',
    'text=再做一次',
    'button:has-text("再做一次")',
]

SCORE_PATTERNS = [
    re.compile(r"得分\s*[:：]?\s*(\d+(?:\.\d+)?)"),
    re.compile(r"成绩\s*[:：]?\s*(\d+(?:\.\d+)?)"),
    re.compile(r"(\d+(?:\.\d+)?)\s*分"),
]

JUDGE_WORDS = {"对", "错", "正确", "错误", "是", "否", "√", "×", "t", "f", "true", "false"}


def _wait(obj, ms):
    """兼容 Page 与 Frame 的等待（Frame 可能没有 wait_for_timeout）。"""
    try:
        obj.wait_for_timeout(ms)
    except Exception:  # noqa: BLE001
        time.sleep(ms / 1000.0)


def _containers(obj):
    """把 Page 或 Frame 归一化成可遍历的容器列表。"""
    try:
        frames = obj.frames
        return list(frames) or [obj]
    except Exception:  # noqa: BLE001
        return [obj]


class Question:
    def __init__(self, stem, qtype, options, element):
        self.stem = stem
        self.type = qtype          # single / multi / judge / text
        self.options = options     # [{"text":..., "el":...}]
        self.element = element

    def __repr__(self):
        return f"Question({self.type}, {self.stem[:24]}..., {len(self.options)} 选项)"


_PUNCT = "，。？！、；：“”‘’（）()[]【】.,?!/-—_·\\"


def _norm(text):
    """归一化：去空白/标点/不可见字符，便于题库匹配。"""
    if not text:
        return ""
    s = str(text).lower()
    s = re.sub(r"[\s\u2002\u200b\u3000\u00a0]+", "", s)
    s = re.sub(f"[{re.escape(_PUNCT)}]", "", s)
    return s


def _is_judge(options):
    if not options or len(options) > 2:
        return False
    words = {_norm(o["text"])[:2] for o in options}
    return all(w in JUDGE_WORDS for w in words if w) and len(words) >= 2


def _visible_text(el):
    try:
        return (el.inner_text() or "").strip().replace("\n", " ")
    except Exception:  # noqa: BLE001
        return ""


JS_PARSE_Q = r"""
(el) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const titleEl = el.querySelector('.Zy_TItle, .newZy_TItle, [class*="TItle"]');
  const stem = norm(titleEl ? titleEl.innerText : el.innerText);
  const lis = Array.from(el.querySelectorAll("li[qtype], ul.Zy_ulTop li, ul li"));
  const opts = [];
  lis.forEach((li, rowIndex) => {
    const numEl = li.querySelector('.num_option');
    const letter = norm(numEl ? (numEl.getAttribute('data') || numEl.innerText) : '').toUpperCase();
    const aEl = li.querySelector('a.after, .after, a');
    let text = norm(aEl ? aEl.innerText : '');
    if (!text) {
      text = norm(li.innerText);
      if (letter) text = text.replace(letter, '').trim();
    }
    if (text) opts.push({ letter: letter.slice(0, 2), text: text.slice(0, 200), rowIndex: rowIndex });
  });
  return { stem: stem, opts: opts };
}
"""


def _parse_question(el, log=None):
    """解析单题：题干 + 选项（含字母与正文）。

    学习通的选项是 li 行：`.num_option` 存字母、`a.after` 存正文。
    必须按行配对，否则只会拿到字母 A/B/C/D，选项正文会漏进题干、拖垮搜题准确率。
    结构化解析失败时退回 legacy 实现。
    """
    try:
        data = el.evaluate(JS_PARSE_Q) or {}
    except Exception:  # noqa: BLE001
        data = {}
    stem = data.get("stem") or ""
    opt_data = data.get("opts") or []
    rows = []
    try:
        rows = el.query_selector_all("li[qtype], ul.Zy_ulTop li, ul li")
    except Exception:  # noqa: BLE001
        rows = []

    options = []
    for i, o in enumerate(opt_data):
        if not o.get("text"):
            continue
        # 用 JS 给的 rowIndex 精确配对 DOM 行：opt_data 过滤过空文本行，
        # 而 rows 没有过滤，直接 rows[i] 会在有分隔行/空行时整体错位 → 点错选项。
        ridx = o.get("rowIndex")
        if isinstance(ridx, int) and 0 <= ridx < len(rows):
            opt_el = rows[ridx]
        else:
            opt_el = rows[i] if i < len(rows) else el
        options.append({"text": o["text"], "letter": (o.get("letter") or "").strip(),
                        "el": opt_el})

    if not stem and not options:
        return _parse_question_legacy(el, log)

    # 多选题的选项行（li.before-after-checkbox）常常没有字母元素，
    # 这里按 DOM 顺序补 A/B/C…，否则字母型答案无法匹配（会漏选/不选）
    if options and not any(o.get("letter") for o in options):
        for i, o in enumerate(options):
            o["letter"] = chr(65 + i)

    text = stem or _visible_text(el)
    try:
        radios = el.query_selector_all("input[type=radio]")
        checks = el.query_selector_all("input[type=checkbox]")
        texts = el.query_selector_all("textarea, input[type=text]")
    except Exception:  # noqa: BLE001
        radios = checks = texts = []

    if "判断" in text:
        qtype = "judge"
    elif "多选" in text or "多项" in text:
        qtype = "multi"
    elif "单选" in text or "单项" in text:
        qtype = "single"
    elif checks:
        qtype = "multi"
    elif radios:
        qtype = "single"
    elif texts and not options:
        qtype = "text"
    elif options:
        qtype = "single"
    else:
        qtype = "text"
    if _is_judge(options):
        qtype = "judge"

    stem_clean = re.sub(r"\s+", " ", text).strip()
    # 去掉题号与题型前缀（"1 "、"【单选题】"）：它们会污染搜题查询词，导致搜不到答案
    stem_clean = re.sub(r"^\s*\d+\s*[.、．]?\s*", "", stem_clean).strip()
    m_type = re.match(r"^\s*【([^】]{1,8})】\s*", stem_clean)
    if m_type:
        type_word = m_type.group(1)
        stem_clean = stem_clean[m_type.end():].strip()
        if "判断" in type_word:
            qtype = "judge"
        elif "多选" in type_word or "多项" in type_word:
            qtype = "multi"
        elif "单选" in type_word or "单项" in type_word:
            qtype = "single"
    if len(options) > 12 or len(stem_clean) > 900:
        return None
    if len(stem_clean) < 2 and not options:
        return None
    return Question(stem_clean, qtype, options, el)


def _parse_question_legacy(el, log=None):
    text = _visible_text(el)
    if not text:
        return None
    # 学习通的选项是「li 行」：内含字母（.num_option）与文本（a.after）。
    # 必须按行解析，否则只会拿到字母 A/B/C/D，真正文本会漏进题干、拖垮搜题质量。
    options = []
    seen = set()
    rows = []
    for rsel in ["li[qtype]", ".Zy_ulTop li", "li[class*='before-after']",
                 "[class*='num_option']"]:
        try:
            rows.extend(el.query_selector_all(rsel))
        except Exception:  # noqa: BLE001
            continue
    if not rows:
        try:
            rows = el.query_selector_all("li")
        except Exception:  # noqa: BLE001
            rows = []

    for row in rows:
        try:
            if not row.is_visible():
                continue
            # 若命中的是字母 span 本身，向上找它所在的 li 行
            if "num_option" in (row.get_attribute("class") or ""):
                parent = row.evaluate_handle("e => e.closest('li') || e").as_element()
                if parent is not None:
                    row = parent
            letter = ""
            letter_el = row.query_selector(".num_option")
            if letter_el is not None:
                letter = ((letter_el.get_attribute("data")
                           or _visible_text(letter_el)) or "").strip()[:2].upper()
            text_el = row.query_selector("a.after, .after, a")
            text = _visible_text(text_el) if text_el is not None else ""
            full = _visible_text(row)                       # 整行文本（含字母），用于剔除题干
            if not text:
                text = re.sub(rf"^{re.escape(letter)}\s*", "", full).strip() if letter else full
            if not text or len(text) > 200:
                continue
            key = (letter, text)
            if key in seen:
                continue
            seen.add(key)
            options.append({"text": text, "letter": letter, "full": full, "el": row})
        except Exception:  # noqa: BLE001
            continue
    try:
        radios = el.query_selector_all('input[type=radio]')
        checks = el.query_selector_all('input[type=checkbox]')
        texts = el.query_selector_all('textarea, input[type=text]')
    except Exception:  # noqa: BLE001
        radios = checks = texts = []

    if radios:
        qtype = "single"
    elif checks:
        qtype = "multi"
    elif texts and not options:
        qtype = "text"
    elif options:
        qtype = "single"
    else:
        qtype = "text"
    # 题干/选项类名优先判定题型（学习通的选项是自定义 div，没有 input 元素）
    opt_cls = " ".join((o["el"].get_attribute("class") or "") for o in options[:3]) if options else ""
    if "num_option_dx" in opt_cls or "多选" in text or "多项" in text:
        qtype = "multi"
    elif "判断" in text:
        qtype = "judge"
    elif "单选" in text or "单项" in text:
        qtype = "single"
    elif _is_judge(options):
        qtype = "judge"

    stem = text
    for o in options:
        # 用整行文本（含字母）剔除选项，避免残留选项正文
        if o.get("full"):
            stem = stem.replace(o["full"], " ")
        stem = stem.replace(o["text"], " ")
    stem = re.sub(r"\s+", " ", stem).strip()
    if len(stem) < 2:
        stem = text[:120]

    # 排除"整页容器"误判
    if len(options) > 12 or len(stem) > 800:
        return None
    return Question(stem, qtype, options, el)


def _dedupe(questions):
    out = []
    seen = set()
    for q in questions:
        key = _norm(q.stem)[:80]
        if not key or key in seen:
            continue
        # 若已有题目的题干完全包含这道题，视为容器重复，丢弃
        if any(key in s for s in seen) or any(s in key and len(s) > 30 for s in seen):
            continue
        seen.add(key)
        out.append(q)
    return out


def extract_questions(page, log):
    questions = []
    for sel in QUESTION_SELECTORS:
        try:
            elements = page.query_selector_all(sel)
        except Exception:  # noqa: BLE001
            continue
        for el in elements:
            q = _parse_question(el, log)
            if q is not None:
                questions.append(q)
    questions = _dedupe(questions)

    if not questions:
        # 兜底：按 radio 的 name 分组重建题目
        groups = {}
        try:
            radios = page.query_selector_all('input[type=radio]')
        except Exception:  # noqa: BLE001
            radios = []
        for r in radios:
            try:
                name = r.get_attribute("name") or "group"
            except Exception:  # noqa: BLE001
                name = "group"
            groups.setdefault(name, []).append(r)
        for name, rs in groups.items():
            container = _nearest_container(rs[0], len(rs))
            if container is None:
                continue
            q = _parse_question(container, log)
            if q is not None:
                questions.append(q)
        questions = _dedupe(questions)

    log.info(f"  解析到 {len(questions)} 道题")
    for q in questions:
        log.debug(f"    [{q.type}] {q.stem[:40]} ({len(q.options)} 选项)")
    return questions


def _nearest_container(el, n_inputs, max_depth=6):
    cur = el
    for _ in range(max_depth):
        try:
            parent = cur.query_selector("xpath=..")
        except Exception:  # noqa: BLE001
            return None
        if parent is None:
            return None
        try:
            cnt = len(parent.query_selector_all("input"))
            if cnt >= n_inputs and len(_visible_text(parent)) > 8:
                return parent
        except Exception:  # noqa: BLE001
            return None
        cur = parent
    return None


# ---------------- 作答 ----------------
# 字母型答案：A / AB / A、B / A,B / A和B / A与B（整体必须是字母列表）
_LETTERS_ONLY_RE = re.compile(r"^[A-Za-z][A-Za-z、,，;；/和与\s]*$")
_ANSWER_PART_SPLIT_RE = re.compile(r"[、,，;；/]+|\s+[和与]\s*|\s{2,}|\s+")


def _parse_letters(answer):
    """仅当答案整体就是"字母列表"时才按字母解析。

    旧实现是"抽出所有 A-Z"，会把"pH值""Cooling"这类文本答案里的字母当成选项字母，
    导致选错或漏选。
    """
    raw = (answer or "").strip()
    if not raw or len(raw) > 24:
        return []
    if not _LETTERS_ONLY_RE.match(raw):
        return []
    letters = re.findall(r"[A-Za-z]", raw.upper())
    return letters if letters and len(letters) <= 8 else []


def split_answer_parts(answer):
    """把答案拆成多个片段（多选常见写法："甲、乙、丁" / "A,B" / "火灾性事故 爆炸性事故"）。

    护栏：纯英文长句不拆（避免把 "cut off power supply" 拆成单词去和选项做包含匹配）。
    """
    raw = str(answer or "")
    if len(raw) > 16 and re.search(r"[A-Za-z]{4,}", raw) and not re.search(r"[、,，;；]", raw):
        return [raw.strip()] if raw.strip() else []
    parts = [p.strip() for p in _ANSWER_PART_SPLIT_RE.split(raw)]
    return [p for p in parts if p]


JS_READ_RECORDED = r"""
(el) => {
  const li = el.querySelector('li[qid]');
  if (!li) return null;
  const qid = li.getAttribute('qid');
  const inp = document.querySelector('input[name="answer' + qid + '"]')
           || document.querySelector('#answer' + qid);
  return inp ? String(inp.value || '') : null;
}
"""


def _read_recorded_answer(el):
    """读取平台隐藏域中已记录的答案（如 answer123=ABD）。"""
    try:
        return el.evaluate(JS_READ_RECORDED)
    except Exception:  # noqa: BLE001
        return None


def _click_option(option, log):
    el = option["el"]
    try:
        if el.is_visible():
            el.click(timeout=3000)
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        inp = el.query_selector('input[type=radio], input[type=checkbox]')
        if inp is not None:
            inp.check(force=True, timeout=3000)
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        el.click(force=True, timeout=3000)
        return True
    except Exception:  # noqa: BLE001
        return False


# 词组类：用"包含"匹配（"不正确"必须优先于"正确"判断）
JUDGE_FALSE_PHRASES = ("不正确", "不对", "不符", "错误", "错", "×", "否", "非", "false", "no")
JUDGE_TRUE_PHRASES = ("正确", "对", "√", "是", "true", "yes")
# 单字符类：必须"完全相等"才认（否则任意含 b/f/1 的文本都会被误判）
JUDGE_FALSE_EXACT = {"f", "n", "0", "b", "x"}
JUDGE_TRUE_EXACT = {"t", "y", "1", "a"}


def judge_index(text):
    """判断题答案归一化 → 0=对 / 1=错 / None。

    规则：① 先判否定词组（"不正确/不对"不能当成"对"）
          ② 再判肯定词组
          ③ 单字母/数字必须整体相等（避免 a-b-c 之类的误命中）
    """
    t = _norm(text)
    if not t:
        return None
    if any(k in t for k in JUDGE_FALSE_PHRASES):
        return 1
    if any(k in t for k in JUDGE_TRUE_PHRASES):
        return 0
    if t in JUDGE_FALSE_EXACT:
        return 1
    if t in JUDGE_TRUE_EXACT:
        return 0
    return None


def fill_answer(q, answer, log):
    if not answer:
        return False
    # 判断题专处理：答案是"对/错/正确/√/T/F"等，需要映射到具体选项
    if q.type == "judge":
        # ① 答案本身可能就是我们选项的字母（如 TR/FA、A/B）→ 精确字母匹配最可靠
        #    （学习通判断题的选项字母常是 TR/FA = True/False，judge_index 不认识）
        la = (str(answer) or "").strip().upper()
        if la:
            hit = next((o for o in q.options if (o.get("letter") or "").upper() == la), None)
            if hit is not None and _click_option(hit, log):
                log.info(f"  判断题作答：{la} → 选中「{hit['text'][:12]}」")
                return True
            # ② 位置兜底：源字母是 A/B 而选项为 对/错 时按常规顺序对应（对=第1个）
            if la in ("A", "B") and len(q.options) >= 2:
                tgt = q.options[0] if la == "A" else q.options[1]
                if _click_option(tgt, log):
                    log.info(f"  判断题作答：{la} → 按顺序选中「{tgt['text'][:12]}」")
                    return True
        # ③ 归一化"对/错"再匹配
        idx = judge_index(answer)
        if idx is not None:
            target = next((o for o in q.options if judge_index(o["text"]) == idx), None)
            if target is None and 0 <= idx < len(q.options):
                target = q.options[idx]          # 选项文本是 A/B 等字母时按顺序对应
            if target is not None and _click_option(target, log):
                log.info(f"  判断题作答：{str(answer)[:8]} → 选中「{target['text'][:12]}」")
                return True
        # 归一化失败则退回通用文本匹配
    if q.type == "text":
        try:
            box = q.element.query_selector("textarea, input[type=text]")
            if box is not None:
                box.fill(str(answer))
                return True
        except Exception:  # noqa: BLE001
            return False
        return False

    targets = []
    letters = _parse_letters(answer)
    # 护栏：字母必须落在本题选项范围内，且每个字母都对应一个真实选项；
    # 否则视为"文本答案里恰好含字母"（如 Cooling → C），不按字母解析
    if letters:
        n_opt = len(q.options)
        if any(ord(c) - 64 > n_opt for c in letters):
            letters = []
        elif q.options and not all(
            any((o.get("letter") or "").startswith(c) for o in q.options)
            for c in letters
        ):
            letters = []
    if letters:
        for letter in letters:
            # 优先按选项自带字母匹配（更可靠），再退回按顺序取第 N 个
            t = next((o for o in q.options if o.get("letter") == letter), None)
            if t is None:
                idx = ord(letter) - 65
                if 0 <= idx < len(q.options):
                    t = q.options[idx]
            if t is not None and all(t is not x for x in targets):
                targets.append(t)
    if not targets:
        # 文本型答案：按"片段"逐个匹配选项（多选答案是多个片段时，必须全部命中，否则会漏选）
        parts = split_answer_parts(answer)
        norm_ans = _norm(answer)
        for o in q.options:
            norm_opt = _norm(o["text"])
            if not norm_opt:
                continue
            hit = False
            for p in parts:
                np_ = _norm(p)
                if not np_:
                    continue
                if np_ == norm_opt or np_ in norm_opt or (len(norm_opt) >= 4 and norm_opt in np_):
                    hit = True
                    break
            if not hit and norm_ans and (norm_ans in norm_opt or (len(norm_opt) >= 6 and norm_opt in norm_ans)):
                hit = True
            if hit:
                targets.append(o)
    # 单选/判断：文本匹配命中多个选项时，只保留**最贴合的那个**（否则点多个等于乱选）
    if q.type in ("single", "judge") and len(targets) > 1:
        norm_ans = _norm(str(answer))
        best, best_len = None, -1
        for t in targets:
            no = _norm(t.get("text") or "")
            if no and no in norm_ans and len(no) > best_len:
                best, best_len = t, len(no)
        if best is None:
            best = targets[0]           # 无法区分就取第一个（与页面顺序一致）
        log.warning(f"  ⚠ 单选题匹配到 {len(targets)} 个选项 → 只取最贴合的「{(best.get('text') or '')[:14]}」")
        targets = [best]
    if not targets and len(q.options) == 1:
        targets = q.options
    if not targets:
        return False
    # 多选题：核对"应选数量"与"实际命中数量"，漏选时告警（多选要求选全才得分）
    if q.type == "multi":
        want = len(letters) if letters else len(split_answer_parts(answer))
        if want >= 2 and len(targets) < want:
            log.warning(f"  ⚠ 多选题可能漏选：答案要求 {want} 项，仅匹配到 {len(targets)} 项"
                        f"（答案：{str(answer)[:30]}）")
        if len(targets) <= 1 and len(q.options) > 1:
            log.warning(f"  ⚠ 多选题只选中 1 项（答案：{str(answer)[:40]}）；"
                        f"若本题确为多选，请人工核对")
    ok = False
    for t in targets:
        ok = _click_option(t, log) or ok
    if ok and q.type == "multi":
        log.info(f"  多选题已选 {len(targets)} 项：{''.join(o.get('letter') or '?' for o in targets)}")

    # 选后自检：读平台隐藏域里"实际记录"的答案，发现漏选立即补选
    if ok and q.type in ("multi", "single", "judge"):
        want = set(letters) if letters else {o.get("letter") for o in targets if o.get("letter")}
        recorded = _read_recorded_answer(q.element)
        if recorded is not None and want:
            got = set(re.findall(r"[A-Za-z]", str(recorded).upper()))
            missing = {c for c in want - got if c.isalpha()}
            if missing:
                log.warning(f"  ⚠ 平台仅记录 {''.join(sorted(got)) or '空'}，缺少 "
                            f"{''.join(sorted(missing))} → 自动补选")
                for letter in sorted(missing):
                    t = next((o for o in q.options if o.get("letter") == letter), None)
                    if t is not None and _click_option(t, log):
                        log.info(f"     已补选 {letter}")
                log.info(f"  补选后平台记录：{_read_recorded_answer(q.element)}")
    return ok


# ---------------- 提交 / 结果 ----------------
# 提交后的"二次确认"弹窗按钮（可能出现在答题帧，也可能在父页面）
CONFIRM_SELECTORS = [
    ".layui-layer-btn0", "#popok", "#popokBtn", ".popbtn a", ".popbtn .ok",
    'button:has-text("确定")', 'a:has-text("确定")',
    'button:has-text("确认")', 'a:has-text("确认")',
    'button:has-text("是")', 'a:has-text("是")',
    '[class*="confirm"] button', '[class*="dialog"] button',
]


def _search_roots(page):
    roots = [page]
    try:
        parent = getattr(page, "page", None)
        if parent is not None and parent is not page:
            roots.append(parent)
    except Exception:  # noqa: BLE001
        pass
    return roots


def _submit_button(page):
    for sel in SUBMIT_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el is not None and el.is_visible():
                return el
        except Exception:  # noqa: BLE001
            continue
    return None


def _confirm_dialog(page, log):
    """点掉"确定提交吗？"类二次确认框（答完题后提交的必经步骤）。"""
    for root in _search_roots(page):
        for sel in CONFIRM_SELECTORS:
            try:
                el = root.query_selector(sel)
            except Exception:  # noqa: BLE001
                continue
            if el is None:
                continue
            try:
                if not el.is_visible():
                    continue
                el.click(timeout=3000)
                log.info(f"  已点击提交确认按钮（{sel}）")
                return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _unanswered_hint(page):
    """读取页面上"还有X道题未作答"之类的拦提交提示。"""
    for frame in _containers(page):
        try:
            text = frame.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except Exception:  # noqa: BLE001
            continue
        m = re.search(r"还有\s*\d+\s*(?:道|个)?(?:题|小题)?\s*未(?:作答|完成|答)|未作答|尚未作答|请先完成", text)
        if m:
            return m.group(0)[:40]
    return ""


def submit_quiz(page, log, humanizer=None):
    """提交测验：点提交 → 处理二次确认 → 复核是否真的提交成功。

    注意：学习通提交后会弹二次确认框，而弹窗看门狗出于安全默认不点"提交/交卷"类按钮，
    因此这里必须显式确认，否则会"看起来点了提交但实际没提交"。
    """
    btn = _submit_button(page)
    if btn is None:
        log.warning("  未找到提交按钮（可能已经提交过）")
        return False
    try:
        if humanizer is not None:
            humanizer.click(getattr(page, "page", page), btn, timeout=5000)
        else:
            btn.click(timeout=3000)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"  点击提交按钮失败: {str(exc)[:60]}")
        return False
    _wait(page, 2000)

    for attempt in range(1, 4):
        if _confirm_dialog(page, log):
            _wait(page, 3000)
        if _submit_button(page) is None:
            log.info("  提交成功（提交按钮已消失）")
            return True
        hint = _unanswered_hint(page)
        if hint:
            log.warning(f"  平台拦截提交：{hint} → 还有空题，必须补答后再交")
        log.info(f"  第 {attempt} 次提交后仍检测到提交按钮 → 重试")
        again = _submit_button(page)
        if again is not None:
            try:
                again.click(timeout=3000)
            except Exception:  # noqa: BLE001
                pass
        _wait(page, 2500)
    log.warning("  提交后仍存在提交按钮 → 可能未提交成功，将在复核阶段确认")
    return False


def read_result(page, questions, bank, log):
    text = ""
    for frame in _containers(page):
        try:
            text += frame.evaluate("() => (document.body ? document.body.innerText : '')") or ""
        except Exception:  # noqa: BLE001
            continue
    score = None
    for pat in SCORE_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                score = float(m.group(1))
                break
            except ValueError:
                continue
    # 回读正确答案沉淀题库
    recorded = 0
    for q in questions:
        try:
            rights = q.element.query_selector_all('[class*="right"], [class*="correct"], [class*="true"], .answer')
        except Exception:  # noqa: BLE001
            continue
        values = []
        for r in rights:
            t = _visible_text(r)
            if t and len(t) < 100:
                values.append(t)
        if values:
            bank.record(q.stem, "|".join(values[:6]), q.type)
            recorded += 1
    if recorded:
        log.info(f"  已从结果页沉淀 {recorded} 道题到题库")
    return score


def wait_manual_answer(log, timeout_s, questions):
    """终端等人工在浏览器里作答；回车可提前继续。"""
    log.warning("-" * 56)
    if questions:
        log.warning(f"题库未命中 {len(questions)} 道题，请在浏览器窗口中手工作答：")
        for i, q in enumerate(questions, 1):
            print(f"  {i}. [{q.type}] {q.stem[:70]}")
    else:
        log.warning("本章测验未能自动解析题目，请在浏览器窗口中手动完成；")
    log.warning(f"程序最长等待 {timeout_s} 秒；答完在控制台回车即可立即提交")
    log.warning("-" * 56)
    event = threading.Event()

    def _reader():
        try:
            input(">>> 答完请回车继续：")
            event.set()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_reader, daemon=True).start()
    event.wait(timeout_s)


def decode_font_texts(page, questions, cfg, log):
    """若答题页使用了学习通加密字体（cxSecretStyle），还原题面与选项文本。

    解码后题干才是「人能读懂、题库/联网搜索能匹配」的真实文本。
    """
    try:
        if not bool(cfg.quiz.decode_font):
            return False
        try:
            html = page.evaluate("() => (document.documentElement ? document.documentElement.outerHTML : '')") or ""
        except Exception:  # noqa: BLE001
            return False
        from .font_decoder import FontDecoder
        if not FontDecoder.detect(html):
            return False
        decoder = FontDecoder.from_html(html, cfg.quiz.font_table, log)
        if decoder is None:
            return False
        changed = 0
        for q in questions:
            new_stem = decoder.decode(q.stem)
            if new_stem and new_stem != q.stem:
                changed += 1
                q.stem = new_stem
            for o in q.options or []:
                o["text"] = decoder.decode(o.get("text") or "")
        log.info(f"  已通过加密字体映射还原题面（{changed}/{len(questions)} 道题文本已还原）")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(f"字体解码失败（将按原文本处理）: {str(exc)[:90]}")
        return False


def _tiku_text_to_letters(text, options, qtype="single"):
    """把题库返回的"文本答案"对齐成本页选项字母（选项顺序不同也不会错）。

    多选可能返回 "爱岗敬业#诚实守信" / "爱岗敬业、诚实守信"，逐个片段对齐后合并。
    """
    from .online_answer import match_option_by_text
    txt = str(text or "").strip()
    if not txt:
        return None
    if qtype == "judge":
        idx = judge_index(txt)
        if idx is None:
            return match_option_by_text(txt, options)
        for o in options or []:
            t = (o.get("text") or "")
            if (idx == 0 and re.search(r"对|正确|√|是", t)) or \
               (idx == 1 and re.search(r"错|不正确|不对|×|否", t)):
                return o.get("letter") or ("TR" if idx == 0 else "FA")
        return None

    if qtype != "multi":
        return match_option_by_text(txt, options)

    parts = [x for x in re.split(r"[#＃、；;，,\s]+", txt) if x.strip()]
    letters = []
    for part in parts:
        got = match_option_by_text(part, options)
        if got and got not in letters:
            letters.append(got)
    if len(letters) >= 2:
        return "".join(sorted(letters))
    return match_option_by_text(txt, options)


def run_quiz(session, cfg, log, page, popup, bank, online=None, humanizer=None):
    """page 可以是 Page，也可以是已经打开答题页的 Frame。

    答题顺序：本地题库 → 联网搜题（可配置）→ 人工作答（on_unknown=pause）。
    """
    _wait(page, 2500)
    try:
        popup.tick(page)
    except Exception:  # noqa: BLE001
        pass
    questions = extract_questions(page, log)
    if questions:
        # 先尝试用加密字体映射还原题面（还原后题库/联网搜索才有意义）
        decode_font_texts(page, questions, cfg, log)
        try:
            from .online_answer import looks_obfuscated
            if looks_obfuscated(" ".join(q.stem for q in questions[:3])):
                log.warning("题面仍疑似加密字体混淆（未能还原，建议人工作答）")
        except Exception:  # noqa: BLE001
            pass
    if not questions:
        if cfg.quiz.on_unknown != "pause":
            return "manual", "未解析到题目，请人工完成本章测验"
        # 平台对题面做字体混淆（实测：环境保护法 → 环糵保护孾），无法可靠自动作答
        log.warning("未自动解析到题目（本章测验可能采用字体混淆或自定义组件）")
        log.warning("→ 请在浏览器窗口里手动完成本章测验，完成后在控制台回车，程序会自动提交")
        wait_manual_answer(log, int(cfg.quiz.pause_timeout_s), [])
        if not submit_quiz(page, log, humanizer):
            return "manual", "未找到提交按钮，请人工提交"
        score = read_result(page, [], bank, log)
        note = "已提交（人工作答）"
        if score is not None:
            note += f"，得分 {score}"
        return "done", note

    unknown = []
    conflicts = []
    local_hits = 0
    online_hits = 0
    guess_hits = 0
    tiku_hits = 0
    llm_hits = 0
    llm_solver = LLMAnswer(cfg, log)
    if llm_solver.enabled:
        ok, why = llm_solver.available()
        log.info(f"  大模型答题：{'已启用（' + llm_solver.model + '）' if ok else '跳过 — ' + why}")
    tiku = Tiku(cfg, log)
    if tiku.enabled:
        ok, why = tiku.available()
        log.info(f"  第三方题库：{'已启用（' + tiku.describe() + '）' if ok else '跳过 — ' + why}")
    for q in questions:
        # 1) 本地题库优先（未验证的联网答案会标注出来）
        hit = bank.lookup(q.stem)
        answer = hit["answer"] if isinstance(hit, dict) else hit
        source = "本地题库" if (isinstance(hit, dict) and hit.get("verified")) else "本地题库(未验证)"
        # 2) 题库未命中 → 大模型（若启用）→ 联网搜题
        rec_source = "online"
        # 1b) 本地"未验证"答案只当候选：若联网能给出**双源一致**的不同答案，以联网为准。
        #     旧实现只要题库命中就跳过搜索，历史上的联网错答案被"本地题库"永久锁死
        #     （实测：金属梯子 A 是错的，重启后被当成已验证答案直接使用）。
        if (answer is not None and isinstance(hit, dict) and not hit.get("verified")
                and online is not None
                and bool(cfg.quiz.online_search.get("reverify_unverified", True))):
            try:
                fresh = None
                if (bool(cfg.quiz.online_search.get("cross_check", True))
                        and hasattr(online, "search_cross")):
                    cres = online.search_cross(session.context, q.stem, q.options, q.type)
                    if cres.get("agree"):
                        fresh = cres.get("answer")
                if fresh is not None and _norm(str(fresh)) != _norm(str(answer)):
                    log.warning(f"  本地未验证答案 {str(answer)[:18]} 与联网双源 {str(fresh)[:18]} "
                                f"不一致 → 采信联网")
                    answer = fresh
                    rec_source = "cross_checked"
                    source = "双源核对(覆盖未验证题库)"
            except Exception as exc:  # noqa: BLE001
                log.debug(f"  未验证答案复核失败（继续用本地候选）: {str(exc)[:60]}")
        # 1.5) 第三方题库：结构化答案，比网页搜索准且快（命中即止）
        if answer is None and tiku.enabled:
            ta, tkind, tprov, tnote = tiku.query(q.stem, q.options, q.type)
            if ta:
                mapped = ta if tkind == "letter" else _tiku_text_to_letters(ta, q.options, q.type)
                if mapped:
                    answer, rec_source, source = mapped, "tiku", f"题库({tprov})"
                    log.info(f"  题库作答（{tprov}）：{q.stem[:24]}… → {mapped}"
                             + ("" if tkind == "letter" else f"（原文 {ta[:16]}）"))
                else:
                    log.warning(f"  题库({tprov}) 返回「{ta[:16]}」但无法对齐本页选项 → 丢弃")

        if answer is None and llm_solver.enabled:
            la, conf, reason = llm_solver.solve(q.stem, q.options, q.type)
            min_conf = float(cfg.quiz.get("llm", {}).get("min_confidence", 0.55) or 0)
            if la and conf < min_conf:
                log.warning(f"  大模型置信度 {conf:.2f} < 门槛 {min_conf:.2f} → 放弃该题（宁缺勿错）")
                la = None
            if la:
                answer = la
                rec_source = "llm"
                source = f"大模型(置信{conf:.1f})"
                log.info(f"  大模型作答：{q.stem[:26]}… → {la}"
                         + (f"（{reason}）" if reason else ""))
                if cfg.quiz.get("llm", {}).get("cross_check_with_search") and online is not None:
                    cres = online.search_cross(session.context, q.stem, q.options, q.type)
                    sa = cres.get("answer")
                    if sa:
                        same = str(sa).replace("、", "")[:8].upper() == str(la)[:8].upper()
                        log.info(f"  搜索交叉验证：{sa} → {'一致 ✔' if same else '不一致 ⚠'}")
                        if not same:
                            conflicts.append(f"{q.stem[:20]}：大模型={la} / 搜索={sa}（采信大模型）")
        if answer is None and online is not None:
            use_cross = (bool(cfg.quiz.online_search.get("cross_check", True))
                         and hasattr(online, "search_cross"))
            if use_cross:
                cres = online.search_cross(session.context, q.stem, q.options, q.type)
                answer = cres.get("answer")
                rec_source = cres.get("source") or "online"
                if cres.get("agree"):
                    source = "双源一致"
                elif rec_source == "online_conflict":
                    source = "冲突采信"
                    conflicts.append(q.stem[:22] + "：" + str(cres.get("note", ""))[:70])
                elif answer:
                    source = "单源(未验证)"
            else:
                answer = online.search(session.context, q.stem, q.options, q.type)
                source = "联网搜索"
        # ★ 多选题完整性：没答案或只选到 1 项（漏选=部分分/0 分）→ 补搜完整答案列表
        if q.type == "multi" and online is not None and (
                answer is None or len(re.sub(r"[^A-Za-z]", "", str(answer))) < 2):
            try:
                full_ans, ev = online.search_multi_full(session.context, q.stem, q.options)
                if full_ans and (answer is None or set(full_ans) >= set(re.sub(r"[^A-Za-z]", "", str(answer)))):
                    answer, rec_source = full_ans, "online"
                    source = "多选补全(逐选项核对)"
                    log.info(f"  ☑ 多选补全：{q.stem[:22]}… → {full_ans}"
                             + (f"｜依据: {ev}" if ev else ""))
            except Exception as exc:  # noqa: BLE001
                log.debug(f"多选补全失败: {str(exc)[:60]}")

        # ★ 判断题兜底：穷尽搜索（含多路变体）后仍无答案 → 按题库惯例给出最可能判断，
        #   明确标注置信度，禁止留空提交（留空与答错同样 0 分，但放弃 = 必错）
        if answer is None and q.type == "judge":
            v, conf, why = reasoning.judge_best_guess(q.stem)
            for i, o in enumerate(q.options):
                t = (o.get("text") or "")
                hit = (v == 0 and re.search(r"对|正确|√|是", t)) or \
                      (v == 1 and re.search(r"错|不正确|不对|×|否", t))
                if hit:
                    answer = o.get("letter") or ("TR" if v == 0 else "FA")
                    source = f"推理判断(置信{conf:.2f})"
                    rec_source = "reasoning"
                    log.info(f"  ⚖ 判断题兜底：{q.stem[:22]}… → {'对' if v == 0 else '错'}"
                             f"（置信 {conf:.2f}｜{why}）")
                    break

        if answer is None:
            # ★ 最后兜底：留空=必错，而且平台会用"还有X道题未作答"拦住提交（6.4 就是这么失败的）
            g, gconf, gwhy = reasoning.best_guess(q.stem, q.options, q.type)
            if g:
                answer, source, rec_source = g, f"兜底推理(置信{gconf:.2f})", "reasoning"
                log.info(f"  ⚑ 兜底作答（{q.type}）：{q.stem[:22]}… → {g}｜{gwhy}")
            else:
                unknown.append(q)
                log.warning(f"  ✘ 无法作答（题型 {q.type}，无选项可猜）：{q.stem[:26]}…")
                continue
        if fill_answer(q, answer, log):
            if source.startswith("本地题库"):
                local_hits += 1
            elif rec_source == "tiku":
                tiku_hits += 1
            elif rec_source == "reasoning" or source.startswith(("兜底推理", "推理判断")):
                guess_hits += 1
            elif source.startswith("大模型"):
                llm_hits += 1
                bank.record(q.stem, answer, q.type, source="llm")
            else:
                online_hits += 1
                # 记录来源：双源一致=已核对 / 冲突采信 / 单源未验证（结果页回读可覆盖）
                bank.record(q.stem, answer, q.type, source=rec_source)
            log.info(f"  已作答（{source}）: {q.stem[:24]} → {str(answer)[:20]}")
        else:
            unknown.append(q)
    log.info(f"  作答统计：本地题库 {local_hits} 题 / 第三方题库 {tiku_hits} 题 / "
             f"大模型 {llm_hits} 题 / 联网搜索 {online_hits} 题"
             f" / 兜底推理 {guess_hits} 题 / 待人工 {len(unknown)} 题")
    if conflicts:
        log.warning(f"  双源冲突 {len(conflicts)} 题（已按策略采信，详见上方日志）：")
        for c in conflicts[:4]:
            log.warning(f"     - {c}")

    if unknown:
        mode = cfg.quiz.on_unknown
        if mode == "pause":
            wait_manual_answer(log, int(cfg.quiz.pause_timeout_s), unknown)
        elif mode == "skip":
            return "manual", f"{len(unknown)} 题未作答，按配置跳过提交"

    submitted = submit_quiz(page, log, humanizer)
    if not submitted:
        return "failed", "未找到提交按钮，需人工提交"
    score = read_result(page, questions, bank, log)
    note = "已提交"
    if score is not None:
        note += f"，得分 {score}"
    if conflicts:
        note += f"；双源冲突 {len(conflicts)} 题（已按策略采信）：" + "；".join(conflicts[:2])
    if unknown:
        note += f"，其中 {len(unknown)} 题由人工完成"
    return "done", note
