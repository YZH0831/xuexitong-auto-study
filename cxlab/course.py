# -*- coding: utf-8 -*-
"""课程定位、章节树与任务点解析、平台状态核验。"""
import re
import threading
import time
import urllib.parse
from datetime import date
from pathlib import Path

HOME_URL = "https://i.chaoxing.com/"


STATUS_LABELS = {
    "active": "可处理",
    "ended": "已结束",
    "dropped": "已退课",
    "not_started": "未开课",
    "unknown": "状态未知",
}

# 开课时间：2026-09-01～2026-12-31（平台混用全角～与半角~）
DATE_RANGE_RE = re.compile(
    r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*[~～\-—–至到]{1,2}\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"
)
DEADLINE_RES = [
    re.compile(r"(?:截止日期|截止时间|结束时间|结课时间|截止)[：:]\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"),
    re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*(?:截止|结束)"),
]
# 「已结束/已退课」列表入口（在课程列表 iframe 内）
ENDED_LIST_SELECTOR = "#endCourseListHref"
DROP_LIST_SELECTOR = "#dropCourseListHref"
JS_SUB_LIST_CARDS = r"""
() => {
  const out = [];
  const seen = new Set();
  document.querySelectorAll('li.course, [courseid]').forEach((el) => {
    const nameEl = el.querySelector('.course-name');
    const t = ((nameEl ? nameEl.innerText : el.innerText) || '').replace(/\s+/g, ' ').trim();
    // 已结束/已退课列表的卡片常没有 courseid 属性，ID 只在链接里
    let cid = el.getAttribute('courseid') || '';
    const link = el.querySelector('a[href*="courseid"]') || (el.tagName === 'A' ? el : null);
    const href = link ? (link.getAttribute('href') || '') : '';
    if (!cid) {
      const m = /courseid=(\d+)/i.exec(href);
      if (m) cid = m[1];
    }
    if (!cid || seen.has(cid)) return;
    seen.add(cid);
    out.push({ courseid: cid, text: t.slice(0, 140) });
  });
  return out;
}
"""


SINGLE_DATE_RE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")


def _mk_date(y, m, d):
    try:
        return date(int(y), int(m), int(d))
    except Exception:  # noqa: BLE001
        return None


def _parse_single_date(text):
    """解析纯日期串（用于 deadline_overrides），如 2026-10-01 / 2026/10/1。"""
    m = SINGLE_DATE_RE.search(text or "")
    return _mk_date(*m.groups()) if m else None


def parse_course_dates(text):
    """从课程卡片文本解析 (开课日期, 结束日期)；解析不到为 None。"""
    text = (text or "").replace(" ", "")
    start = end = None
    m = DATE_RANGE_RE.search(text)
    if m:
        start = _mk_date(*m.groups()[:3])
        end = _mk_date(*m.groups()[3:])
    else:
        for pat in DEADLINE_RES:
            m = pat.search(text)
            if m:
                end = _mk_date(*m.groups())
                break
    return start, end


def classify_course(card, cfg, excluded=None, today=None):
    """判定单门课程状态 → (status, reason)。

    优先级：① 平台「已结束/已退课」标记 ② 配置手工覆盖 ③ 卡片日期范围
    ④ 无法判定 → unknown（按 unknown_status_policy 处理）
    """
    today = today or date.today()
    cid = str(card.get("course_id") or "")

    # ★ 手工"强制处理"优先级最高：即便平台把课标成已结束，用户明确要求做就做
    overrides = cfg.course.get("deadline_overrides") or {}
    for key, spec in overrides.items():
        if not match_course(card, key):
            continue
        spec_s = str(spec).strip()
        if spec_s.lower() in ("none", "ignore", "always", "force", "忽略", "强制"):
            return "active", "手工指定：始终处理（已覆盖平台标记）"
        break

    if excluded and cid in excluded:
        tag = excluded[cid]
        return ("dropped" if "退课" in tag else "ended"), f"平台标记：{tag}"

    for key, spec in overrides.items():
        if not match_course(card, key):
            continue
        spec_s = str(spec).strip()
        d = _parse_single_date(spec_s) or parse_course_dates(spec_s)[1]
        if d:
            return ("ended" if d < today else "active"), f"手工截止日期 {d.isoformat()}"
        return "active", f"手工指定：{spec_s}"

    start, end = parse_course_dates(card.get("text") or "")
    if start and start > today:
        return "not_started", f"{start.isoformat()} 才开课"
    if end:
        if end < today:
            if bool(cfg.course.get("skip_past_deadline", True)):
                return "ended", f"结束时间 {end.isoformat()} 已过（逾期 {(today - end).days} 天）"
            return "active", f"结束时间已过，但按配置仍处理（{end.isoformat()}）"
        return "active", f"开课中（{start.isoformat() if start else '?'} ~ {end.isoformat()}）"
    return "unknown", "卡片无日期信息"


JS_IFRAME_INFO = r"""
() => {
  const fr = document.querySelector('#frame_content') || document.querySelector('iframe[src*="visit"]');
  return fr ? { src: fr.src || '', id: fr.id || '' } : null;
}
"""
JS_SET_IFRAME_SRC = r"""
(u) => {
  const fr = document.querySelector('#frame_content') || document.querySelector('iframe[src*="visit"]');
  if (!fr) return false;
  fr.src = u;
  return true;
}
"""


