# -*- coding: utf-8 -*-
"""新增功能自测：课程单选/多选解析、拟人化延时、联网搜题解析与限额、安全限制。"""
import argparse
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 本地用 config.yaml（含真实 Key），CI 里没有该文件 → 回退到示例配置
CFG_PATH = ROOT / "config.yaml"
if not CFG_PATH.exists():
    CFG_PATH = ROOT / "config.example.yaml"
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("test")

from cxlab.config import Config, load_config  # noqa: E402
from cxlab.course import (  # noqa: E402
    is_all_spec, label_for, label_index, match_course, parse_index_spec, split_tokens,
)
import cxlab.course as course_mod  # noqa: E402
from cxlab.humanize import Humanizer  # noqa: E402
import cxlab.video as video_mod  # noqa: E402
import cxlab.local_repair as local_repair_mod  # noqa: E402
from cxlab.llm_answer import LLMAnswer  # noqa: E402
from cxlab.online_answer import OnlineAnswer, looks_obfuscated  # noqa: E402
from cxlab.runner import Runner  # noqa: E402

cfg = Config(load_config(str(CFG_PATH)))

# ---------------- 1) 课程选择解析 ----------------
assert split_tokens("实验室安全, 高等数学") == ["实验室安全", "高等数学"]
assert split_tokens("实验室安全，高等数学") == ["实验室安全", "高等数学"]   # 中文逗号
assert split_tokens(["a", " b "]) == ["a", "b"]
assert split_tokens("") == []

card_a = {"name": "示例大学实验室安全", "course_id": "100000001", "clazzid": "200000002"}
card_b = {"name": "高等数学C园艺", "course_id": "267530203", "clazzid": "155823758"}
assert match_course(card_a, "实验室安全")          # 名称片段
assert match_course(card_a, "100000001")           # courseId
assert match_course(card_a, "200000002")           # clazzid
assert not match_course(card_b, "实验室安全")
print("[1] 课程单选/多选解析 OK")

# ---------------- 1b) 字母标签与多选表达式 ----------------
assert label_for(1) == "A" and label_for(26) == "Z" and label_for(27) == "AA"
assert label_index("A") == 1 and label_index("Z") == 26 and label_index("AA") == 27
assert label_index("") is None and label_index("A1") is None

assert parse_index_spec("A C F", 10) == {1, 3, 6}      # 字母多选（空格分隔）
assert parse_index_spec("A,F", 10) == {1, 6}           # 字母多选（逗号分隔）
assert parse_index_spec("A-C", 10) == {1, 2, 3}        # 字母区间
assert parse_index_spec("1,3,5", 10) == {1, 3, 5}      # 数字多选
assert parse_index_spec("2-4", 10) == {2, 3, 4}        # 数字区间
assert parse_index_spec("1,A", 10) == {1}              # 混合（1 与 A 同为第 1 个）
assert parse_index_spec("all", 5) == {1, 2, 3, 4, 5}
assert parse_index_spec("", 5) == {1, 2, 3, 4, 5}      # 空 = 全部
assert parse_index_spec("99", 5) == set()              # 越界被过滤
assert is_all_spec("all") and is_all_spec("") and is_all_spec("全部")
assert not is_all_spec("A"), "字母 A 是有效课程标签，不能被当作全选"
print("[1b] 字母标签 + 课时多选表达式（A C F / A-C / 1,3,5-8 / all）OK")

# ---------------- 2) 拟人化延时 ----------------
h = Humanizer(cfg, log)
assert h.enabled, "humanize 应默认开启"
samples = []
for _ in range(5):
    t0 = time.time()
    h.pause("click_before_ms", (200, 900))
    samples.append((time.time() - t0) * 1000)
assert all(150 <= s <= 1100 for s in samples), samples
assert len(set(round(s) for s in samples)) > 1, "延时应带随机性"
t0 = time.time()
h.task_interval(3)
dt = time.time() - t0
assert 2.0 <= dt <= 5.5, dt
print(f"[2] 拟人化 OK：点击前延时样本 {[round(s) for s in samples]} ms，任务间隔 {dt:.1f}s")

