# -*- coding: utf-8 -*-
"""学习通刷课持续监控：定期体检 + 异常桌面弹窗（只读监测，不改任何配置/BAT）。

用法：
    python tools/monitor.py [--interval 300] [--max-hours 9] [--once]

检查项（每 interval 秒一次）：
  1. 主进程是否存活（wmic 查 main.py）
  2. 最新运行日志是否在推进（停滞 > 10 分钟判为卡住）
  3. 日志中的异常特征（掉线/需人工/断网/风控/进程消失/正常结束）
  4. 平台进度（解析"平台进度：已完成任务点 X/Y"）
异常 → 桌面弹窗（情况/原因/建议），同类告警 15 分钟内不重复弹。
"""
import argparse
import ctypes
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
MB_OK = 0x0
MB_ICONWARNING = 0x30
MB_ICONINFO = 0x40

MB_ICONWARNING = 0x30


def _ts():
    return datetime.now().strftime("%H:%M:%S")


class Logger:
    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8")

    def __call__(self, msg):
        line = f"{_ts()} {msg}"
        print(line, flush=True)
        self.f.write(line + "\n")
        self.f.flush()


# ---------------- 弹窗 ----------------
_last_popup = {}


def popup(title, body, icon=MB_ICONWARNING, cooldown=900):
    """桌面弹窗（同类告警冷却期内不重复弹）。"""
    key = title[:24]
    now = time.time()
    if now - _last_popup.get(key, 0) < cooldown:
        return False
    _last_popup[key] = now
    try:
        ctypes.windll.user32.MessageBoxW(0, f"{title}\n\n{body}", "学习通刷课监控", MB_OK | icon)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------- 检测 ----------------