def _open_sub_list(session, cfg, log, selector, url_key, label, url_path=""):
    """打开课程列表里的「已结束/已退课」子列表并取回卡片。

    主路径：直接把课程 iframe 的 src 指到子列表地址（确定性高，不依赖页面 JS 初始化）；
    兜底：点击列表页上的入口链接。
    """
    page = session.page
    try:
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=int(cfg.login.nav_timeout_ms))
    except Exception as exc:  # noqa: BLE001
        log.debug(f"打开学习空间失败: {str(exc)[:60]}")

    # 等 iframe 就绪
    info = None
    for _ in range(10):
        try:
            info = page.evaluate(JS_IFRAME_INFO)
        except Exception:  # noqa: BLE001
            info = None
        if info and info.get("src"):
            break
        page.wait_for_timeout(1000)

    navigated = False
    if info and info.get("src") and url_path:
        try:
            parts = urllib.parse.urlsplit(info["src"])
            full = f"{parts.scheme}://{parts.netloc}{url_path}"
            page.evaluate(JS_SET_IFRAME_SRC, full)
            navigated = True
            log.debug(f"{label}: iframe → {full}")
        except Exception as exc:  # noqa: BLE001
            log.debug(f"{label}: 切换 iframe 失败 {str(exc)[:60]}")

    if not navigated:
        # 兜底：找入口链接点击
        frame = None
        for f in all_frames(page):
            if "visit/" in (f.url or ""):
                frame = f
                break
        if frame is None:
            return []
        try:
            el = None
            for _ in range(8):
                el = frame.query_selector(selector)
                if el is not None and el.is_visible():
                    break
                page.wait_for_timeout(1000)
            if el is None:
                log.debug(f"未找到 {label} 入口")
                return []
            el.click(timeout=8000)
        except Exception as exc:  # noqa: BLE001
            log.debug(f"打开{label}失败: {str(exc)[:70]}")
            return []

    # 等子列表帧出现（最多 7 秒；列表多半很快出来，空列表也不必久等）
    target = None
    deadline = time.time() + 7
    while time.time() < deadline and target is None:
        for f in all_frames(page):
            if url_key in (f.url or ""):
                target = f
                break
        if target is None:
            page.wait_for_timeout(500)
    if target is None:
        log.debug(f"{label}: 未等到子列表帧")
        return []

    # 等卡片渲染（最多 4 秒）
    for _ in range(4):
        try:
            if target.query_selector("li.course, [courseid]"):
                break
        except Exception:  # noqa: BLE001
            break
        page.wait_for_timeout(1000)
    try:
        return target.evaluate(JS_SUB_LIST_CARDS) or []
    except Exception:  # noqa: BLE001
        return []


def fetch_excluded_courses(session, cfg, log):
    """读取平台「已结束课程 / 已退课课程」列表 → {courseId: 标签}。"""
    result = {}
    if not bool(cfg.course.skip_ended_courses):
        return result
    for label, sel, key, path in (
        ("已结束课程", ENDED_LIST_SELECTOR, "stuendcourselist", "/mooc-ans/visit/stuendcourselist"),
        ("已退课课程", DROP_LIST_SELECTOR, "getdropclazzlist", "/mooc-ans/visit/getdropclazzlist"),
    ):
        cards = _open_sub_list(session, cfg, log, sel, key, label, url_path=path)
        added = 0
        for c in cards:
            cid = str(c.get("courseid") or "")
            if cid and cid not in result:
                result[cid] = label
                added += 1
        if cards:
            log.info(f"平台列表「{label}」：{added} 门")
    return result


def annotate_courses(session, cfg, log, cards):
    """给课程卡片补充状态判定结果（status / status_reason）。"""
    try:
        excluded = fetch_excluded_courses(session, cfg, log)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"读取平台结束/退课列表失败（不影响其它判定）: {str(exc)[:70]}")
        excluded = {}
    for c in cards:
        status, reason = classify_course(c, cfg, excluded=excluded)
        c["status"] = status
        c["status_reason"] = reason
    return cards


def filter_by_status(cards, cfg, log):
    """按策略过滤课程 → (保留, 排除)。"""
    policy = str(cfg.course.deadline_policy or "skip").strip().lower()
    if policy in ("all", "off", "none", "不判断"):
        return list(cards), []
    mark_only = policy == "mark"
    keep_unknown = str(cfg.course.unknown_status_policy or "keep").strip().lower() != "skip"

    kept, dropped = [], []
    for c in cards:
        st = c.get("status") or "unknown"
        label = STATUS_LABELS.get(st, st)
        if mark_only:
            kept.append(c)
            continue
        if st in ("ended", "dropped"):
            dropped.append(c)
            continue
        if st == "not_started" and bool(cfg.course.skip_not_started):
            dropped.append(c)
            continue
        if st == "unknown" and not keep_unknown:
            dropped.append(c)
            continue
        kept.append(c)
    for c in dropped:
        log.info(f"排除课程：{c['name']}（{STATUS_LABELS.get(c.get('status'), '')}："
                 f"{c.get('status_reason', '')}）")
    if dropped:
        log.info(f"共排除 {len(dropped)} 门；剩余 {len(kept)} 门待处理")
    return kept, dropped