# ---------------- 3) 联网搜题（只测解析与限额，不发真实请求） ----------------
oa = OnlineAnswer(cfg, log)
assert oa.enabled
opts = [{"text": "A 保护优先"}, {"text": "B 预防为主"}, {"text": "C 综合治理"}]
# 新约束：答案必须出现在题干附近（模拟真实搜索结果：题干与答案同页）
# 且源页字母须经"选项文本对齐"换算（源页选项顺序可能与我们的不同）
STEM_T = "我国新《环境保护法》第五条规定"
OPTS_AB = [{"text": "A 政府为主", "letter": "A"}, {"text": "B 公众参与", "letter": "B"}]
# 我们的选项（顺序与源页可能不同）
opts_c = [{"text": "保护优先", "letter": "A"}, {"text": "预防为主", "letter": "B"},
          {"text": "综合治理", "letter": "C"}, {"text": "公众参与", "letter": "D"}]
# 源页选项与我们同序 → 字母直接可用（源C=综合治理=我们的C）
page_same = f"某题库 {STEM_T} A 保护优先 B 预防为主 C 综合治理 D 公众参与 正确答案：C 解析…"
assert oa._extract(page_same, opts_c, stem=STEM_T) == "C", oa._extract(page_same, opts_c, stem=STEM_T)
# 源页选项乱序（它把"综合治理"排在A）→ 源答案A应映射到我们的C
page_shuf = f"某库 {STEM_T} A 综合治理 B 预防为主 C 保护优先 D 公众参与 答案：A"
assert oa._extract(page_shuf, opts_c, stem=STEM_T) == "C", oa._extract(page_shuf, opts_c, stem=STEM_T)
# 源D=公众参与=我们的D
page_shuf2 = f"某库 {STEM_T} A 综合治理 B 预防为主 C 保护优先 D 公众参与 答案：D"
assert oa._extract(page_shuf2, opts_c, stem=STEM_T) == "D"
assert oa._extract(f"{STEM_T} A 政府为主 B 公众参与 答案 是 AB 。", OPTS_AB,
                   stem=STEM_T) == "AB"
assert oa._extract("里面没有答案标记", opts, stem=STEM_T) is None
# 页面里没有本题题干 → 裸字母答案不采信（避免从热搜/别的题误抓）
assert oa._extract("热搜榜 1 某新闻 2 某新闻 正确答案：C", opts, stem=STEM_T) is None
# 题干在页面上但答案离得很远（>600 字）→ 也不采信
assert oa._extract(STEM_T + "填充" * 800 + " 正确答案：C", opts, stem=STEM_T) is None
assert oa._extract("命中原文：综合治理 综合治理", opts, stem=STEM_T) == "综合治理"
assert looks_obfuscated("我国新《环糵保护孾》宎五条宄宍") is True
assert looks_obfuscated("我国新《环境保护法》第五条") is False
oa.max_searches = 1
oa.count = 0
assert oa.can_search()[0] is True
oa.count = 1
assert oa.can_search()[0] is False
print("[3] 联网搜题解析/限额 OK")

# ---------------- 4) 安全限制 ----------------
cfg2 = Config(load_config(str(CFG_PATH)))
args = argparse.Namespace(select="", pick=False, list_courses=False, dry_run=True,
                          only="", headless=True, course="", config="config.yaml")
runner = Runner(cfg2, log, args)
runner.start_ts = time.time()
assert runner._limit_reached() is False
cfg2["safety"]["max_tasks_per_run"] = 2
runner.task_count = 2
assert runner._limit_reached() is True and "任务点" in runner.stop_reason
cfg2["safety"]["max_tasks_per_run"] = 0
cfg2["safety"]["max_runtime_min"] = 0.02          # 1.2 秒
runner.start_ts = time.time() - 2
assert runner._limit_reached() is True and "时长" in runner.stop_reason
print("[4] 安全限制 OK（任务点数上限 / 运行时长上限）")

