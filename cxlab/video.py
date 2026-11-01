# -*- coding: utf-8 -*-
"""视频任务点：定位真正的播放器、必要时点击播放、监控进度、按平台规则决定倍速。

要点（实测 2026-10-13 示例大学实验室安全课程）：
- 任务点 iframe 为 /ananas/modules/video/index.html；
- cards 帧里存在空的 <audio> 占位元素，必须跳过（否则会误判为播放器）；
- 平台规则文案：「观看时长需 ≥ 总时长的 90%，未完成任务点前不可拖拽、
  不可离开或将页面最小化」→ 必须 1 倍速真实播放。
"""
import re
import time

JS_PLAY = """
(el, rate) => {
  try { el.muted = true; } catch (e) {}
  try { el.playbackRate = rate; } catch (e) {}
  try { const p = el.play(); if (p && p.catch) p.catch(() => {}); } catch (e) {}
  return true;
}
"""

JS_STATE = """
(el) => ({
  ct: el.currentTime || 0,
  dur: isFinite(el.duration) ? el.duration : 0,
  paused: !!el.paused,
  ended: !!el.ended,
  rate: el.playbackRate || 1
})
"""

JS_INFO = """
(el) => ({
  src: (el.currentSrc || el.getAttribute('src') || '').trim(),
  dur: (isFinite(el.duration) ? el.duration : 0),
  ready: el.readyState || 0,
  tag: el.tagName.toLowerCase(),
  paused: !!el.paused
})
"""

# 平台上常见的"真实学习时间"规则文案
DURATION_RULE = re.compile(r"(累计观看|观看时长|需观看|观看满|观看时间)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(分钟|min)")
NOSPEED_HINT = [
    "禁止倍速", "不可快进", "不允许倍速", "不得快进",
    "不可拖拽", "总时长的", "观看时长需", "不可离开", "不可最小化",
]
PLAYER_URL_HINTS = ("modules/video", "modules/audio")   # 音频任务用 video.js 播 m4a，走同一套逻辑

PLAY_SELECTORS = [
    ".vjs-big-play-button",
    '[class*="big-play"]',
    '[class*="play-btn"]',
    '[class*="playBtn"]',
    '[class*="play_btn"]',
    '[aria-label*="播放"]',
    'text=播放视频',
    'text=点击播放',
    '[class*="play"]',
]

TIP_SELECTORS = [
    "text=知道了",
    '[class*="know"]',
    '[class*="tips"] [class*="close"]',
]


def find_player_frame(page, timeout_s=20):
    """优先返回 URL 含 modules/video 的播放器 frame。"""
    deadline = time.time() + timeout_s
    cards_like = None
    while time.time() < deadline:
        for frame in page.frames:
            try:
                url = frame.url or ""
            except Exception:  # noqa: BLE001
                continue
            if any(h in url for h in PLAYER_URL_HINTS):
                return frame
            if "knowledge/cards" in url:
                cards_like = frame
        time.sleep(0.5)
    return cards_like


def prepare_player(frame, log):
    """关闭播放器提示、点击播放按钮，触发视频加载（真实点击=真实用户手势）。"""
    if frame is None:
        return False
    for sel in TIP_SELECTORS:
        try:
            el = frame.query_selector(sel)
            if el is not None and el.is_visible():
                el.click(timeout=2000)
                log.debug(f"  已关闭播放器提示: {sel}")
                time.sleep(0.5)
                break
        except Exception:  # noqa: BLE001
            continue
    for sel in PLAY_SELECTORS:
        try:
            el = frame.query_selector(sel)
            if el is not None and el.is_visible():
                el.click(timeout=3000)
                log.info(f"  已点击播放按钮 ({sel})")
                time.sleep(1.5)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def find_video_element(page, timeout_s=30):
    """定位真正可用的媒体元素：跳过空 src / cards 页占位元素，优先能读到时长的。"""
    start = time.time()
    fallback = None
    while time.time() - start < timeout_s:
        for frame in page.frames:
            try:
                url = frame.url or ""
            except Exception:  # noqa: BLE001
                continue
            for tag in ("video", "audio"):
                try:
                    el = frame.query_selector(tag)
                except Exception:  # noqa: BLE001
                    continue
                if el is None:
                    continue
                try:
                    info = el.evaluate(JS_INFO)
                except Exception:  # noqa: BLE001
                    continue
                src = info.get("src") or ""
                if not src:
                    continue          # 空源 = 占位元素（如 cards 帧里的 auditionAudio）
                if "knowledge/cards" in src and info.get("dur", 0) <= 0:
                    # cards 路径下的空占位；但真实音频若也走这个路径且能读到时长得放行
                    continue
                if info.get("dur", 0) > 0:
                    return frame, el
                if fallback is None and (any(h in url for h in PLAYER_URL_HINTS) or "ananas" in url):
                    fallback = (frame, el)
        if fallback is not None and time.time() - start > 10:
            return fallback
        time.sleep(0.5)
    return fallback if fallback else (None, None)