def split_tokens(value):
    """把配置项/命令行参数解析成关键词列表（支持逗号分隔字符串或列表）。"""
    if not value:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,，;；]", value)
    else:
        parts = list(value)
    return [str(p).strip() for p in parts if str(p).strip()]


def match_course(card, token):
    """命中判定：课程名包含关键词，或 courseId/clazzid 完全相等。"""
    t = (token or "").strip()
    if not t:
        return False
    if t in (card.get("course_id"), card.get("clazzid")):
        return True
    return t in (card.get("name") or "")


def label_for(index):
    """1 → A，2 → B，…，27 → AA（Excel 列名风格）。"""
    s = ""
    i = int(index)
    while i > 0:
        i, rem = divmod(i - 1, 26)
        s = chr(65 + rem) + s
    return s


def label_index(token):
    """字母标签 → 1-based 序号（A→1，AA→27）；非法返回 None。"""
    t = (token or "").strip().upper()
    if not t or not t.isalpha() or len(t) > 3:
        return None
    n = 0
    for ch in t:
        n = n * 26 + (ord(ch) - 64)
    return n or None


def is_all_spec(spec):
    """是否表示「全部」（all / * / 全部 / 空）。"""
    s = str(spec or "").strip().lower()
    return s in ("", "all", "*", "全部", "所有", "全选")