# ---------------- 5) 本次修复的高风险分支回归 ----------------
cfg3 = Config(load_config(str(CFG_PATH)))
args3 = argparse.Namespace(select="", pick=False, list_courses=False, dry_run=True,
                           only="video", headless=True, course="", config="config.yaml")
runner3 = Runner(cfg3, log, args3)
assert runner3._should_handle("video") and runner3._should_handle("audio")
assert not runner3._should_handle("document") and not runner3._should_handle("quiz")
args3.only = "document"
assert runner3._should_handle("document") and runner3._should_handle("read")
args3.only = "quiz"
assert runner3._should_handle("quiz") and not runner3._should_handle("video")
args3.only = ""
assert all(runner3._should_handle(t) for t in ("video", "audio", "document", "read", "quiz"))

counts = Runner._task_counts({
    "1": {"total": 3, "finish": 1, "unfinish": 2},
    "2": {"total": 2, "finish": 2, "unfinish": 0},
})
assert counts == (3, 5), counts


class _P:
    pass


p1, p2, p3 = _P(), _P(), _P()
assert runner3._quiz_pages(p1, [p2, p3, p1]) == [p1, p2, p3]


class _Lesson:
    def __init__(self, title):
        self.title = title


_lessons = [_Lesson(f"第{i}节") for i in range(1, 4)]
_course = type("C", (), {"name": "测试课程"})()
assert runner3._filter_lessons(_lessons, "1", _course) == [_lessons[0]]
assert runner3._filter_lessons(_lessons, "99", _course) == [], "无效课时表达式不能扩大为全部课时"

_cards = [
    {"name": "课程A", "course_id": "1", "clazzid": "11", "cpi": "21", "enc": "",
     "url": "https://example.com/a"},
    {"name": "课程B", "course_id": "2", "clazzid": "22", "cpi": "22", "enc": "",
     "url": "https://example.com/b"},
]
_old_list, _old_annotate, _old_filter = (
    course_mod.list_courses, course_mod.annotate_courses, course_mod.filter_by_status
)
course_mod.list_courses = lambda s, c, l: _cards
course_mod.annotate_courses = lambda s, c, l, cards: cards
course_mod.filter_by_status = lambda cards, c, l: (cards, [])
try:
    chosen = course_mod.choose_courses(None, cfg3, log, "all")
    assert [c.name for c in chosen] == ["课程A", "课程B"], chosen
finally:
    course_mod.list_courses, course_mod.annotate_courses, course_mod.filter_by_status = (
        _old_list, _old_annotate, _old_filter
    )

with tempfile.TemporaryDirectory() as td:
    cfgp = Path(td) / "config.yaml"
    cfgp.write_text(
        "course:\n  name_contains: 测试\nquiz:\n  font_table: data/font_map_table.json\n",
        encoding="utf-8",
    )
    cfg_anywhere = load_config(str(cfgp))
    assert "llm" in cfg_anywhere.quiz, "默认大模型配置必须挂在 quiz.llm"
    assert Path(cfg_anywhere.quiz.font_table) == (Path(td) / "data/font_map_table.json").resolve()

llm = LLMAnswer(cfg3, log)
ans, conf, _ = llm._parse('{"answer":"切断电源","confidence":0.9}', [], "text")
assert ans == "切断电源" and conf == 0.9, (ans, conf)

# CI 没有 config.yaml，示例配置默认关闭大模型；这里强制启用测试额度逻辑。
cfg3["quiz"]["llm"]["enabled"] = True
cfg3["quiz"]["llm"]["api_key"] = "test-key"
cfg3["quiz"]["llm"]["max_calls"] = 1
llm2 = LLMAnswer(cfg3, log)
llm2.max_calls = 1


def _bad_chat(_prompt):
    llm2.calls += 1
    return "不是 JSON"


