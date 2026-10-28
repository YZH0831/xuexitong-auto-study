# -*- coding: utf-8 -*-
"""拟人化操作：鼠标轨迹、随机延时、页面滚动、周期性长暂停。

目的：让自动化行为更接近真人操作节奏，降低触发平台风控的概率。
所有动作都带随机抖动，绝不使用固定的机械间隔。

鼠标轨迹算法移植自开源项目 ghost-cursor（MIT，Xetera/ghost-cursor；
本文件的移植参考了其 Python 版 mcolella14/python_ghost_cursor 的 spoof.py/math.py）：
  - 三次贝塞尔曲线 + 随机控制点（不会走直线）
  - 步数按菲茨定律推算（目标越远/越小，步数越多）
  - 距离超过阈值时先"冲过头"再回摆修正（真实鼠标的 overshoot 行为）
之所以移植而非直接依赖：该库绑定 pyppeteer 且依赖 numpy，与本项目的
Playwright 栈不兼容，故只取算法、零新增依赖。
"""
import math
import random
import time


# ---------------- ghost-cursor 算法移植 ----------------
def _bezier_points(start, end, spread=None, max_points=40):
    """生成 start→end 的三次贝塞尔轨迹点（随机控制点，避免直线）。"""
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    distance = math.hypot(dx, dy)
    if distance < 2:
        return [(ex, ey)]
    ux, uy = dx / distance, dy / distance          # 单位方向
    px, py = -uy, ux                               # 垂直方向
    width = max(1.0, distance)
    spread_len = spread if spread is not None else min(width * 0.3, 200.0)

    def with_spread(bx, by, sign):
        s = random.random() * spread_len
        return (bx + ux * s * sign + px * s * random.uniform(-0.6, 0.6),
                by + uy * s * sign + py * s * random.uniform(-0.6, 0.6))

    c1 = with_spread(sx, sy, 1.0)
    c2 = with_spread(ex, ey, -1.0)

    # 步数：菲茨定律（目标越远越大 → 步数越多），带随机基数
    fitts = 2 * math.log2(distance / max(width, 1) + 1)
    steps = int(math.ceil((math.log2(fitts + 1) + random.random() * 25) * 3))
    steps = max(8, min(steps, max_points))

    pts = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1 - t
        x = (mt ** 3) * sx + 3 * (mt ** 2) * t * c1[0] + 3 * mt * (t ** 2) * c2[0] + (t ** 3) * ex
        y = (mt ** 3) * sy + 3 * (mt ** 2) * t * c1[1] + 3 * mt * (t ** 2) * c2[1] + (t ** 3) * ey
        pts.append((max(0.0, x), max(0.0, y)))
    return pts


OVERSHOOT_THRESHOLD = 500      # 超过这个像素距离就先冲过头再修正
OVERSHOOT_RADIUS = 120


def _pick(pair, default):
    """从 [lo, hi] 配置里取一个随机浮点值。"""
    try:
        lo, hi = float(pair[0]), float(pair[1])
    except Exception:  # noqa: BLE001
        lo, hi = default
    if hi < lo:
        lo, hi = hi, lo
    return random.uniform(lo, hi)


