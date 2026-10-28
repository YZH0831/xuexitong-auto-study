# -*- coding: utf-8 -*-
"""弹窗与风控看门狗：自动关闭已知弹窗，遇验证码/风控页暂停等待人工处理。"""
import time

RISK_URL_KEYS = ["antispider", "verify", "captcha", "riskcontrol", "9010"]
DIALOG_SELECTORS = [
    ".layui-layer",
    '[class*="dialog"]',
    '[class*="modal"]',
    '[role="dialog"]',
    ".popbox",
    ".notice",
    "#notice",
]
# 只点这些语义的安全按钮，绝不碰提交类确认
CLOSE_TEXTS = ["我知道了", "知道了", "继续学习", "稍后再说", "暂不", "关闭", "取消", "忽略", "跳过", "以后再说"]
DANGER_TEXTS = ["提交", "交卷", "确认提交", "确定放弃"]
RISK_WAIT_S = 900

# 单次 JS 调用完成弹窗探测（替代逐个选择器的 CDP 往返）
JS_DIALOGS = r"""
() => {
  const sels = ['.layui-layer','[class*="dialog"]','[class*="modal"]','[role="dialog"]','.popbox','.notice','#notice'];
  const out = [];
  for (const s of sels) {
    document.querySelectorAll(s).forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.width < 10 || r.height < 10) return;
      const text = (el.innerText || '').trim().slice(0, 120);
      if (!text) return;
      out.push({ text: text, hasInput: !!el.querySelector('input,textarea,select') });
    });
  }
  return out.slice(0, 12);
}
"""


class RiskControlStop(Exception):
    """遇到风控页且 safety.stop_on_risk=True 时抛出，用于优雅中止运行。"""


class PopupGuard:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.risk_events = 0
        self._last_check = 0.0
        try:
            self.check_interval = float(self.cfg.popup.check_interval_s or 5)
        except Exception:  # noqa: BLE001
            self.check_interval = 5.0

    def tick(self, page):
        """等待循环中调用：风控页判定（零成本，基于 URL）+ 低频弹窗探测。"""
        try:
            url = page.url or ""
        except Exception:  # noqa: BLE001
            return
        low = url.lower()
        if self.cfg.popup.pause_on_antispider and any(k in low for k in RISK_URL_KEYS):
            self._wait_manual(page, url)
            return
        if not self.cfg.popup.auto_close:
            return
        now = time.time()
        if now - self._last_check < self.check_interval:
            return                      # 降频：默认 5 秒探测一次，弹窗出现后仍会被捕获
        self._last_check = now
        self._close_known(page)

    # ---------------- 风控页 ----------------
    def _wait_manual(self, page, url):
        self.risk_events += 1
        try:
            stop = bool(self.cfg.safety.stop_on_risk)
        except Exception:  # noqa: BLE001
            stop = False
        if stop:
            self.log.error("检测到风控/验证页面，且已配置「遇风控立即停止」→ 中止本次运行")
            raise RiskControlStop(f"遇到风控/验证页面: {url[:80]}")
        self.log.warning("=" * 60)
        self.log.warning(f"检测到风控/验证页面: {url[:100]}")
        self.log.warning("程序已暂停 —— 请在浏览器窗口中人工完成验证（例如滑块/人脸识别）")
        self.log.warning(f"完成后程序会自动继续，最长等待 {RISK_WAIT_S // 60} 分钟")
        self.log.warning("=" * 60)
        deadline = time.time() + RISK_WAIT_S
        while time.time() < deadline:
            time.sleep(3)
            try:
                cur = (page.url or "").lower()
            except Exception:  # noqa: BLE001
                return
            if not any(k in cur for k in RISK_URL_KEYS):
                self.log.info("验证已完成，程序继续")
                return
        raise TimeoutError("等待人工处理风控页面超时（15 分钟），已中止该任务")

    # ---------------- 已知弹窗 ----------------
    def _close_known(self, page):
        """单次 JS 探测弹窗；仅当确实存在弹窗时才做点击（避免无谓 CDP 往返）。"""
        try:
            candidates = page.evaluate(JS_DIALOGS) or []
        except Exception:  # noqa: BLE001
            return
        for c in candidates:
            text = (c.get("text") or "").strip()
            if not text:
                continue
            if c.get("hasInput"):
                continue          # 含输入框的弹窗（登录、验证码、答题）一律不碰
            if any(t in text for t in DANGER_TEXTS):
                self.log.info(f"[弹窗] 保留不点（含提交/交卷语义）: {text[:40]}")
                continue
            if self._click_close(page):
                self.log.info(f"[弹窗] 已自动关闭: {text[:40]}")

    @staticmethod
    def _click_close(page):
        for t in CLOSE_TEXTS:
            for sel in (f'button:has-text("{t}")', f'a:has-text("{t}")'):
                try:
                    el = page.query_selector(sel)
                    if el is not None and el.is_visible():
                        el.click(timeout=3000)
                        return True
                except Exception:  # noqa: BLE001
                    continue
        try:
            el = page.query_selector('[class*="close"], [aria-label="关闭"]')
            if el is not None and el.is_visible():
                el.click(timeout=3000)
                return True
        except Exception:  # noqa: BLE001
            pass
        return False