def parse_index_spec(spec, total=None):
    """解析多选表达式 → set(1-based 序号)。

    支持混合写法：`1,3,5` / `2-5` / `A,C` / `A-C` / `1,A`（数字与字母混用）；
    `all`/`*`/`全部`/空 → 返回全部（需给 total）。无法解析的部分忽略。
    """
    if is_all_spec(spec):
        return set(range(1, int(total) + 1)) if total else set()
    result = set()
    for part in re.split(r"[,，;；\s]+", str(spec).strip()):
        if not part:
            continue
        m = re.match(r"^([A-Za-z]+)\s*[-~]\s*([A-Za-z]+)$", part)
        if m:
            a, b = label_index(m.group(1)), label_index(m.group(2))
            if a and b:
                result.update(range(min(a, b), max(a, b) + 1))
            continue
        m = re.match(r"^(\d+)\s*[-~]\s*(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            result.update(range(min(a, b), max(a, b) + 1))
            continue
        if part.isdigit():
            result.add(int(part))
            continue
        li = label_index(part)
        if li:
            result.add(li)
    if total:
        result = {i for i in result if 1 <= i <= int(total)}
    return result


def read_line(prompt, timeout_s, log, default=""):
    """带超时的控制台输入（超时返回 default）。"""
    event = threading.Event()
    holder = {}

    def _reader():
        try:
            holder["raw"] = input(prompt).strip()
        except Exception:  # noqa: BLE001
            holder["raw"] = ""
        event.set()

    threading.Thread(target=_reader, daemon=True).start()
    if not event.wait(timeout_s):
        log.warning(f"输入超时（{timeout_s} 秒），采用默认值：{default or '（空）'}")
        return default
    raw = holder.get("raw", "")
    return raw if raw != "" else default


def print_lesson_list(chapters):
    """打印课时清单（序号 + 标题 + 状态）。"""
    print("\n----- 课时清单 -----")
    for i, ch in enumerate(chapters, 1):
        if ch.hint == 0:
            flag = "✔已完成"
        elif ch.hint:
            flag = f"待完成({ch.hint})"
        else:
            flag = ""
        print(f"  [{i:>3}] {ch.title[:54]}{('  ' + flag) if flag else ''}")
    print("----- 共 %d 个课时 -----" % len(chapters))


def pick_courses_interactively(cards, log, timeout_s=180):
    """课程勾选：字母标注 + 多选（A C F / A-C / 1,3 / all / 课程名关键词）。"""
    print("\n===== 请选择要执行的课程（可多选）=====")
    for i, c in enumerate(cards, 1):
        status = c.get("status") or "unknown"
        tag = STATUS_LABELS.get(status, status)
        print(f"  [{label_for(i):>2}] {c['name']}    [{tag}] {c.get('status_reason', '')}")
    print("  输入示例：A C F（多门）｜A-C（区间）｜1,3（序号）｜all（全部）｜active（只选可处理的）")
    raw = read_line(">>> 请输入后回车: ", timeout_s, log, "")
    if str(raw).strip().lower() in ("active", "可处理", "有效"):
        picked = [c for c in cards if (c.get("status") or "unknown") in ("active", "unknown")]
        log.info("已选择可处理课程：" + "、".join(c["name"] for c in picked))
        return picked
    if is_all_spec(raw):
        if not raw:
            log.warning("未输入，默认处理全部课程")
        return cards

    idx = parse_index_spec(raw, len(cards))
    if idx:
        chosen = [cards[i - 1] for i in sorted(idx)]
        log.info("已选择课程：" + "、".join(c["name"] for c in chosen))
        return chosen

    picked = [c for c in cards if any(match_course(c, t) for t in split_tokens(raw))]
    if picked:
        log.info("已选择课程：" + "、".join(c["name"] for c in picked))
        return picked
    log.warning("未能解析你的输入，默认处理全部课程")
    return cards


class CourseInfo:
    def __init__(self, course_id="", clazzid="", cpi="", enc="", name="", url="", userid=""):
        self.course_id = course_id
        self.clazzid = clazzid
        self.cpi = cpi
        self.enc = enc
        self.name = name
        self.url = url
        self.userid = userid

    def __repr__(self):
        return (f"CourseInfo(name={self.name!r}, courseId={self.course_id}, "
                f"clazzid={self.clazzid}, cpi={self.cpi})")


class Chapter:
    def __init__(self, cid, title, element, hint=None):
        self.id = str(cid)
        self.title = title
        self.element = element
        self.hint = hint          # None=未知；0=已完成；>0=未完成任务点数

    def __repr__(self):
        return f"Chapter({self.id}, {self.title!r}, hint={self.hint})"


class TaskPoint:
    def __init__(self, title, ptype, element):
        self.title = title
        self.type = ptype          # video / document / quiz / read / live / unknown
        self.element = element

    def __repr__(self):
        return f"TaskPoint({self.type}, {self.title!r})"


TYPE_HINTS = [
    ("video", ["视频", "播放", "video", "录播"]),
    ("document", ["文档", "课件", "PPT", "ppt", "pdf", "讲义"]),
    ("quiz", ["测验", "测试", "作业", "考试", "quiz", "work", "exam", "答题"]),
    ("read", ["阅读", "文章", "read"]),
    ("live", ["直播", "live", "见面课"]),
]

ID_PATTERNS = {
    "course_id": ["courseId", "courseid", "moocId"],
    "clazzid": ["clazzid", "classId"],
    "cpi": ["cpi"],
    "enc": ["enc"],
}


def parse_ids(text):
    ids = {}
    for key, pats in ID_PATTERNS.items():
        for p in pats:
            m = re.search(re.escape(p) + r"=([0-9a-zA-Z]{1,64})", text or "")
            if m:
                ids[key] = m.group(1)
                break
    return ids


def _cookie_userid(session):
    try:
        for c in session.context.cookies():
            if c.get("name") in ("_uid", "UID"):
                return str(c.get("value", ""))
    except Exception:  # noqa: BLE001
        pass
    return ""


# ---------------- 课程列表 ----------------
COURSE_CARD_SELECTORS = [
    '[class*="courseName"]',
    '[class*="course-name"]',
    '[class*="courseItem"]',
    '[class*="course_item"]',
    '[class*="courseCard"]',
    '[class*="course-card"]',
    'a[href*="mycourse"]',
    'a[href*="courseId"]',
    'a[href*="courseid"]',
    'a[href*="clazzid"]',
    '[class*="course"] a[href]',
]

MAX_COURSES = 100


def all_frames(page):
    """返回页面所有 frame（含主框架）；课程列表、章节树可能都在 iframe 里。"""
    try:
        return list(page.frames) or [page]
    except Exception:  # noqa: BLE001
        return [page]


JS_COURSE_CARDS = r"""
() => {
  const out = [];
  document.querySelectorAll('li.course, [courseid][clazzid]').forEach((el) => {
    const cid = el.getAttribute('courseid') || '';
    const clz = el.getAttribute('clazzid') || '';
    if (!cid || !clz) return;
    const nameEl = el.querySelector('.course-name');
    const a = el.querySelector('a[href]');
    out.push({
      name: ((nameEl ? nameEl.innerText : el.innerText) || '').replace(/\s+/g, ' ').trim().slice(0, 60),
      course_id: cid,
      clazzid: clz,
      text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200),
      url: a ? (a.getAttribute('href') || '') : '',
      personid: el.getAttribute('personid') || ''
    });
  });
  return out;
}
"""


def _collect_courses(page):
    """跨 frame 采集课程卡片（单次 JS/帧）。

    返回 [{name,url,course_id,clazzid,cpi,text}]；text 保留卡片原文，
    用于解析「开课时间：…～…」等状态信息。
    """
    items = []
    seen = set()
    for frame in all_frames(page):
        try:
            cards = frame.evaluate(JS_COURSE_CARDS) or []
        except Exception:  # noqa: BLE001
            continue
        for c in cards:
            key = (c.get("course_id"), c.get("clazzid"))
            if key in seen:
                continue
            seen.add(key)
            url = c.get("url") or ""
            if url:
                url = urllib.parse.urljoin(HOME_URL, url)
            m = re.search(r"cpi=(\d+)", url or "")
            items.append({
                "name": (c.get("name") or "").strip(),
                "url": url,
                "course_id": c.get("course_id") or "",
                "clazzid": c.get("clazzid") or "",
                "cpi": (m.group(1) if m else (c.get("personid") or "")),
                "text": c.get("text") or "",
            })
    return items


def _wait_cards(page, wait_s):
    deadline = time.time() + wait_s
    while time.time() < deadline:
        cards = _collect_courses(page)
        if cards:
            return cards
        page.wait_for_timeout(1500)
    return []


def list_courses(session, cfg, log, wait_s=25):
    """打开学习空间，耐心等待前端渲染后读取「我的课程」。"""
    page = session.page
    log.info("打开学习空间，读取「我的课程」列表…")
    try:
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=int(cfg.login.nav_timeout_ms))
    except Exception as exc:  # noqa: BLE001
        log.warning(f"打开学习空间失败: {str(exc)[:80]}")
    cards = _wait_cards(page, wait_s)
    if not cards:
        # 点一下「课程」标签再等
        try:
            tab = page.query_selector('text=课程')
            if tab is not None and tab.is_visible():
                tab.click()
                page.wait_for_timeout(2500)
        except Exception:  # noqa: BLE001
            pass
        cards = _wait_cards(page, 15)
    return cards