JS_GET_TEXT = "() => (document.body ? document.body.innerText : '')"
JS_VISIBILITY = "() => ({ hidden: !!document.hidden, vis: document.visibilityState })"


def detect_rules(page):
    """一次遍历所有帧，同时判定「累计观看 N 分钟」与「禁倍速」限制。

    返回 (watch_min, no_speed)：watch_min 为分钟数或 None；no_speed 为布尔值。
    （原实现分两次各遍历一遍所有帧，现合并为一遍）
    """
    watch_min = None
    no_speed = False
    for frame in page.frames:
        try:
            text = frame.evaluate(JS_GET_TEXT) or ""
        except Exception:  # noqa: BLE001
            continue
        if not text:
            continue
        if watch_min is None:
            m = DURATION_RULE.search(text)
            if m:
                watch_min = float(m.group(2))
        if not no_speed and any(h in text for h in NOSPEED_HINT):
            no_speed = True
        if watch_min is not None and no_speed:
            break
    return watch_min, no_speed


def detect_watch_duration(page):
    """检测「累计观看 N 分钟」类要求，返回分钟数；没有返回 None。"""
    return detect_rules(page)[0]


def detect_no_speed(page):
    """检测"禁止倍速/必须真实观看"类限制。"""
    return detect_rules(page)[1]


def _kick(el, rate, frame, log):
    """尝试开始播放：先调 play()，若仍暂停则点播放器按钮。"""
    try:
        el.evaluate(JS_PLAY, rate)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"  play() 调用失败: {exc}")
    if frame is None:
        return
    try:
        st = el.evaluate(JS_STATE)
        if st.get("paused") and not st.get("ended"):
            for sel in PLAY_SELECTORS:
                b = frame.query_selector(sel)
                if b is not None and b.is_visible():
                    b.click(timeout=2000)
                    time.sleep(1)
                    return
    except Exception:  # noqa: BLE001
        pass


