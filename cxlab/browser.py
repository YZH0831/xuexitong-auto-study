# -*- coding: utf-8 -*-
"""浏览器会话管理：启动 Edge/Chrome、登录态持久化、新标签页捕获、会话保活。"""
import time
from pathlib import Path

# 网络层错误标记（与"登录失效"区分开）
NET_ERROR_MARKERS = (
    "ERR_INTERNET_DISCONNECTED", "ERR_CONNECTION", "ERR_NAME_NOT_RESOLVED",
    "ERR_TIMED_OUT", "ERR_NETWORK_CHANGED", "ERR_PROXY", "ERR_ADDRESS_UNREACHABLE",
    "ERR_EMPTY_RESPONSE", "ERR_SOCKET", "ERR_FAILED", "ERR_ABORTED", "CONNECTION REFUSED",
    "TIMEOUT", "ECONNREFUSED", "ENOTFOUND", "SOCKET HANG UP", "CONNECTION CLOSED",
)

from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

KEEPALIVE_URL = "https://i.chaoxing.com/"
KEEPALIVE_INTERVAL_S = 240


class BrowserSession:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self._last_keepalive = 0.0
        self.pw = sync_playwright().start()
        self.browser = self._launch()
        storage = Path(cfg.state_dir) / "storage_state.json"
        ctx_kwargs = dict(
            user_agent=UA,
            viewport={"width": int(cfg.browser.window_width), "height": int(cfg.browser.window_height)},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        if storage.exists():
            ctx_kwargs["storage_state"] = str(storage)
            log.info(f"已加载登录态文件: {storage}")
        self.context = self.browser.new_context(**ctx_kwargs)
        self.context.set_default_timeout(int(cfg.browser.page_timeout_ms))
        self.page = self.context.new_page()
        self.page.on("dialog", self._on_dialog)
        self._known_pages = {id(p) for p in self.context.pages}
        self.context.on("page", self._on_new_page)
        log.info(f"浏览器已启动 channel={self._channel_used} headless={cfg.browser.headless}")

    # ---------------- 启动 ----------------
    def _launch(self):
        channel = self.cfg.browser.channel
        order = {
            "auto": ["msedge", "chrome", "chromium"],
            "msedge": ["msedge"],
            "chrome": ["chrome"],
            "chromium": ["chromium"],
        }.get(channel, ["msedge", "chrome", "chromium"])
        args = ["--disable-blink-features=AutomationControlled", "--start-maximized"]
        last_err = None
        for ch in order:
            try:
                self._channel_used = ch
                return self.pw.chromium.launch(
                    channel=ch,
                    headless=bool(self.cfg.browser.headless),
                    slow_mo=int(self.cfg.browser.slow_mo),
                    args=args,
                )
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                self.log.warning(f"以 {ch} 启动失败: {str(exc)[:120]}")
        # 最后尝试不带 channel（playwright 自带 chromium）
        try:
            self._channel_used = "chromium-bundled"
            return self.pw.chromium.launch(
                headless=bool(self.cfg.browser.headless),
                slow_mo=int(self.cfg.browser.slow_mo),
                args=args,
            )
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(
                "浏览器启动失败。请安装 Edge 或 Chrome，或先执行: python -m playwright install chromium\n"
                f"最后错误: {exc}\n首次错误: {last_err}"
            )

    # ---------------- 事件 ----------------
    def _on_dialog(self, dialog):
        """自动接受 alert/confirm/beforeunload 弹窗（学习通常见的离开确认）。"""
        try:
            self.log.info(f"[页面对话框] {dialog.type}: {dialog.message[:60]}")
            dialog.accept()
        except Exception:  # noqa: BLE001
            pass

    def _on_new_page(self, page):
        self._known_pages.add(id(page))
        self.log.info(f"[新标签页] {page.url[:80]}")

    # ---------------- 登录态 ----------------
    @property
    def state_file(self):
        return Path(self.cfg.state_dir) / "storage_state.json"

    def state_exists(self):
        return self.state_file.exists()

    def save_state(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.context.storage_state(path=str(self.state_file))
        self.log.info(f"登录态已保存: {self.state_file.resolve()}")

    # ---------------- 新标签页捕获 ----------------
    # ---------------- 会话健康探测 ----------------
    def probe_session(self, timeout_ms=15000, url=None):
        """探测当前会话状态 → 'ok' / 'logged_out' / 'offline' / 'unknown'。

        - offline   ：网络层错误（ERR_INTERNET_DISCONNECTED 等），与登录无关
        - logged_out：被重定向到登录页/401/页面要求先登录（如手机端登录把电脑端顶下线）
        """
        url = url or "https://mooc1.chaoxing.com/visit/interaction"
        try:
            resp = self.context.request.get(url, timeout=timeout_ms, max_redirects=5)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).upper()
            if any(k in msg for k in NET_ERROR_MARKERS):
                return "offline"
            return "unknown"
        try:
            final = (resp.url or "").lower()
            if "passport" in final or "/login" in final:
                return "logged_out"
            if resp.status in (401, 403):
                return "logged_out"
            body = (resp.text() or "")[:6000]
            if any(k in body for k in ("请先登录", "请登录后", "登录超时", "账号在别处登录", "您的账号已在其他设备")):
                return "logged_out"
            if not any(c.get("name") in ("_uid", "UF") for c in self.context.cookies()):
                return "logged_out"
            return "ok"
        except Exception:  # noqa: BLE001
            return "unknown"

    def click_capture(self, page, element, wait_s=10):
        """点击元素；若打开新标签页则返回新页面，否则返回原页面。

        若有 humanizer，则用拟人化点击（鼠标轨迹 + 随机延时）。
        """
        before_url = page.url
        before = {id(p) for p in self.context.pages}
        try:
            if getattr(self, "humanizer", None) is not None:
                self.humanizer.click(page, element, timeout=10000)
            else:
                element.click(timeout=10000)
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"点击失败: {str(exc)[:100]}")
            return page, False
        deadline = time.time() + wait_s
        while time.time() < deadline:
            for p in self.context.pages:
                if id(p) not in before:
                    try:
                        p.bring_to_front()
                    except Exception:  # noqa: BLE001
                        pass
                    self.log.info(f"任务页已在新标签打开: {p.url[:80]}")
                    return p, True
            if page.url != before_url:
                return page, False
            time.sleep(0.2)
        return page, False

    def close_page(self, page):
        """关闭非主页面。"""
        try:
            if page and not page.is_closed() and id(page) != id(self.page):
                page.close()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 保活 ----------------
    def keep_alive(self, force=False):
        """周期性轻请求，维持会话 cookie 活跃。"""
        now = time.time()
        if not force and now - self._last_keepalive < KEEPALIVE_INTERVAL_S:
            return
        self._last_keepalive = now
        try:
            self.context.request.get(KEEPALIVE_URL, timeout=30000)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 收尾 ----------------
    def close(self):
        try:
            self.save_state()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.context.close()
            self.browser.close()
            self.pw.stop()
        except Exception:  # noqa: BLE001
            pass