def choose_courses(session, cfg, log, cli_select=""):
    """挑出要处理的课程。

    优先级：course.url > cli_select（--select）> course.selected_courses > course.name_contains
    关键词支持逗号分隔，可用课程名片段或 courseId；"*" / all / 全部 = 全部课程。
    """
    if cfg.course.url:
        return [CourseInfo(name=(cfg.course.name_contains or "指定课程"), url=cfg.course.url)]
    cards = list_courses(session, cfg, log)
    if not cards:
        dump = f"{cfg.output_dir}/structure_course_list.txt"
        dump_structure(session.page, dump, log)
        raise RuntimeError(
            f"没读到「我的课程」列表（页面结构已导出到 {dump}）。"
            "也可以在 config 的 course.url 里直接填某门课程的学习页地址"
        )
    log.info(f"共发现 {len(cards)} 门课程，开始判定状态（平台标记 → 日期范围 → 手工覆盖）：")
    annotate_courses(session, cfg, log, cards)
    for c in cards:
        st = c.get("status") or "unknown"
        log.info(f"    [{STATUS_LABELS.get(st, st)}] {c['name']}  — {c.get('status_reason', '')}")
    # 记录状态供报告使用
    cfg["_course_status"] = [(c["name"], c.get("status", "unknown"), c.get("status_reason", "")) for c in cards]

    # 0) 按截止日期/平台状态过滤
    cards, dropped = filter_by_status(cards, cfg, log)
    cfg["_course_dropped"] = [(c["name"], c.get("status", ""), c.get("status_reason", "")) for c in dropped]
    if not cards:
        raise RuntimeError(
            "所有课程都被状态判定过滤掉了。可用 course.deadline_policy: mark 查看全部课程状态，"
            "或用 course.deadline_overrides 手工指定"
        )

    # 1) 排除（配置关键词）
    exclude = split_tokens(cfg.course.exclude_courses)
    if exclude:
        before = len(cards)
        cards = [c for c in cards if not any(match_course(c, t) for t in exclude)]
        log.info(f"按排除条件过滤：{before} → {len(cards)} 门")
        if not cards:
            raise RuntimeError(f"排除条件 {exclude} 过滤掉了所有课程")

    # 2) 决定选择来源
    tokens = split_tokens(cli_select) or split_tokens(cfg.course.selected_courses)
    all_mode = False
    if not tokens:
        kw = str(cfg.course.name_contains or "").strip()
        all_mode = kw in ("", "*", "all", "ALL", "全部")
        tokens = [] if all_mode else split_tokens(kw)

    # 3) 交互勾选 / 全选 / 关键词匹配
    if cfg.course.pick_interactively:
        log.info("已启用交互勾选课程")
        chosen = pick_courses_interactively(cards, log)
    elif all_mode:
        log.info("配置为「全部课程」，将逐门处理")
        chosen = cards
    else:
        chosen = [c for c in cards if any(match_course(c, t) for t in tokens)]
        if not chosen:
            names = "、".join(c["name"] for c in cards[:20])
            raise RuntimeError(
                f"没有课程匹配 {tokens}。当前账号的课程有：{names}"
                "（用 '*' 或 --select all 处理全部课程）"
            )
        log.info("已按条件选中：" + "、".join(c["name"] for c in chosen))
    result = []
    for c in chosen[:MAX_COURSES]:
        result.append(CourseInfo(
            course_id=c.get("course_id", ""),
            clazzid=c.get("clazzid", ""),
            cpi=c.get("cpi", ""),
            enc=c.get("enc", ""),
            name=c["name"],
            url=c["url"],
        ))
    return result


def enter_course(session, course, cfg, log):
    """进入指定课程：优先用直链跳转，然后等待章节树出现（可能在 iframe 里）。"""
    page = session.page
    log.info(f"进入课程: {course.name}")
    nav_timeout = int(cfg.login.nav_timeout_ms)
    if course.url:
        try:
            page.goto(course.url, wait_until="domcontentloaded", timeout=nav_timeout)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"打开课程页失败: {str(exc)[:80]}")
        page.wait_for_timeout(3500)

    # 等待章节树出现；若课程首页没有，尝试点「章节」标签
    if find_chapters_frame(page) is None:
        for sel in ['text=章节', 'a:has-text("章节")', '[class*="chapter"] a']:
            try:
                clicked = False
                for frame in all_frames(page):
                    el = frame.query_selector(sel)
                    if el is not None and el.is_visible():
                        target, _ = session.click_capture(page, el, wait_s=10)
                        page = target
                        page.wait_for_timeout(3000)
                        clicked = True
                        break
                if clicked:
                    break
            except Exception:  # noqa: BLE001
                continue

    # 补齐课程参数（直链里通常已带，缺则从各 frame 的 URL/内容里找）
    if not (course.course_id and course.clazzid):
        cand = [course.url] + [f.url for f in all_frames(page)]
        for text in cand:
            ids = parse_ids(text or "")
            if ids.get("course_id"):
                course.course_id = ids.get("course_id", "")
                course.clazzid = ids.get("clazzid", course.clazzid)
                course.cpi = ids.get("cpi", course.cpi)
                course.enc = ids.get("enc", course.enc)
                break
    if not course.cpi:
        try:
            m = re.search(r"cpi=(\d+)", " ".join(f.url for f in all_frames(page)))
            if m:
                course.cpi = m.group(1)
        except Exception:  # noqa: BLE001
            pass
    course.userid = _cookie_userid(session)
    log.info(f"课程参数: courseId={course.course_id} clazzid={course.clazzid} cpi={course.cpi}")
    log.info(f"章节树位置: {'已找到' if find_chapters_frame(page) is not None else '未找到'}")
    return course