def play_video(session, cfg, log, page, popup, tp, force_rate=None, keepalive=None):
    """播放当前页视频，返回 (status, note)。

    force_rate: 强制倍速（用于"倍速未被登记 → 用 1x 重播"的兜底）。
    keepalive : 回调（每次轮询调用），返回 False 表示会话中断需停止播放。
    """
    vcfg = cfg.video

    player = find_player_frame(page, timeout_s=20)
    if player is not None:
        log.debug(f"  播放器 frame: {player.url[:90]}")
    prepare_player(player, log)

    frame, el = find_video_element(page, timeout_s=30)
    if el is None:
        return "manual", "未找到可用的视频播放器元素（可能需要人工点击播放）"

    rule_min, no_speed = detect_rules(page)       # 单次遍历取回两种规则
    force_1x = bool(vcfg.duration_required_1x) and (rule_min is not None or no_speed)
    rate = 1.0 if force_1x else float(vcfg.speed)
    if force_rate is not None:
        rate = float(force_rate)
    if rule_min:
        log.info(f"  平台要求累计观看 {rule_min} 分钟 → 强制 1 倍速真实播放")
    elif force_1x:
        log.info("  检测到平台限制（需真实观看/禁止倍速）→ 强制 1 倍速")
    else:
        log.info(f"  播放倍速: {rate}x")

    _kick(el, rate, frame, log)

    limit_s = float(vcfg.timeout_per_video_min) * 60
    poll = max(1.0, float(vcfg.poll_interval_s))
    start = time.time()
    last_ct = -1.0
    stall = 0
    no_dur = 0
    paused_cnt = 0
    hidden_ticks = 0
    last_alive = 0.0
    # 每 45 秒动一次鼠标：鼠标长时间静止时平台可能判为挂机、不计观看时长
    # （实测纯视频章节全部复核失败，而带测验的章节因做题时鼠标在动反而成功）
    alive_every = 45.0

    while True:
        # 会话健康检查：掉线/断网时由 runner 负责自动重登录或等待恢复
        if keepalive is not None:
            try:
                if not keepalive("视频播放"):
                    return "manual", "播放期间会话中断且未恢复（已看时长保留在平台，重跑会接着算）"
            except Exception as exc:  # noqa: BLE001
                log.debug(f"keepalive 回调异常（忽略）: {str(exc)[:60]}")
        popup.tick(page)
        session.keep_alive()
        elapsed = time.time() - start
        try:
            state = el.evaluate(JS_STATE)
        except Exception:  # noqa: BLE001
            frame2, el2 = find_video_element(page, timeout_s=8)
            if el2 is None:
                return "manual", "视频元素丢失（页面可能已跳转）"
            el = el2
            if frame2 is not None:
                frame = frame2
            continue

        # 平台规则：观看时「不可离开或将页面最小化」；后台运行可能导致不计时
        try:
            vis = page.evaluate(JS_VISIBILITY)
            if vis and vis.get("hidden"):
                hidden_ticks += 1
                if hidden_ticks in (2, 30):
                    log.warning("  浏览器窗口处于后台/最小化：平台可能暂停计时或不计入观看时长，"
                                "请保持该窗口可见（勿最小化）")
            else:
                hidden_ticks = 0
        except Exception:  # noqa: BLE001
            pass

        # 拟人保活：播放期间定期小幅移动鼠标（不点击，避免把视频点暂停），
        # 让平台看到"人还在看"，避免防挂机机制把这段时间判为无效观看。
        if elapsed - last_alive >= alive_every:
            last_alive = elapsed
            try:
                box = page.viewport_size or {"width": 1280, "height": 720}
                w = max(1, box["width"] - 320)
                h = max(1, box["height"] - 320)
                page.mouse.move(
                    160 + (int(elapsed) * 37) % w,
                    160 + (int(elapsed) * 61) % h,
                    steps=10,
                )
            except Exception:  # noqa: BLE001
                pass

        dur = state.get("dur", 0)
        ct = state.get("ct", 0)

        if state.get("ended") or (dur > 0 and ct >= dur - 0.4):
            log.info(f"  播放完成（用时 {elapsed/60:.1f} 分钟）")
            break

        if dur <= 0:
            no_dur += 1
            if no_dur in (5, 15):
                _kick(el, rate, frame, log)
            if no_dur >= 40:
                return "failed", "视频长时间未加载出时长（可能网络或播放器异常）"
        else:
            no_dur = 0

        if state.get("paused"):
            paused_cnt += 1
            if paused_cnt in (3, 10):
                _kick(el, rate, frame, log)
        else:
            paused_cnt = 0

        # 进度停滞：倍速被重置或播放器暂停
        if abs(ct - last_ct) < 0.01:
            stall += 1
            if stall == 3:
                log.debug("  进度停滞，重新应用倍速并尝试继续播放")
                try:
                    el.evaluate(JS_PLAY, rate)
                except Exception:  # noqa: BLE001
                    pass
            if stall >= 8:
                try:
                    el.click(timeout=2000)
                except Exception:  # noqa: BLE001
                    pass
                _kick(el, rate, frame, log)
                stall = 0
        else:
            stall = 0
        last_ct = ct

        if elapsed > limit_s:
            return "failed", f"播放超时（{vcfg.timeout_per_video_min} 分钟），进度 {ct:.0f}s"

        if int(elapsed) % 60 < poll and dur > 0:
            log.info(f"  进度 {ct:.0f}/{dur:.0f}s ({ct/dur*100:.0f}%)")
        time.sleep(poll)

    # 给平台一点时间登记完成状态。
    # 实测：纯视频章节播完立刻切走，平台常判「仍显示 1 个未完成」；
    # 而带测验的章节因为多停留了几分钟做题，视频反而被登记上了。
    # 因此这里主动多停留 ~20 秒并保持页面活跃（鼠标微动 + 轻微滚动），
    # 让播放器的最后一次心跳/上报有机会送达。
    page.wait_for_timeout(6000)
    try:
        for i in range(3):
            box = page.viewport_size or {"width": 1280, "height": 720}
            page.mouse.move(
                120 + (i * 97) % max(1, box["width"] - 240),
                120 + (i * 61) % max(1, box["height"] - 240),
                steps=10,
            )
            page.evaluate("(d) => window.scrollBy(0, d)", 6 if i % 2 == 0 else -6)
            page.wait_for_timeout(4500)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"播放后保活异常（忽略）: {str(exc)[:60]}")
    popup.tick(page)
    return "done", f"播放完成 rate={rate}x"
