# -*- coding: utf-8 -*-
"""冒烟测试：真实启动浏览器 -> 打开学习通登录页 -> 校验关键选择器与会话检查逻辑。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cxlab.browser import BrowserSession  # noqa: E402
from cxlab.config import load_config  # noqa: E402
from cxlab.logutil import setup_logging  # noqa: E402
from cxlab import login as login_mod  # noqa: E402
from cxlab import course as course_mod  # noqa: E402
from cxlab import video as video_mod  # noqa: E402
from cxlab import document as document_mod  # noqa: E402
from cxlab import quiz as quiz_mod  # noqa: E402
from cxlab import homework as homework_mod  # noqa: E402
from cxlab import popups as popups_mod  # noqa: E402
from cxlab import runner as runner_mod  # noqa: E402
from cxlab import report as report_mod  # noqa: E402
from cxlab import answers as answers_mod  # noqa: E402

print("[1] 模块导入 OK")

cfg = load_config(str(ROOT / "config.yaml"))
cfg["browser"]["headless"] = True          # 冒烟测试用无头
cfg["browser"]["channel"] = "auto"
log = setup_logging(cfg.log_dir, cfg.log_level)
print("[2] 配置加载 OK ->", cfg.course.name_contains)
print("    路径解析检查 output_dir =", cfg.output_dir)
print("    路径解析检查 state_dir  =", cfg.state_dir)
assert str(cfg.output_dir).startswith(str(ROOT)), "output_dir 未解析到项目目录"
assert str(cfg.state_dir).startswith(str(ROOT)), "state_dir 未解析到项目目录"

LOGIN_URL = login_mod.LOGIN_URL
session = BrowserSession(cfg, log)
print("[3] 浏览器启动 OK ->", session._channel_used)

page = session.page
page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
page.wait_for_timeout(3000)
print("[4] 打开登录页 OK ->", page.url[:70])

checks = {
    "#pwd (密码框)": page.query_selector("#pwd"),
    "#loginBtn (登录按钮)": page.query_selector("#loginBtn"),
    "placeholder 手机号/超星号": page.get_by_placeholder("手机号/超星号").first,
    "#captcha (验证码容器)": page.query_selector("#captcha"),
}
for name, el in checks.items():
    print(f"    {name}: {'命中' if el is not None else '未找到'}")

# 会话检查（未登录应返回 False，且不抛异常）
valid = login_mod.check_session(session)
print("[5] 未登录时会话检查返回:", valid, "（应为 False）")

# 状态接口（未登录情况下应优雅降级，不抛异常）
info = course_mod.CourseInfo(course_id="1", clazzid="1", cpi="1")
st = course_mod.chapter_status(session, info, ["1"], log)
print("[6] 章节状态接口未登录时返回:", st, "（应为空且未抛异常）")

# 页面结构导出（验证 JS 可用）
out = ROOT / "output" / "smoke_structure.txt"
course_mod.dump_structure(page, str(out), log)
print("[7] 页面结构导出:", out.exists(), out.stat().st_size if out.exists() else 0, "bytes")

png = ROOT / "output" / "smoke_login.png"
page.screenshot(path=str(png))
print("[8] 截图已保存:", png.exists())

session.close()
print("[OK] 冒烟测试完成")