def find_chapters_frame(page, use_cache=True):
    """返回含有章节树的 frame；带缓存（先验旧引用，失效再全量扫描）。"""
    if use_cache:
        cached = _FRAME_CACHE.get("frame")
        if cached is not None:
            try:
                if cached.query_selector('div.chapter_item[id^="cur"], a[href*="chapterId"]') is not None:
                    return cached
            except Exception:  # noqa: BLE001
                pass
            _FRAME_CACHE["frame"] = None
    for frame in all_frames(page):
        try:
            el = frame.query_selector('div.chapter_item[id^="cur"], a[href*="chapterId"], a[href*="knowledgeId"]')
        except Exception:  # noqa: BLE001
            continue
        if el is not None:
            _FRAME_CACHE["frame"] = frame
            return frame
    return None


def find_content_frame(page):
    """返回任务点容器 frame（knowledge/cards）。"""
    for frame in all_frames(page):
        try:
            if "knowledge/cards" in (frame.url or ""):
                return frame
        except Exception:  # noqa: BLE001
            continue
    return None


def chapter_element(page, cid):
    """定位指定章节的可点击元素（优先在缓存的树帧里找，通常 1 次 CDP 命中）。"""
    frames = []
    cached = _FRAME_CACHE.get("frame")
    if cached is not None:
        frames.append(cached)
    frames.extend([f for f in all_frames(page) if f is not cached])
    for frame in frames:
        try:
            el = frame.query_selector(f"div.chapter_item#cur{cid}")
            if el is not None:
                return frame, el
            el = frame.query_selector(f'a[href*="chapterId={cid}"], a[href*="knowledgeId={cid}"]')
            if el is not None:
                return frame, el
        except Exception:  # noqa: BLE001
            continue
    return None, None


def card_type(cards_frame):
    """判断当前任务点卡片类型：video / document / quiz / unknown。"""
    subs = []
    try:
        subs = cards_frame.evaluate(
            "() => Array.from(document.querySelectorAll('iframe')).map(x => x.getAttribute('src') || '')"
        ) or []
    except Exception:  # noqa: BLE001
        pass
    html = ""
    try:
        html = cards_frame.evaluate("() => (document.body ? document.body.innerHTML : '')") or ""
    except Exception:  # noqa: BLE001
        pass
    blob = " ".join(subs) + " " + html[:5000]
    low = blob.lower()
    if "modules/video" in low or "ans-insertvideo" in low:
        return "video"
    if "modules/document" in low or "insertdoc" in low or "pdfjs" in low or "office" in low:
        return "document"
    if "/work/" in low or "insertwork" in low or "测验" in blob or "答题" in blob:
        return "quiz"
    # 音频任务点：iframe 为 ananas/modules/audio（video.js 播 m4a）
    if "modules/audio" in blob or "insertaudio" in blob or "ans-attach-online ans-insertaudio" in blob:
        return "audio"
    # 新版界面(mooc2-ans)：mooc-ans/knowledge/cards 是知识点正文页（知识目标/能力目标这类内容）
    # → 按"文档/阅读"处理（滚动+停留够时长即可算完成）
    try:
        furl = (cards_frame.url or "").lower()
    except Exception:  # noqa: BLE001
        furl = ""
    if "knowledge/cards" in furl:
        return "document"
    return "unknown"


def chapter_state(page, cid):
    """单次 JS 读取某个知识点在章节树上的最新完成状态（0=已完成 / >0=未完成数 / None）。"""
    frame = find_chapters_frame(page)
    if frame is None:
        return None
    try:
        return frame.evaluate(JS_ONE_CHAPTER, str(cid))
    except Exception:  # noqa: BLE001
        return None


def tree_progress(frame):
    """读取章节树头部「已完成任务点: x/y」。"""
    try:
        text = frame.evaluate("() => (document.body ? document.body.innerText : '')") or ""
        m = re.search(r"已完成任务点[:：]\s*(\d+)\s*/\s*(\d+)", text)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:  # noqa: BLE001
        pass
    return None


def goto_toc(session, course, log):
    """尽量回到/打开章节目录页。"""
    page = session.page
    if not (course.course_id and course.clazzid and course.cpi):
        return page
    url = (f"https://mooc1.chaoxing.com/mycourse/studentcourse?courseid={course.course_id}"
           f"&clazzid={course.clazzid}&cpi={course.cpi}&ut=s")
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"打开章节目录页失败: {str(exc)[:80]}")
    return page


