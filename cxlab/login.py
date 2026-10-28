# -*- coding: utf-8 -*-
"""登录：二维码 / 手机号密码 / 短信验证码；会话复用与校验。"""
import re
import time

LOGIN_URL = "https://passport2.chaoxing.com/login?fid=&newversion=true&refer=https%3A%2F%2Fi.chaoxing.com"
HOME_URL = "https://i.chaoxing.com/"


def _has_login_cookie(session):
    try:
        cookies = session.context.cookies()
        return any(c.get("name") in ("_uid", "UF") for c in cookies)
    except Exception:  # noqa: BLE001
        return False


def check_session(session):
    """用已保存的登录态请求首页，判断是否仍然有效。"""
    try:
        resp = session.context.request.get(HOME_URL, timeout=30000)
        url = str(resp.url)
        if "passport2.chaoxing.com" in url or "login" in url.lower():
            return False
        return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def ensure_login(session, cfg, log):
    if session.state_exists() and check_session(session):
        log.info("复用已保存的登录会话（无需重新登录）")
        return True
    if session.state_exists():
        log.warning("已保存的登录态已失效，需要重新登录")
    return do_login(session, cfg, log)


def _wait_login_success(session, timeout_s, log, tip="等待登录完成"):
    page = session.page
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            url = page.url
            if "i.chaoxing.com" in url and "passport2" not in url:
                if _has_login_cookie(session):
                    return True
            if _has_login_cookie(session) and "passport2" not in page.url:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    return False


def open_login_page(session, cfg, log):
    """打开登录页，失败自动重试（网络慢时首屏可能超过 15 秒）。"""
    page = session.page
    nav_timeout = int(cfg.login.nav_timeout_ms)
    retries = int(cfg.browser.nav_retry)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=nav_timeout)
            page.wait_for_timeout(2500)
            return True
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            log.warning(f"打开登录页失败（第 {attempt}/{retries} 次，超时 {nav_timeout}ms）: {str(exc)[:80]}")
            time.sleep(2)
    log.error(f"打开登录页最终失败: {last_err}")
    log.error("排查：1) 浏览器里手动打开 passport2.chaoxing.com 看能否访问；"
              "2) 若用了代理/VPN 先关掉；3) 把 config 里 login.nav_timeout_ms 调大到 120000")
    return False


def clear_session_cookies(session, log=None):
    """清掉失效 cookie，避免"半登录"状态导致反复跳登录页。"""
    try:
        session.context.clear_cookies()
        if log is not None:
            log.info("已清除失效的登录 cookie")
        return True
    except Exception as exc:  # noqa: BLE001
        if log is not None:
            log.warning(f"清除 cookie 失败: {str(exc)[:60]}")
        return False


def relogin(session, cfg, log):
    """掉线后自动重新登录（无人值守）。返回 True/False。

    顺序：① 配置了账号密码 → 密码登录（可无人值守）  ② 否则 → 扫码（需人工在浏览器扫码）
    """
    log.warning("=" * 60)
    log.warning("检测到电脑端已掉线（常见原因：手机端登录把电脑端顶下线）→ 开始自动重新登录")
    log.warning("=" * 60)
    clear_session_cookies(session, log)

    # ① 密码登录：无人值守首选
    if cfg.account.phone and cfg.account.password:
        for attempt in range(1, int(cfg.login.relogin_max_attempts) + 1):
            log.info(f"自动重登录（密码方式）第 {attempt}/{int(cfg.login.relogin_max_attempts)} 次")
            page = session.page
            try:
                if open_login_page(session, cfg, log) and _password_login(session, cfg, log):
                    log.info("自动重新登录成功")
                    session.save_state()
                    return True
            except Exception as exc:  # noqa: BLE001
                log.warning(f"密码登录异常: {str(exc)[:80]}")
            time.sleep(min(20 * attempt, 60))
        log.error("密码方式自动重登录失败")

    # ② 扫码兜底（需要人工）
    log.warning("请在浏览器中扫码登录（等待 %d 秒），扫码后可自动继续任务",
                int(cfg.login.qr_wait_s))
    try:
        if do_login(session, cfg, log):
            log.info("扫码重新登录成功，继续任务")
            return True
    except Exception as exc:  # noqa: BLE001
        log.warning(f"扫码登录失败: {str(exc)[:80]}")
    return False