llm2._chat = _bad_chat
assert llm2.solve("题干", [], "text")[0] is None
assert llm2.calls == 1, "剩余额度不足时不能再发起第二次重试"
print("[5] 高风险分支回归 OK（--only / 任务点计数 / 答题页隔离 / 无效课时 / 显式全选 / 配置路径 / 填空解析 / LLM限额）")

# ---------------- 6) 小圆状态复核：绿色/橙色0立即返回，不再固定干等 ----------------
_orig_chapter_state = course_mod.chapter_state
_orig_chapter_status = course_mod.chapter_status
try:
    print("[6] prepare runner...", flush=True)
    args6 = argparse.Namespace(select="", pick=False, list_courses=False, dry_run=True,
                               only="", headless=True, course="", config="config.yaml")
    runner6 = Runner(Config(load_config(str(CFG_PATH))), log, args6)
    print("[6] runner ready", flush=True)
    runner6.session = type("S", (), {"keep_alive": lambda self: None, "page": None})()
    runner6.course = type("C", (), {"name": "测试课", "course_id": "1", "clazzid": "2", "cpi": "3"})()
    runner6._tree_page = lambda: object()
    runner6.STATE_POLL_S = 0

    course_mod.chapter_status = lambda *a, **k: {}
    course_mod.chapter_state = lambda page, cid: 0
    print("[6] green check", flush=True)
    assert runner6._verify_chapter(type("CH", (), {"id": "g", "title": "绿色"})()) == 0
    print("[6] green ok", flush=True)

    course_mod.chapter_state = lambda page, cid: -1
    print("[6] orange0 check", flush=True)
    assert runner6._verify_chapter(type("CH", (), {"id": "o0", "title": "橙色0"})()) == -1
    print("[6] orange0 ok", flush=True)

    og_states = iter([1, 0])
    course_mod.chapter_state = lambda page, cid: next(og_states)
    print("[6] orange->green check", flush=True)
    assert runner6._verify_chapter(type("CH", (), {"id": "og", "title": "橙转绿"})()) == 0
    print("[6] orange->green ok", flush=True)
finally:
    course_mod.chapter_state = _orig_chapter_state
    course_mod.chapter_status = _orig_chapter_status
print("[6] 小圆状态复核 OK（绿色/橙色0立即返回，橙色转绿可观察到）")

# ---------------- 7) 未完成提示后重扫：只处理非题目，不重复提交 ----------------
class _FakeWaitPage:
    def wait_for_timeout(self, ms):
        return None


args7 = argparse.Namespace(select="", pick=False, list_courses=False, dry_run=True,
                           only="", headless=True, course="", config="config.yaml")
runner7 = Runner(Config(load_config(str(CFG_PATH))), log, args7)
runner7.session = type("S", (), {"keep_alive": lambda self: None, "page": None})()
runner7.course = type("C", (), {"name": "测试课", "course_id": "1", "clazzid": "2", "cpi": "3"})()
runner7._tree_page = lambda: object()
calls7 = {"open": 0, "doc": 0, "quiz": 0}
runner7._open_chapter = lambda ch: (calls7.__setitem__("open", calls7["open"] + 1) or _FakeWaitPage(), None)
runner7._quiz_tabs = lambda page: [
    (None, None, "章节测验", "quiz"),
    (None, None, "课件", "doc"),
]
runner7._verify_chapter = lambda ch, rounds=5: 1


def _fake_browse(page, ch):
    calls7["doc"] += 1
    return "done", "浏览完成"


def _forbidden_quiz(page, ch):
    calls7["quiz"] += 1
    return "done", "不应调用"


runner7._browse_here = _fake_browse
runner7._handle_quiz = _forbidden_quiz
r_status, r_note = runner7._rescan_current_tasks(
    type("CH", (), {"id": "x", "title": "重扫章节"})(), _FakeWaitPage()
)
assert r_status == "done" and "课件" in r_note, (r_status, r_note)
assert calls7 == {"open": 2, "doc": 1, "quiz": 0}, calls7
print("[7] 未完成重扫 OK（题目项跳过，其它内容只处理一次，不重复提交）")