# ---------------- 章节树 ----------------
JS_CHAPTERS = r"""
() => {
  const out = [];
  document.querySelectorAll('div.chapter_item[id^="cur"]').forEach((el) => {
    const m = /cur(\d+)/.exec(el.id || '');
    let cid = m ? m[1] : '';
    if (!cid) {
      const oc = el.getAttribute('onclick') || '';
      const mm = /toOld\(\s*'?\d+'?\s*,\s*'?(\d+)'?/.exec(oc);
      if (mm) cid = mm[1];
    }
    if (!cid) return;
    const nameEl = el.querySelector('.catalog_name');
    const jd = el.querySelector('.catalog_jindu');
    let hint = null;
    if (jd) {
      const cls = (jd.className || '').toString().toLowerCase();
      const txt = (jd.innerText || '').trim();
      if (/^\d+$/.test(txt)) {
        hint = parseInt(txt, 10);                       // 旧版：橙色数字 = 未完成任务点数
      } else if (/openlock|wancheng|complete|finish|done/.test(cls)) {
        hint = 0;                                       // 已完成（绿色）
      } else if (/bukaifang|chuangguan|catalog_tishi|weikaifa|tishi/.test(cls)) {
        hint = 1;                                       // 新版图标：待完成（橙/灰）
      }
    }
    const raw = (nameEl ? nameEl.innerText : el.innerText) || '';
    out.push({
      id: cid,
      title: raw.replace(/\s+/g, ' ').trim().slice(0, 60),
      hint: hint
    });
  });
  return out;
}
"""

# 章节树缓存：同一页面/帧内复用解析结果，避免每个章节重复解析整棵树
_TREE_CACHE = {"key": None, "frame": None, "chapters": []}
_FRAME_CACHE = {"frame": None}


JS_ONE_CHAPTER = r"""
(cid) => {
  const el = document.querySelector('#cur' + cid);
  if (!el) return null;
  const jd = el.querySelector('.catalog_state, .catalog_jindu');
  if (!jd) return null;
  const cls = (jd.className || '').toString().toLowerCase();
  const txt = (jd.innerText || '').trim();
  if (/^\d+$/.test(txt)) return parseInt(txt, 10);
  if (/openlock|wancheng|complete|finish|done/.test(cls)) return 0;
  if (/bukaifang|chuangguan|catalog_tishi|weikaifa|tishi/.test(cls)) return 1;
  return null;
}
"""


def reset_caches():
    """清空章节树/帧缓存（重新登录或页面大变后调用）。"""
    _TREE_CACHE.update(key=None, frame=None, chapters=[])
    _FRAME_CACHE.update(frame=None)


def update_chapter_hint(cid, hint):
    """把复核到的最新状态写回缓存，避免下次再整树重解析。"""
    for ch in _TREE_CACHE.get("chapters", []):
        if ch.id == str(cid):
            ch.hint = hint
            return True
    return False


def _js_chapters(frame):
    """单次 JS 调用解析整棵章节树（替代逐元素 CDP 往返，约 10~20 倍提速）。"""
    try:
        raw = frame.evaluate(JS_CHAPTERS) or []
    except Exception:  # noqa: BLE001
        return []
    chapters = []
    for item in raw:
        try:
            cid = str(item.get("id") or "")
            if not cid:
                continue
            chapters.append(Chapter(cid, item.get("title") or f"知识点{cid}", None, item.get("hint")))
        except Exception:  # noqa: BLE001
            continue
    return chapters


def _hint_from_item(el):
    """从章节点读取完成状态：0=已完成；>0=未完成任务点数；None=未知。"""
    try:
        jd = el.query_selector(".catalog_jindu")
        if jd is None:
            return None
        cls = (jd.get_attribute("class") or "").lower()
        text = (jd.inner_text() or "").strip()
        if any(k in cls for k in ("openlock", "complete", "finish", "done")):
            return 0
        if text.isdigit():
            return int(text)
    except Exception:  # noqa: BLE001
        pass
    return None


def _chapters_in(frame):
    """解析章节树：优先单次 JS 解析（快），失败再退回逐元素方式。"""
    chapters = _js_chapters(frame)
    if chapters:
        return chapters
    return _chapters_in_slow(frame)


def _chapters_in_slow(frame):
    """逐元素解析（旧页面结构兜底，慢但兼容）。"""
    chapters = []
    seen = set()
    # 1) 新版：div.chapter_item#curXXX
    try:
        elements = frame.query_selector_all('div.chapter_item[id^="cur"]')
    except Exception:  # noqa: BLE001
        elements = []
    for el in elements:
        try:
            el_id = el.get_attribute("id") or ""
            m = re.match(r"cur(\d+)", el_id)
            cid = m.group(1) if m else ""
            if not cid:
                onclick = el.get_attribute("onclick") or ""
                mm = re.search(r"toOld\(\s*'?\d+'?\s*,\s*'?(\d+)'?", onclick)
                if mm:
                    cid = mm.group(1)
            if not cid or cid in seen:
                continue
            seen.add(cid)
            name_el = el.query_selector(".catalog_name")
            if name_el is not None:
                title = (name_el.inner_text() or "").strip().replace("\n", " ")
            else:
                title = (el.inner_text() or "").strip().replace("\n", " ")[:60]
            if not title:
                title = f"知识点{cid}"
            chapters.append(Chapter(cid, title[:60], el, _hint_from_item(el)))
        except Exception:  # noqa: BLE001
            continue
    if chapters:
        return chapters
    # 2) 兜底：老版 href 形式
    for sel in ['a[href*="chapterId"]', 'a[href*="knowledgeId"]']:
        try:
            elements = frame.query_selector_all(sel)
        except Exception:  # noqa: BLE001
            continue
        for el in elements:
            try:
                href = el.get_attribute("href") or ""
                m = re.search(r"(?:chapterId|knowledgeId)=(\d+)", href)
                if not m:
                    continue
                cid = m.group(1)
                if cid in seen:
                    continue
                seen.add(cid)
                title = (el.inner_text() or "").strip().replace("\n", " ")[:60] or f"章节{cid}"
                chapters.append(Chapter(cid, title, el, _hint_from_item(el)))
            except Exception:  # noqa: BLE001
                continue
    return chapters