def latest_run_log():
    runs = sorted(LOGS.glob("run_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return runs[0] if runs else None


def main_process_alive():
    """判断刷课主进程是否还在。

    Windows 11 24H2 起 wmic 已被移除，所以优先用 tasklist 统计
    "除自己以外"的 python 进程数（自己就是监控进程，必须排除）。
    """
    import os
    my_pid = str(os.getpid())
    try:
        out = subprocess.run(["tasklist", "/fi", "imagename eq python.exe", "/fo", "csv", "/nh"],
                             capture_output=True, text=True, timeout=20)
        rows = [r for r in (out.stdout or "").splitlines() if "python" in r.lower()]
        others = []
        for r in rows:
            cols = [c.strip('"') for c in r.split('","')]
            pid = cols[1].strip('"') if len(cols) > 1 else ""
            if pid and pid != my_pid:
                others.append(pid)
        return len(others) > 0
    except Exception:  # noqa: BLE001
        return True   # 查不到就当作还活着，避免误告警


def recent_lines(path, minutes=20):
    """只取最近 N 分钟内的日志行（避免把 20 分钟前的旧告警当成当前状态）。"""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001
        return ""
    now = datetime.now()
    out = []
    for ln in reversed(lines):
        m = re.match(r"^(\d{2}):(\d{2}):(\d{2})", ln)
        if m:
            hh, mm, ss = (int(x) for x in m.groups())
            t = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
            if t > now:
                t = t.replace(day=t.day - 1)          # 跨午夜
            if (now - t).total_seconds() > minutes * 60:
                break
        out.append(ln)
    return "\n".join(reversed(out))


def current_chapter(txt):
    m = re.findall(r"开始处理：(.+)", txt)
    return m[-1].strip()[:40] if m else ""


def check(log, state):
    """返回本周期发现的告警列表 [(标题, 正文, 图标)]。"""
    alerts = []
    now = time.time()
    age = now - log.stat().st_mtime
    txt = recent_lines(log, minutes=20)
    alive = main_process_alive()
    state["progress"] = state.get("progress", "")

    # 1) 结束（区分正常跑完 / 中途掉线）
    if "运行结束" in txt:
        early = re.search(r"TargetClosed|has been closed|ERR_INTERNET|ERR_CONNECTION", txt)
        m_prog = re.findall(r"平台当前进度：已完成任务点 (\d+)/(\d+)", txt) \
            or re.findall(r"平台进度：已完成任务点 (\d+)/(\d+)", txt)
        prog = f"{m_prog[-1][0]}/{m_prog[-1][1]}" if m_prog else "?"
        if early:
            alerts.append((
                "【任务中途中断】浏览器被关闭或网络断开",
                f"【情况】程序已退出，但平台进度只有 {prog}（未跑完）。\n"
                f"【原因】日志出现 TargetClosed / 连接中断 —— 通常是浏览器窗口被关掉。\n"
                f"【建议】重新双击 BAT 即可续跑（已完成的会自动跳过）；挂机期间请不要关闭浏览器窗口。",
                MB_ICONWARNING))
            state["finished"] = True
            return alerts
        done = re.findall(r"平台当前进度：已完成任务点 (\d+)/(\d+)", txt)
        prog = f"{done[-1][0]}/{done[-1][1]}" if done else state["progress"]
        alerts.append((
            "【运行结束】刷课任务已收工",
            f"平台进度：{prog}\n\n【情况】程序已正常结束并输出汇总。\n"
            f"【建议】打开 output 目录查看《完成报告》；下次运行会自动跳过已完成内容。\n"
            f"（本次监控随之退出，如需再监测请重新启动）",
            MB_ICONINFO))
        state["finished"] = True
        return alerts

    # 2) 进程消失但日志没结束 → 异常退出
    if not alive:
        alerts.append((
            "【进程异常退出】主程序不在了，但日志没有结束标记",
            "【情况】检测不到 main.py 进程，且日志缺少“运行结束”。\n"
            "【可能原因】程序崩溃 / 被手动关闭 / 电脑休眠。\n"
            "【建议】重新双击桌面 BAT 续跑；已完成的课时会自动跳过，不会重做。",
            MB_ICONWARNING))
        state["finished"] = True
        return alerts

    # 3) 被顶下线 / 需重新登录
    if re.search(r"(电脑端已掉线|自动重新登录|登录失败|passport2\.chaoxing)", txt[-4000:]):
        alerts.append((
            "【掉线】电脑端被顶下线或登录失效",
            "【情况】日志出现掉线/重新登录特征。\n"
            "【原因】学习通单端登录：手机端登录会把电脑端顶下线；或会话过期。\n"
            "【建议】若配置了账号密码会自动恢复；否则请到电脑前扫码一次，程序会接着跑。",
            MB_ICONWARNING))

    # 4) 需要人工
    if re.search(r"(题库未命中|待人工|请手动|手工作答)", txt[-4000:]):
        alerts.append((
            "【需要人工】有题目双源都没搜到答案",
            "【情况】测验中有题目未能自动作答，正在等你人工作答。\n"
            "【建议】到浏览器窗口里手动选一下，答完在控制台按回车即可提交；"
            "答过的题会自动进题库，以后不再问。",
            MB_ICONWARNING))

    # 5) 网络中断
    if len(re.findall(r"ERR_INTERNET_DISCONNECTED|ERR_CONNECTION", txt[-4000:])) >= 3:
        alerts.append((
            "【网络中断】连续出现连接失败",
            "【情况】日志多次出现 ERR_INTERNET_DISCONNECTED / ERR_CONNECTION。\n"
            "【建议】检查 Wi-Fi/网线；程序会按 15/30/60/120/300 秒退避重试，恢复后自动续跑。",
            MB_ICONWARNING))

    # 6) 风控/验证码（排除"搜索引擎返回验证码/空页"这类引擎限流提示，那不算平台风控）
    risk_lines = [ln for ln in txt.splitlines()
                  if re.search(r"(风控|验证码|风险控制|操作异常)", ln)
                  and not re.search(r"(疑似被限流|返回验证码/空页|联网搜题失败)", ln)]
    if risk_lines:
        alerts.append((
            "【风控告警】检测到验证码/风险控制页",
            "【情况】平台弹出验证码或风控页。\n"
            "【建议】尽快到电脑前人工处理一次；连续出现建议暂停 30 分钟再跑。",
            MB_ICONWARNING))

    # 7) 引擎限流（短时间多次）
    if len(re.findall(r"疑似被限流", txt[-4000:])) >= 3:
        alerts.append((
            "【搜索引擎被限流】多个引擎返回验证码",
            "【情况】通用搜索通道连续被限流。\n"
            "【影响】仅影响“题库未命中时的搜题”，视频播放不受影响。\n"
            "【建议】等待 10~30 分钟自动恢复；或把 config.yaml 的 engine_order 换一换。",
            MB_ICONWARNING))

    # 8) 日志停滞
    if age > 600:
        alerts.append((
            "【疑似卡住】日志超过 10 分钟没有更新",
            f"【情况】最新日志 {int(age/60)} 分钟无新增。\n"
            "【可能原因】卡在人工等待（题库未命中）/ 网络重试退避 / 页面加载异常。\n"
            "【建议】到电脑前看一眼浏览器窗口：若在等人工作答，答完按回车；"
            "若持续无响应，可关闭后重新运行（进度不丢）。",
            MB_ICONWARNING))

    # 进度跟踪（有变化就记录，不弹窗）
    m = re.findall(r"平台进度：已完成任务点 (\d+/\d+)", txt)
    if m and m[-1] != state.get("progress"):
        state["progress"] = m[-1]
    where = f"\n【当前知识点】{current_chapter(txt)}" if current_chapter(txt) else ""
    return [(t, b + where, i) for t, b, i in alerts]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--max-hours", type=float, default=9)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()

    LOGS.mkdir(exist_ok=True)
    log = Logger(LOGS / f"monitor_{datetime.now():%Y%m%d}.log")
    state = {}
    deadline = time.time() + a.max_hours * 3600
    log(f"===== 持续监控启动 | 每 {a.interval}s 一检 | 最长 {a.max_hours} 小时 | 只读监测 =====")

    while time.time() < deadline:
        run = latest_run_log()
        if run is None:
            log("未找到运行日志（可能还没开始跑）")
        else:
            alerts = check(run, state)
            age = int(time.time() - run.stat().st_mtime)
            log(f"体检 {run.name} | 日志 {age}s 前更新 | 平台进度 {state.get('progress') or '?'}"
                f" | 进程 {'在' if main_process_alive() else '不在'}")
            for title, body, icon in alerts:
                ok = popup(title, body, icon)
                log(f"  弹窗{'✓' if ok else '×（冷却中/失败）'} {title}")
            if state.get("finished"):
                log("监控目标已结束，退出监控")
                return
        if a.once:
            return
        time.sleep(a.interval)
    log("到达最长监控时长，退出（如需继续请重新启动）")


if __name__ == "__main__":
    main()