def do_login(session, cfg, log):
    page = session.page
    method = cfg.account.login_method
    if not open_login_page(session, cfg, log):
        return False
    log.info(f"登录方式: {'二维码扫码' if method == 'qr' else '手机号+密码'}")

    if method == "qr":
        # 默认页通常已展示二维码；若没有，再尝试切到「二维码登录」入口
        qr_shown = False
        for sel in ['img[src*="qr"]', "[class*='qrcode']", "canvas"]:
            try:
                el = page.query_selector(sel)
                if el is not None and el.is_visible():
                    qr_shown = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if not qr_shown:
            for sel in ['text=二维码登录', 'text=扫码登录', '.qrcode-login']:
                try:
                    el = page.query_selector(sel)
                    if el is not None and el.is_visible():
                        el.click()
                        page.wait_for_timeout(1000)
                        break
                except Exception:  # noqa: BLE001
                    continue
        log.info(f"请用【学习通 APP】扫描浏览器中的二维码登录（最多等待 {cfg.login.qr_timeout_s} 秒）")
        ok = _wait_login_success(session, int(cfg.login.qr_timeout_s), log)
    else:
        ok = _password_login(session, cfg, log)

    if ok:
        log.info("登录成功")
        session.save_state()
        return True
    log.error("登录失败或超时")
    return False


def _password_login(session, cfg, log):
    page = session.page
    for attempt in range(1, int(cfg.login.max_retry) + 1):
        log.info(f"第 {attempt} 次尝试密码登录")
        try:
            phone_box = page.query_selector("#uname") or page.get_by_placeholder("手机号/超星号").first
            pwd_box = page.query_selector("#pwd") or page.get_by_placeholder("学习通密码").first
            if phone_box is None or pwd_box is None:
                log.error("未找到登录输入框（页面结构可能变化）")
                return False
            phone_box.fill(cfg.account.phone)
            page.wait_for_timeout(300)
            pwd_box.fill(cfg.account.password)
            page.wait_for_timeout(500)
            btn = page.query_selector("#loginBtn")
            if btn is None:
                log.error("未找到登录按钮")
                return False
            btn.click()
        except Exception as exc:  # noqa: BLE001
            log.error(f"登录表单操作失败: {exc}")
            return False

        # 登录后处理：验证码 / 短信 / 错误提示
        deadline = time.time() + 60
        while time.time() < deadline:
            if _wait_login_success(session, 1, log):
                return True
            # 滑块/图形验证码：暂停等人工
            try:
                cap = page.query_selector('#captcha, .nc_wrapper, [class*="captcha"], [id*="captcha"]')
                if cap and cap.is_visible():
                    log.warning("检测到验证码，请在浏览器中手动完成（程序等待中）")
                    if _wait_login_success(session, 180, log):
                        return True
            except Exception:  # noqa: BLE001
                pass
            # 短信验证码
            try:
                sms = page.query_selector('input[placeholder*="验证码"]')
                if sms and sms.is_visible():
                    code = cfg.account.sms_code
                    if not code:
                        try:
                            code = input("请输入手机收到的短信验证码后回车: ").strip()
                        except EOFError:
                            code = ""
                    if code:
                        sms.fill(code)
                        page.wait_for_timeout(300)
                        confirm = page.query_selector('button:has-text("确定"), button:has-text("确认"), #smsBtn')
                        if confirm:
                            confirm.click()
                    if _wait_login_success(session, 30, log):
                        return True
            except Exception:  # noqa: BLE001
                pass
            # 错误提示
            try:
                tip = page.query_selector('#loginTip, .error-tip, [class*="error"]')
                if tip and tip.is_visible():
                    msg = (tip.inner_text() or "").strip()[:60]
                    if msg:
                        log.warning(f"登录提示: {msg}")
                        break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)
    return False