def discover_chapters(page, log, force=False):
    """解析章节树（单次 JS + 按页面缓存）。

    force=True 用于需要最新状态的场景（如完成后的复核）。
    """
    frame = find_chapters_frame(page, use_cache=not force)
    if frame is None:
        return []
    key = ((page.url or ""), (frame.url or ""))
    if not force and _TREE_CACHE.get("key") == key and _TREE_CACHE.get("chapters"):
        log.debug("章节树命中缓存，跳过重复解析")
        return _TREE_CACHE["chapters"]

    best = _chapters_in(frame)
    if not best:
        for other in all_frames(page):
            found = _chapters_in(other)
            if len(found) > len(best):
                best = found
    log.info(f"解析到 {len(best)} 个章节")
    for c in best[:8]:
        log.debug(f"  章节 {c.id}: {c.title}")
    _TREE_CACHE.update(key=key, frame=frame, chapters=best)
    return best


# ---------------- 任务点 ----------------
def guess_type(text, classes=""):
    blob = f"{text} {classes}"
    for ptype, keywords in TYPE_HINTS:
        if any(k in blob for k in keywords):
            return ptype
    return "unknown"


def _task_points_in(frame):
    points = []
    seen = set()
    for sel in ['a[href*="jobid"]', '[class*="job"]', '[class*="task"]', '[class*="card"]', '[class*="item"]']:
        try:
            elements = frame.query_selector_all(sel)
        except Exception:  # noqa: BLE001
            continue
        for el in elements:
            try:
                if not el.is_visible():
                    continue
                text = (el.inner_text() or "").strip().replace("\n", " ")
                if not text or len(text) > 120:
                    continue
                cls = el.get_attribute("class") or ""
                ptype = guess_type(text, cls)
                if ptype == "unknown":
                    continue
                if text in seen:
                    continue
                # 过滤父容器：若已有条目文本完全包含当前文本，跳过当前
                if any(text in t for t in seen):
                    continue
                seen.add(text)
                points.append(TaskPoint(text, ptype, el))
            except Exception:  # noqa: BLE001
                continue
    return points


def discover_task_points(page, log):
    """在所有 frame 中找任务点卡片，取条目最多的那个。"""
    best = []
    for frame in all_frames(page):
        found = _task_points_in(frame)
        if len(found) > len(best):
            best = found
    log.info(f"当前章节解析到 {len(best)} 个任务点")
    for p in best:
        log.debug(f"  任务点 [{p.type}] {p.title}")
    return best


# ---------------- 平台状态核验 ----------------
def chapter_status(session, course, chapter_ids, log):
    """调用平台接口查询章节任务点完成情况（权威状态）。"""
    if not (course.course_id and course.clazzid and course.cpi) or not chapter_ids:
        return {}
    payload = {
        "view": "json",
        "nodes": ",".join(str(c) for c in chapter_ids),
        "clazzid": course.clazzid,
        "time": str(int(time.time() * 1000)),
        "userid": course.userid or "",
        "cpi": course.cpi,
        "courseid": course.course_id,
    }
    for userid in [payload["userid"], "", None]:
        payload["userid"] = userid or ""
        try:
            resp = session.context.request.post(
                "https://mooc1-api.chaoxing.com/job/myjobsnodesmap",
                form=payload,
                timeout=20000,
            )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.debug(f"状态接口请求失败(userid={userid}): {exc}")
            continue
        if not isinstance(data, dict) or not data:
            continue
        result = {}
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            total = int(value.get("totalcount", 0) or 0)
            finish = int(value.get("finishcount", 0) or 0)
            unfinish = int(value.get("unfinishcount", 0) or 0)
            if unfinish and not total:
                total = unfinish
            result[str(key)] = {"total": total, "finish": finish, "unfinish": unfinish}
        if result:
            return result
    log.warning("未能通过接口获取章节状态（将改用页面上的完成标记判断）")
    return {}


# ---------------- 结构导出（排查用） ----------------
def dump_structure(page, path, log):
    js = """() => {
      const out = [];
      const nodes = document.querySelectorAll('a,button,div[class],li,span[class]');
      for (const el of nodes) {
        if (out.length >= 1000) break;
        const r = el.getBoundingClientRect();
        if (r.width < 4 || r.height < 4) continue;
        let cls = '';
        try { cls = (el.className || '').toString().slice(0, 60); } catch (e) { cls = ''; }
        const txt = (el.innerText || '').replace(/\\s+/g, ' ').slice(0, 60);
        if (!cls && !txt) continue;
        out.push(el.tagName.toLowerCase() + ' | ' + cls + ' | ' + txt);
      }
      return out;
    }"""
    try:
        lines = page.evaluate(js)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines), encoding="utf-8")
        log.info(f"页面结构已导出: {path} （用于排查选择器）")
    except Exception as exc:  # noqa: BLE001
        log.warning(f"页面结构导出失败: {exc}")