class Humanizer:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        h = dict(cfg.get("humanize", {}) or {})
        self.h = h
        self.enabled = bool(h.get("enabled", True))
        self._op_count = 0
        self.long_pause_every = int(h.get("long_pause_every", 0) or 0)
        self._last_pos = (random.uniform(80, 400), random.uniform(80, 300))   # 模拟鼠标当前位置

    # ---------------- 基础延时 ----------------
    def pause(self, key="click_before_ms", default=(200, 900), scale=1.0):
        """按配置项随机停顿（毫秒）。"""
        if not self.enabled:
            return
        ms = _pick(self.h.get(key), default) * scale
        if ms > 0:
            time.sleep(ms / 1000.0)

    def sleep(self, seconds, jitter=0.25):
        """随机抖动睡眠。"""
        if seconds and seconds > 0:
            time.sleep(max(0.05, seconds * (1 + random.uniform(-jitter, jitter))))

    # ---------------- 鼠标 ----------------
    def move_mouse(self, page, x, y, simple=False):
        """移动到目标坐标。默认用 ghost-cursor 风格的贝塞尔轨迹。"""
        if not self.enabled or page is None:
            return
        try:
            start = (self._last_pos[0], self._last_pos[1])
            distance = math.hypot(x - start[0], y - start[1])
            if distance > OVERSHOOT_THRESHOLD and not simple:
                # 先冲过头，再回摆修正（拟真）
                ang = math.atan2(y - start[1], x - start[0])
                over = (x + math.cos(ang) * OVERSHOOT_RADIUS,
                        y + math.sin(ang) * OVERSHOOT_RADIUS)
                self._trace(page, _bezier_points(start, over))
                self._trace(page, _bezier_points(over, (x, y), spread=10.0))
            else:
                pts = _bezier_points(start, (x, y)) if not simple else [(x, y)]
                self._trace(page, pts)
            self._last_pos = (x, y)
        except Exception:  # noqa: BLE001
            pass

    def _trace(self, page, points):
        """沿轨迹点移动鼠标（点间加极短随机停顿）。"""
        for (px, py) in points:
            try:
                page.mouse.move(px, py)
            except Exception:  # noqa: BLE001
                return
            if random.random() < 0.15:
                time.sleep(random.uniform(0.005, 0.02))

    def click(self, page, element, timeout=8000):
        """拟人化点击：随机前置延时 + 鼠标轨迹 + 按下/抬起微延时 + 后置延时。"""
        if element is None:
            return False
        if page is None or not self.enabled:
            element.click(timeout=timeout)
            self._after_op()
            return True
        try:
            element.scroll_into_view_if_needed(timeout=3000)
        except Exception:  # noqa: BLE001
            pass
        try:
            box = element.bounding_box()
        except Exception:  # noqa: BLE001
            box = None
        if not box or box.get("width", 0) < 2:
            element.click(timeout=timeout)
            self._after_op()
            return True

        x = box["x"] + box["width"] * random.uniform(0.30, 0.70)
        y = box["y"] + box["height"] * random.uniform(0.30, 0.70)
        self.pause("click_before_ms", (200, 900))
        self.move_mouse(page, x, y)
        time.sleep(random.uniform(0.08, 0.35))
        try:
            page.mouse.down()
            time.sleep(random.uniform(0.05, 0.16))
            page.mouse.up()
        except Exception:  # noqa: BLE001
            element.click(timeout=timeout)
        self.pause("click_after_ms", (300, 1200))
        self._after_op()
        return True

    # ---------------- 滚动 ----------------
    def scroll(self, page, times=None):
        if not self.enabled or page is None:
            return
        try:
            n = int(times) if times else int(_pick(self.h.get("scroll_times"), (1, 3)))
            for _ in range(max(0, n)):
                dy = int(_pick(self.h.get("scroll_step_px"), (200, 600)))
                if random.random() < 0.25:
                    dy = -dy
                page.mouse.wheel(0, dy)
                time.sleep(random.uniform(0.3, 1.3))
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 长暂停 ----------------
    def _after_op(self):
        self._op_count += 1
        if not self.enabled or self.long_pause_every <= 0:
            return
        if self._op_count % self.long_pause_every == 0:
            s = _pick(self.h.get("long_pause_s"), (15, 45))
            self.log.info(f"  [拟人] 已连续操作 {self._op_count} 次，休息 {s:.0f} 秒")
            time.sleep(s)

    def task_interval(self, seconds):
        """任务间隔：配置值 + 随机抖动。"""
        if seconds and seconds > 0:
            time.sleep(seconds * random.uniform(0.8, 1.6))