# ---------------- 8) 直播入口优先于“敬请期待”占位跳过 ----------------
args8 = argparse.Namespace(select="", pick=False, list_courses=False, dry_run=True,
                           only="video", headless=True, course="", config="config.yaml")
runner8 = Runner(Config(load_config(str(CFG_PATH))), log, args8)
runner8.task_count = 0
runner8._handle_live = lambda page, ch: ("done", "直播回放：已播完")
runner8._placeholder_note = lambda cards: (_ for _ in ()).throw(
    AssertionError("直播模块不应先走占位跳过")
)
status8, note8 = runner8._handle_card(object(), None, "video", type("CH", (), {"id": "live"})())
assert status8 == "done" and "直播" in note8, (status8, note8)

_orig_all_frames = course_mod.all_frames


class _LiveFrame:
    url = "https://mooc1.chaoxing.com/ananas/modules/live/index.html"

    def query_selector(self, sel):
        return _LiveEl() if sel == "a.liveHref" else None


class _LiveEl:
    def is_visible(self):
        return True


class _LivePage:
    pass


course_mod.all_frames = lambda page: [_LiveFrame()]
try:
    # 仅验证选择器路径存在；元素可见性用真实 Playwright 时已实测。
    live_hit = runner8._find_live_entry(_LivePage())
    assert live_hit is not None and isinstance(live_hit[1], _LiveEl), live_hit
finally:
    course_mod.all_frames = _orig_all_frames
print("[8] 直播分支 OK（先点 liveHref，播放器在 zhibo 新标签页；无直播入口才按占位跳过）")

# ---------------- 9) 视频完成线 / 倍速限制识别 ----------------
live_rule = ("观看时长达到总时长的90%可完成任务点。你已观看0分钟"
             "（时长每5分钟更新一次），当前观看进度为0%")
assert video_mod.parse_completion_ratio(live_rule) == 0.9
assert video_mod.parse_completion_ratio(
    "观看时长达到总时长的90%可完成任务点。当前观看进度为100%") == 0.9
assert video_mod.parse_completion_ratio("请完整观看本视频") == 1.0


class _NoRuleFrame:
    def evaluate(self, *a, **k):
        return "你已观看 56.47 分钟（时长每 5 分钟更新一次）"


class _NoRulePage:
    frames = [_NoRuleFrame()]


assert video_mod.detect_completion_ratio(_NoRulePage(), default=None) is None
assert not any(h in live_rule for h in video_mod.NOSPEED_HINT), \
    "90% 完成线不能被误判成禁倍速"


class _RateLimitedEl:
    def __init__(self, ceiling):
        self.ceiling = ceiling

    def evaluate(self, js, rate):
        return min(float(rate), self.ceiling)


assert video_mod.probe_playback_rate(_RateLimitedEl(2.5), 3.0) == 2.5
assert video_mod.probe_playback_rate(_RateLimitedEl(1.0), 3.0) == 1.0
assert any(h in "本视频禁止倍速播放" for h in video_mod.NOSPEED_HINT)
assert video_mod.PLAYER_URL_HINTS[-1] == "modules/live"
print("[9] 视频规则 OK（90% 完成线可识别；90%文案不误判禁倍速；明确禁倍速仍锁 1x）")

# ---------------- 10) 橙色0：点进去、下滑、点下一节 ----------------
args10 = argparse.Namespace(select="", pick=False, list_courses=False, dry_run=True,
                            only="", headless=True, course="", config="config.yaml")
runner10 = Runner(Config(load_config(str(CFG_PATH))), log, args10)
runner10.report = type("R", (), {"add": lambda self, *a, **k: None})()
runner10.popup = type("P", (), {"tick": lambda self, page=None: None})()
calls10 = {"open": 0, "scroll": 0, "click": 0, "marks": None}


