# -*- coding: utf-8 -*-
"""自动续跑守护（autopilot）：主程序崩溃/浏览器被关时自动重新拉起。

为什么需要：学习通的进度在平台侧，工具本身是幂等的（已完成的节点自动跳过），
所以"中断 → 重新运行"是安全的。本守护把这件事自动化，避免挂机时白等。

运行：
    .venv\\Scripts\\python.exe tools\\autopilot.py --select 某课程甲

停止：
    删除项目根目录下的 STOP.autopilot 文件，或 Ctrl+C

规则：
    - 跑完（日志含"运行结束"且无中断特征）→ 弹窗告知并退出
    - 中断（TargetClosed / 连接断开 / 异常退出）→ 等 delay 秒后重启
    - 连续 max_restarts 次重启，或连续 4 次进度不涨 → 弹窗告警并退出
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
PY = ROOT / ".venv" / "Scripts" / "python.exe"
LOG_DIR = ROOT / "logs"
STOP_FILE = ROOT / "STOP.autopilot"
MB_ICONWARNING = 0x30
MB_ICONINFO = 0x40
INTERRUPT_MARKS = ("TargetClosed", "has been closed", "ERR_INTERNET", "ERR_CONNECTION",
                   "Browser closed", "连接已关闭")


def log(msg):
    line = f"{datetime.now():%H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_DIR / f"autopilot_{datetime.now():%Y%m%d}.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def popup(title, body, icon=MB_ICONWARNING):
    try:
        ctypes.windll.user32.MessageBoxW(0, f"{title}\n\n{body}", "学习通自动续跑守护",
                                         icon)
    except Exception:  # noqa: BLE001
        pass


def latest_log():
    runs = sorted(LOG_DIR.glob("run_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return runs[0] if runs else None


def read_tail(path, n=800):
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:  # noqa: BLE001
        return ""


def progress_of(path):
    txt = read_tail(path, 1200)
    m = re.findall(r"平台(?:当前)?进度：已完成任务点 (\d+)/(\d+)", txt)
    if m:
        return int(m[-1][0]), int(m[-1][1])
    m2 = re.findall(r"平台状态（接口）：待完成 (\d+) / 已完成 (\d+)", txt)
    if m2:
        return int(m2[-1][1]), int(m2[-1][1]) + int(m2[-1][0])
    return -1, -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--select", default="某课程甲")
    ap.add_argument("--delay", type=int, default=25)
    ap.add_argument("--max-restarts", type=int, default=15)
    a = ap.parse_args()

    if STOP_FILE.exists():
        STOP_FILE.unlink()
    log(f"===== 守护启动 | 课程={a.select} | 中断后 {a.delay}s 自动重启 | 最多 {a.max_restarts} 次 =====")
    restarts = 0
    best = -1
    stagnant = 0

    while True:
        if STOP_FILE.exists():
            log("检测到 STOP.autopilot，守护退出")
            return 0
        log(f"启动主程序（第 {restarts + 1} 次）")
        try:
            code = subprocess.call([str(PY), "main.py", "--config", "config.yaml",
                                    "--select", a.select], cwd=str(ROOT))
        except KeyboardInterrupt:
            log("收到 Ctrl+C，守护退出")
            return 0
        except Exception as exc:  # noqa: BLE001
            log(f"启动失败: {exc}")
            code = 0

        lg = latest_log()
        if lg is None:
            log("没有找到运行日志，退出")
            return 1
        txt = read_tail(lg)
        done, total = progress_of(lg)
        interrupted = any(k in txt for k in INTERRUPT_MARKS)
        finished = "运行结束" in txt

        log(f"主程序退出 code={code} | 进度 {done}/{total} | 中断特征={interrupted} | 正常结束={finished}")

        if finished and not interrupted:
            popup("【任务完成】刷课已跑完",
                  f"平台进度：{done}/{total}\n\n已正常结束，无需人工处理。\n"
                  f"（守护进程退出）", MB_ICONINFO)
            return 0
        if total > 0 and done >= total:
            popup("【任务完成】全部任务点已完成",
                  f"平台进度：{done}/{total}", MB_ICONINFO)
            return 0

        # 中断 → 判断是"偶发"还是"反复原地打转"
        restarts += 1
        if done > best:
            best, stagnant = done, 0
        else:
            stagnant += 1
        if restarts > a.max_restarts or stagnant >= 4:
            popup("【守护告警】反复中断，已停止自动重启",
                  f"【情况】已连续重启 {restarts} 次，进度 {done}/{total}，最近 {stagnant} 次没有增长。\n"
                  f"【可能原因】浏览器被反复关闭 / 网络不稳定 / 平台要求验证码。\n"
                  f"【建议】检查浏览器窗口是否被人为关闭；人工打开学习通确认是否需要验证码；"
                  f"处理后重新运行守护脚本即可。")
            return 2

        log(f"{a.delay}s 后自动重启（第 {restarts} 次重启）…")
        time.sleep(a.delay)


if __name__ == "__main__":
    sys.exit(main())
