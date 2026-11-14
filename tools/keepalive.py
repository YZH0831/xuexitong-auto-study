# -*- coding: utf-8 -*-
"""无人值守守护：主程序退出后按退避策略自动续跑，运行期间阻止系统空闲睡眠。

由项目根目录的 keepalive.bat 启动，一般不用手动执行。
停止：在项目根目录执行  keepalive.bat stop
日志：logs/keepalive.log
"""
from __future__ import annotations

import ctypes
import hashlib
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "keepalive.log"
STOP_FILE = ROOT / "state" / "keepalive.stop"

# 自动识别项目，决定每轮跑什么
if (ROOT / "tools" / "study_ai.py").exists():
    TAG = "U校园"
    CMD = [str(PY), "-u", "tools/study_ai.py",
           "--minutes", "600", "--passes", "2", "--book", "vls"]
else:
    TAG = "学习通"
    CMD = [str(PY), "-u", "main.py", "--config", "config.yaml"]

GAP_NORMAL = 30           # 正常结束后休息秒数
GAP_SESSION = 1800        # 检出会话被踢/熔断后的冷却（30 分钟）
FAST_EXIT_S = 120         # 少于 2 分钟视为快速退出
COOLDOWNS = [60, 300, 900, 1800, 3600]
MAX_FAST = 5              # 连续快速退出达到此数就停机，避免无限空转
SESSION_MARKS = ("多终端登录", "会话不可用", "暂停本轮", "登录态失效")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
CREATE_NO_WINDOW = 0x08000000

_MUTEX_HANDLE = None


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    with LOG_FILE.open("a", encoding="utf-8", errors="replace") as f:
        f.write(line + "\n")
    try:
        print(line, flush=True)
    except Exception:
        pass  # pythonw 下没有控制台


def single_instance() -> bool:
    """同一项目只允许一个守护；False 表示已有实例在运行。"""
    global _MUTEX_HANDLE
    k = ctypes.windll.kernel32
    name = "Local\\wb_keepalive_" + hashlib.md5(
        str(ROOT).encode("utf-8")).hexdigest()[:16]
    k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    k.CreateMutexW.restype = ctypes.c_void_p
    _MUTEX_HANDLE = k.CreateMutexW(None, False, name)
    return k.GetLastError() != 183


def wait_until(end: float) -> bool:
    """分段等待，期间可响应停止信号；返回是否收到停止。"""
    while time.time() < end:
        if STOP_FILE.exists():
            return True
        time.sleep(min(2.0, max(0.0, end - time.time())))
    return STOP_FILE.exists()


def session_broken(since: float) -> bool:
    """本轮日志里是否出现会话被踢 / 熔断（用于拉长冷却，避免频繁顶号）。"""
    try:
        cands = [f for f in LOG_DIR.glob("run_*.log")
                 if f.stat().st_mtime >= since - 5]
        if not cands:
            return False
        f = max(cands, key=lambda x: x.stat().st_mtime)
        tail = "\n".join(
            f.read_text(encoding="utf-8", errors="replace").splitlines()[-80:])
        return any(m in tail for m in SESSION_MARKS)
    except Exception:
        return False


def stop_child(p) -> None:
    try:
        p.terminate()
        p.wait(timeout=10)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def main() -> int:
    if not single_instance():
        log(f"{TAG} 守护已在运行，忽略本次启动")
        return 0
    if not PY.exists():
        log("缺少 .venv/Scripts/python.exe，请先运行 setup.bat 安装环境")
        return 1
    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STOP_FILE.exists():
        STOP_FILE.unlink()

    k = ctypes.windll.kernel32
    k.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    log(f"=== {TAG} 守护启动 | {ROOT} ===")
    log(f"每轮命令: {' '.join(CMD[2:])}")

    fast = 0
    rnd = 0
    try:
        while True:
            if STOP_FILE.exists():
                break
            rnd += 1
            t0 = time.time()
            log(f"第 {rnd} 轮开始")
            try:
                p = subprocess.Popen(
                    CMD, cwd=str(ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception as exc:
                log(f"启动失败：{type(exc).__name__}: {exc}，60 秒后重试")
                if wait_until(time.time() + 60):
                    break
                continue

            stopping = False
            while p.poll() is None:
                if STOP_FILE.exists():
                    log("收到停止信号，结束当前任务…")
                    stop_child(p)
                    stopping = True
                    break
                time.sleep(2)
            rc = p.poll()
            dur = time.time() - t0
            log(f"第 {rnd} 轮结束 rc={rc} 用时 {dur / 60:.1f} 分钟")
            if stopping:
                break

            if session_broken(t0):
                fast = 0
                wait = GAP_SESSION
                log(f"检出会话被踢/熔断，冷却 {wait // 60} 分钟再续跑，避免频繁顶号触发风控")
            elif dur < FAST_EXIT_S:
                fast += 1
                if fast >= MAX_FAST:
                    log(f"连续 {fast} 次快速退出，判定无任务可做或环境异常，守护自动停机")
                    break
                wait = COOLDOWNS[min(fast - 1, len(COOLDOWNS) - 1)]
                log(f"快速退出 ({fast}/{MAX_FAST})，{wait} 秒后重试")
            else:
                fast = 0
                wait = GAP_NORMAL

            if wait_until(time.time() + wait):
                break
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出")
    finally:
        k.SetThreadExecutionState(ES_CONTINUOUS)  # 恢复系统默认睡眠策略
        if STOP_FILE.exists():
            try:
                STOP_FILE.unlink()
            except Exception:
                pass
        log(f"=== {TAG} 守护退出，共运行 {rnd} 轮 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