def _open10(ch):
    calls10["open"] += 1
    return _FakeWaitPage(), None


def _scroll10(page):
    calls10["scroll"] += 1


def _click10(page, marks=None):
    calls10["click"] += 1
    calls10["marks"] = marks
    return True


runner10._open_chapter = _open10
runner10._scroll_to_bottom_for_next = _scroll10
runner10._click_next_block = _click10
ch10 = course_mod.Chapter("zero", "橙色0", None, -1)
assert runner10._advance_orange_zero(ch10) == "done"
assert calls10 == {"open": 1, "scroll": 1, "click": 1,
                   "marks": ("下一节", "下一章")}, calls10

# 没点到下一节时，不再直接进入后续章节
runner10._click_next_block = lambda page, marks=None: False
ch10b = course_mod.Chapter("zero2", "橙色0失败", None, -1)
assert runner10._advance_orange_zero(ch10b) == "manual"
assert ch10b.hint == 1
print("[10] 橙色0推进 OK（点进去→下滑→点下一节；没点到则停止）")

# ---------------- 11) 两类绿色标识 + 本地助手保底 ----------------
assert course_mod.is_green_marker("catalog_state icon_yiwanc", "") is True   # 绿色勾
assert course_mod.is_green_marker("catalog_jindu green", "") is True          # 绿色小圆
assert course_mod.is_green_marker("", "已完成") is True
assert course_mod.is_green_marker("catalog_jindu", "1") is False
assert course_mod.is_green_marker("item incomplete", "") is False
redacted = local_repair_mod._safe_text(
    "Bearer abc.def token=secret123 https://example.com/x?enc=zzz")
assert "abc.def" not in redacted and "secret123" not in redacted and "example.com" not in redacted

with tempfile.TemporaryDirectory() as td:
    fake_cmd = [
        sys.executable, "-c",
        "import json,pathlib,sys; pathlib.Path(sys.argv[1]).write_text("
        "json.dumps({'handled': True, 'note': '模拟修复完成'}), encoding='utf-8')",
        "{response}",
    ]
    lr = {"enabled": True, "timeout_s": 5, "command": fake_cmd}
    ok, detail = local_repair_mod.request_repair(
        lr, log, td, course_mod.Chapter("repair-probe", "修复探针", None, 1),
        "测试本地助手响应协议", "不包含隐私",
    )
    assert ok and "模拟修复完成" in detail, (ok, detail)
    repair_files = list((Path(td) / "output/local_repair").glob("*.json"))
    assert repair_files
    assert all("config.yaml" not in f.read_text(encoding="utf-8") for f in repair_files)

args11 = argparse.Namespace(select="", pick=False, list_courses=False, dry_run=True,
                            only="", headless=True, course="", config="config.yaml")
runner11 = Runner(Config(load_config(str(CFG_PATH))), log, args11)
runner11.report = type("R", (), {"add": lambda self, *a, **k: None})()
repair_calls = {"n": 0, "result": True}
_orig_request_repair = local_repair_mod.request_repair


def _fake_request_repair(cfg, log, root, chapter, reason, note="", timeout_s=None):
    repair_calls["n"] += 1
    return repair_calls["result"], "本地助手测试响应"


local_repair_mod.request_repair = _fake_request_repair
try:
    ch11 = course_mod.Chapter("fix", "待修复", None, 1)
    assert runner11._try_local_repair(ch11, "测试问题", "测试说明") is True
    assert runner11._try_local_repair(ch11, "再次测试") is False  # 同章本 run 只唤起一次
    repair_calls["result"] = False
    ch11b = course_mod.Chapter("manual", "最后人工", None, 1)
    assert runner11._try_local_repair(ch11b, "测试人工") is False
finally:
    local_repair_mod.request_repair = _orig_request_repair
assert repair_calls["n"] == 2, repair_calls
print("[11] 绿色标识 + 本地助手保底 OK（两类绿色可识别；修复成功才重试，失败才人工）")

# ---------------- 12) 运行器保底：修好重试，失败才最终人工 ----------------
def _make_repair_runner(repair_ok):
    args = argparse.Namespace(select="", pick=False, list_courses=False, dry_run=False,
                              only="", headless=True, course="", config="config.yaml")
    r = Runner(Config(load_config(str(CFG_PATH))), log, args)
    r.report = type("R", (), {"add": lambda self, *a, **k: None})()
    r.session = type("S", (), {"probe_session": lambda self, *a, **k: "ok", "page": None})()
    r.course = type("C", (), {"name": "测试课", "course_id": "1", "clazzid": "2", "cpi": "3"})()
    r.humanizer = type("H", (), {"scroll": lambda self, p=None: None,
                                 "task_interval": lambda self, s=0: None})()
    r._refresh_platform_progress = lambda chapters, quiet=False: None
    r._ensure_session = lambda context="": True
    r._try_local_repair = lambda ch, reason, note="": repair_ok
    return r


runner12 = _make_repair_runner(True)
state12 = {"process": 0}


def _process_first_fail_then_done(ch):
    state12["process"] += 1
    if state12["process"] == 1:
        ch.hint = 1
        return "manual"
    ch.hint = 0
    return "done"


runner12.process_chapter = _process_first_fail_then_done
ch12 = course_mod.Chapter("repair", "本地修复", None, 1)
runner12._run_rounds([ch12])
assert state12 == {"process": 2}, state12
assert runner12.failed_chapters == []

runner12b = _make_repair_runner(False)
state12b = {"process": 0}


def _process_always_manual(ch):
    state12b["process"] += 1
    ch.hint = 1
    runner12b.failed_chapters.append(ch)
    runner12b.failed_this_run.append(ch.title)
    return "manual"


runner12b.process_chapter = _process_always_manual
ch12b = course_mod.Chapter("manual-final", "最终人工", None, 1)
runner12b._run_rounds([ch12b])
assert state12b == {"process": 1}, state12b
assert [c.id for c in runner12b.failed_chapters] == ["manual-final"]
print("[12] 本地助手保底调度 OK（修好→重试当前章节；失败→停止并转人工）")

# ---------------- 13) 直播 1x 重播必须重新点 liveHref，并透传 force_rate ----------------
class _LivePage:
    url = "https://zhibo.chaoxing.com/fake"

    def wait_for_timeout(self, ms):
        return None

    def bring_to_front(self):
        return None


runner13 = Runner(Config(load_config(str(CFG_PATH))), log, argparse.Namespace(
    select="", pick=False, list_courses=False, dry_run=True,
    only="", headless=True, course="", config="config.yaml"))
runner13.session = type("S", (), {
    "click_capture": lambda self, page, el, wait_s=0: (_LivePage(), True),
    "close_page": lambda self, page: None,
})()
runner13.popup = type("P", (), {"tick": lambda self, page=None: None})()
runner13._find_live_entry = lambda page: (object(), object())
runner13._ensure_session = lambda context="": True
_seen13 = {}
_orig_detect13 = video_mod.detect_completion_ratio
_orig_play13 = video_mod.play_video
video_mod.detect_completion_ratio = lambda page, default=None: 0.9


def _fake_play13(session, cfg, log, page, popup, tp, force_rate=None,
                 keepalive=None, completion_ratio=None):
    _seen13.update(force_rate=force_rate, completion=completion_ratio)
    return "done", "fake live play"


video_mod.play_video = _fake_play13
try:
    status13, note13 = runner13._handle_live(_LivePage(), course_mod.Chapter("live", "直播", None, 1),
                                             force_rate=1.0)
finally:
    video_mod.detect_completion_ratio = _orig_detect13
    video_mod.play_video = _orig_play13
assert status13 == "done" and _seen13 == {"force_rate": 1.0, "completion": 0.9}, _seen13
print("[13] 直播 1x 重播 OK（重新点 liveHref，force_rate/completion 正确透传）")

print("\n[OK] 新增功能自测全部通过")
