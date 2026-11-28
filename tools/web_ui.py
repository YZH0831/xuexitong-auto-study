# -*- coding: utf-8 -*-
"""学习通自动化助手 - 本地 Web 控制台后端（零第三方依赖）。

只监听 127.0.0.1；命令白名单；不暴露任意路径读写。
    .venv\\Scripts\\python.exe -u tools\\web_ui.py
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
LOGS = ROOT / "logs"
OUTPUT = ROOT / "output"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
CONFIG = ROOT / "config.yaml"

# pythonw 下父进程没有控制台，子进程必须显式禁止创建新控制台，否则会弹出黑框
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _win_flags() -> dict:
    """Windows 下隐藏子进程的控制台窗口（非 Windows 返回空参数）。"""
    return {"creationflags": NO_WINDOW} if os.name == "nt" else {}


def _child_env() -> dict:
    """强制子进程输出 UTF-8。

    pythonw 没有控制台，子进程的 stdout 会回落到系统 GBK(cp936)；
    中文课程名再按 UTF-8 解码就会变乱码。设这两个变量后全链路统一 UTF-8。
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _decode_out(data) -> str:
    """兼容 UTF-8（新）与 GBK（旧环境 / 漏设变量）两种子进程输出。"""
    if isinstance(data, str):
        return data
    try:
        s = data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("gbk", "replace")
    if s.count("\ufffd") > 3:
        try:
            return data.decode("gbk", "replace")
        except UnicodeDecodeError:
            pass
    return s

HTML = TOOLS / "web_ui.html"
CONSOLE_LOG = LOGS / "webui_console.log"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOCK = threading.Lock()
START_LOCK = threading.Lock()   # 串行化 /api/run，防止连点同时起两个刷课进程
PROC: subprocess.Popen | None = None
PROC_CMD = ""
PROC_START = 0.0
SERVER: ThreadingHTTPServer | None = None
LAST_HIT = time.time()
APP_TAG = "cx-console"
WINDOW_TITLE = "学习通自动化助手 · 控制台"
IDLE_EXIT_S = 30 * 60

EDGE_CANDIDATES = (
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
)


def fix_stdio():
    """pythonw.exe 下 sys.stdout/stderr 是 None，重定向到启动日志，避免 print 崩溃。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        LOGS.mkdir(exist_ok=True)
        f = open(LOGS / "webui_boot.log", "a", encoding="utf-8", errors="replace")
    except OSError:
        class _Null:
            def write(self, *a):
                return 0

            def flush(self):
                pass
        f = _Null()
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


def touch():
    global LAST_HIT
    LAST_HIT = time.time()


def edge_path() -> Path | None:
    for p in EDGE_CANDIDATES:
        if p and p.exists():
            return p
    return None


def focus_window() -> bool:
    """已有应用窗口就唤到前台，而不是再开一个。"""
    try:
        import ctypes
        from ctypes import wintypes

        u = ctypes.windll.user32
        found = None

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _each(hwnd, _):
            nonlocal found
            n = u.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, buf, n + 1)
                if WINDOW_TITLE in buf.value:
                    found = hwnd
                    return False
            return True

        u.EnumWindows(_each, 0)
        if not found:
            found = u.FindWindowW(None, WINDOW_TITLE)
        if not found:
            return False
        u.ShowWindow(found, 9)
        u.SetForegroundWindow(found)
        try:
            u.SwitchToThisWindow(found, True)
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception:  # noqa: BLE001
        return False


def wait_for_window(timeout: float = 4.0) -> bool:
    """等首个实例把窗口拉起来：服务端口先就绪，窗口要晚一点才出现。"""
    end = time.time() + timeout
    while time.time() < end:
        if focus_window():
            return True
        time.sleep(0.15)
    return False


_LOCAL_URL_RE = re.compile(r"^http://127\.0\.0\.1:(\d{2,5})$")
_APP_LAUNCH_LOCK = threading.Lock()
_LAST_APP_LAUNCH = 0.0
_APP_DEBOUNCE_S = 2.0


def safe_console_url(url: str) -> str:
    """只接受本控制台自己的 127.0.0.1 地址，杜绝把浏览器导向任何外部页面。"""
    u = (url or "").strip()
    m = _LOCAL_URL_RE.fullmatch(u)
    if not m:
        return ""
    port = int(m.group(1))
    return u if 8765 <= port <= 8784 else ""


def open_app_window(url: str, wait_s: float = 0.0, allow_launch: bool = True) -> bool:
    """打开（或聚焦）控制台窗口。

    ★ 2026-11-28 加固「连点 bat 弹出浏览器里的学习通」：
      1. 地址必须是 127.0.0.1:8765-8784，否则一律不开浏览器；
      2. 抢锁失败的实例只允许聚焦已有窗口（allow_launch=False），绝不自行
         启动 Edge —— 首个实例自己会开窗，连点不会再冒出第二个浏览器；
      3. 删掉 webbrowser.open 兜底：默认浏览器会恢复上次会话（学习通）；
      4. 2 秒内的重复开窗请求直接忽略，挡住同一 Edge profile 连发两次。
    """
    global _LAST_APP_LAUNCH
    u = safe_console_url(url)
    if not u:
        print(f"拒绝打开浏览器：目标不是合法的控制台地址 {url!r}")
        return False
    if wait_s and wait_for_window(wait_s):
        return True
    if focus_window():
        return True
    if not allow_launch:
        return False
    with _APP_LAUNCH_LOCK:
        now = time.time()
        if now - _LAST_APP_LAUNCH < _APP_DEBOUNCE_S:
            return False
        _LAST_APP_LAUNCH = now
    exe = edge_path()
    if exe:
        try:
            subprocess.Popen([str(exe), f"--app={u}", "--window-size=1460,960",
                              "--no-first-run", "--no-default-browser-check"])
            return True
        except OSError:
            pass
    # 不再调用 webbrowser.open：默认浏览器可能恢复上次会话，表现为"自动弹出学习通"
    return False


def find_running() -> int:
    """已有同项目控制台在跑就返回端口（按项目根目录校验，避免认错其它项目）。"""
    me = str(ROOT).lower()
    for port in range(8765, 8785):  # 与 pick_port() / safe_console_url() 保持一致
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=0.3) as r:
                d = json.loads(r.read().decode("utf-8"))
                if d.get("app") == APP_TAG and str(d.get("root", "")).lower() == me:
                    return port
        except Exception:  # noqa: BLE001
            continue
    return 0


_MUTEX_HANDLE = None
_MUTEX_NAME = "Local\\CXConsole_" + hashlib.md5(
    str(ROOT).encode("utf-8", "ignore")).hexdigest()[:16]


def acquire_instance_mutex() -> bool:
    """进程级单实例锁：连点两次时后启动的进程直接切到已有窗口。

    ★ 2026-11-25 修复：原来用 ctypes.windll + kernel32.GetLastError()，
      但 ctypes.windll 默认不保留 Win32 last-error —— GetLastError() 恒为 0，
      于是 ERROR_ALREADY_EXISTS(183) 永远读不到，锁形同虚设，连点两次照样
      开第二个控制台（实测 6 个 pythonw 进程、多个端口）。
      正确做法：WinDLL(..., use_last_error=True) + ctypes.get_last_error()，
      并显式声明 argtypes/restype，避免 64 位下句柄/字符串被截断。
    """
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateMutexW.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        ctypes.set_last_error(0)
        h = k.CreateMutexW(None, False, _MUTEX_NAME)
        err = ctypes.get_last_error()
        if not h:
            return True  # 拿不到锁就退回端口探测逻辑
        if err == 183:  # ERROR_ALREADY_EXISTS：已有实例持有锁
            k.CloseHandle(h)
            return False
        _MUTEX_HANDLE = h   # 保持句柄存活到进程退出，锁才不会提前释放
        return True
    except Exception:  # noqa: BLE001
        return True


def wait_running_port(timeout: float = 8.0) -> int:
    """等另一个实例把服务端口绑好，避免它还没起来就误判为没有实例。"""
    end = time.time() + timeout
    while True:
        port = find_running()
        if port:
            return port
        if time.time() >= end:
            return 0
        time.sleep(0.2)


def idle_watchdog():
    """窗口关掉后没人访问，30 分钟自动回收后台服务。"""
    while True:
        time.sleep(60)
        with LOCK:
            alive = PROC is not None and PROC.poll() is None
        if alive:
            touch()
            continue
        if time.time() - LAST_HIT > IDLE_EXIT_S and SERVER is not None:
            print("空闲超时，控制台自动退出")
            SERVER.shutdown()
            return

# 命令白名单：前端只能传这里的 key
CMDS = {
    "dry": ["--dry-run"],
    "run": [],
    "video": ["--only", "video"],
    "document": ["--only", "document"],
    "quiz": ["--only", "quiz"],
    "list": ["--list-courses"],
    "pick": ["--pick"],
}
CMD_LABEL = {
    "dry": "试运行", "run": "正式运行", "video": "只刷视频/音频",
    "document": "只处理文档", "quiz": "只做测验", "list": "列出课程", "pick": "交互选课",
}


def newest_run_log() -> Path | None:
    fs = glob.glob(str(LOGS / "run_*.log"))
    return max((Path(f) for f in fs), key=lambda p: p.stat().st_mtime) if fs else None


def tail_text(path: Path | None, n: int = 240) -> list[str]:
    if not path or not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except OSError:
        return []


def external_active() -> bool:
    """最近 150 秒内有刷课日志写入，且不是本服务启动的进程。"""
    with LOCK:
        ours_alive = PROC is not None and PROC.poll() is None
    if ours_alive:
        return False
    p = newest_run_log()
    return bool(p) and (time.time() - p.stat().st_mtime) < 150


def parse_progress(lines: list[str]) -> dict:
    text = "".join(lines[-120:])
    m = None
    for m in re.finditer(r"进度\s+(\d+)/(\d+)s\s*\((\d+)%\)", text):
        pass
    if m:
        return {"cur": int(m.group(1)), "total": int(m.group(2)), "pct": int(m.group(3))}
    return {"cur": 0, "total": 0, "pct": 0}


def version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "1.2.4"
    except OSError:
        return "1.1.3"


def status_payload() -> dict:
    with LOCK:
        alive = PROC is not None and PROC.poll() is None
        pid = PROC.pid if alive else None
        cmd = PROC_CMD if alive else ""
        rc = PROC.poll() if PROC is not None else None
        started = PROC_START if alive else 0.0
    log = newest_run_log()
    lines = tail_text(log, 120)
    return {
        "running": alive,
        "pid": pid,
        "cmd": cmd,
        "label": CMD_LABEL.get(cmd, cmd),
        "started": started,
        "last_rc": rc,
        "external": external_active(),
        "version": version(),
        "log_file": log.name if log else "",
        "log_mtime": log.stat().st_mtime if log else 0,
        "progress": parse_progress(lines),
        "port": SERVER.server_address[1] if SERVER else 0,
    }


def start_job(cmd: str, force: bool = False, select: str = "", lessons: str = "") -> tuple[bool, str]:
    global PROC, PROC_CMD, PROC_START
    if cmd not in CMDS:
        return False, "未知命令"
    if not PY.exists():
        return False, "运行环境缺失，请先运行 setup.bat"
    # 连点「运行」按钮时两个 HTTP 线程会同时走到这里。原来只在 LOCK 里检查
    # PROC、却在外面才赋值，检查与赋值之间存在窗口，两个请求都能通过 →
    # 起两个 main.py、两个 Playwright 浏览器（表现为又弹出一个学习通）。
    # 用 START_LOCK 把「检查→建日志→拉起→登记」整段串行化。
    with START_LOCK:
        with LOCK:
            if PROC is not None and PROC.poll() is None:
                return False, "已有任务在运行"
        if not force and external_active():
            return False, "检测到外部刷课实例正在运行，可能触发多终端顶号"
        LOGS.mkdir(exist_ok=True)
        try:
            fh = CONSOLE_LOG.open("w", encoding="utf-8", errors="replace")
        except OSError as e:
            return False, f"无法写控制台日志：{e}"
        argv = [str(PY), "-u", "main.py", "--config", "config.yaml"] + CMDS[cmd]
        if select.strip():
            argv += ["--select", select.strip()]
        if lessons.strip():
            argv += ["--lessons", lessons.strip()]
        try:
            kw = {"cwd": str(ROOT), "stdin": subprocess.PIPE,
                  "stdout": fh, "stderr": subprocess.STDOUT,
                  "env": _child_env()}
            if os.name == "nt":
                kw["creationflags"] = (NO_WINDOW
                                       | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
                kw["encoding"] = "utf-8"
            p = subprocess.Popen(argv, **kw)
        except OSError as e:
            fh.close()
            return False, f"启动失败：{e}"
        with LOCK:
            PROC, PROC_CMD, PROC_START = p, cmd, time.time()
    return True, f"已启动：{CMD_LABEL[cmd]}"


def stop_job() -> tuple[bool, str]:
    with LOCK:
        p = PROC
    if p is None or p.poll() is not None:
        return False, "当前没有由控制台启动的任务"
    try:
        if os.name == "nt":
            # Windows：terminate() 本身就是硬杀（不会走 Python 的清理逻辑），
            # 且杀不掉 Playwright 拉起的浏览器子进程 → taskkill 连树清理，
            # 防浏览器野进程残留。旧文案"已发送停止指令"与行为不符，一并纠正。
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(p.pid)],
                capture_output=True, timeout=15, creationflags=NO_WINDOW)
        else:
            p.terminate()
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
        return True, "已强制结束任务（连同其拉起的浏览器进程）"
    except OSError as e:
        return False, f"停止失败：{e}"


def run_doctor() -> str:
    try:
        r = subprocess.run([str(PY), "-u", "tools/doctor.py"], cwd=str(ROOT),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=45, env=_child_env(), **_win_flags())
        return _decode_out(r.stdout)
    except subprocess.TimeoutExpired:
        return "环境自检超时（45 秒）"
    except OSError as e:
        return f"无法运行自检：{e}"


COURSES_CACHE = ROOT / "state" / "courses_cache.json"
COURSES_TTL_S = 3600          # 缓存 1 小时；过期自动重拉，避免新加的课读不到
COURSE_RE = re.compile(r"^\s*\[\s*([A-Za-z0-9]+)\]\s+(.+?)\s{2,}\[(.+?)\]\s*(.*)$")
LOG_COURSE_RE = re.compile(r"^\s*\[([^\]]{2,10})\]\s+(.+?)\s+[—-]\s+(.+)$")
COURSE_STATUS = {"可处理", "状态未知", "已结束", "已退课", "已过截止日期", "未开课"}
LOG_PREFIX_RE = re.compile(r"^\S+\s+\[(?:INFO|WARNING|ERROR|DEBUG|CRITICAL)\]\s*")


def candidates_from_log() -> list[dict]:
    """从最近的运行日志里提取课程清单；最新日志还没写到课程段就往前翻。"""
    files = sorted((Path(f) for f in glob.glob(str(LOGS / "run_*.log"))),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:8]
    for p in files:
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        out, seen = [], set()
        for raw in lines:
            line = LOG_PREFIX_RE.sub("", raw)
            m = LOG_COURSE_RE.match(line)
            if not m:
                continue
            status, name, reason = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
            if status not in COURSE_STATUS or not name or name in seen or len(name) > 40:
                continue
            seen.add(name)
            out.append({"label": "", "name": name, "status": status,
                        "reason": reason, "course_id": ""})
        if out:
            return out[:40]
    return []



def parse_courses(text: str) -> list[dict]:
    """解析 main.py --list-courses 的输出为结构化课程列表。"""
    items: list[dict] = []
    cur = None
    for line in text.splitlines():
        m = COURSE_RE.match(line)
        if m:
            cur = {"label": m.group(1), "name": m.group(2).strip(),
                   "status": m.group(3).strip(), "reason": m.group(4).strip(),
                   "course_id": ""}
            items.append(cur)
            continue
        m2 = re.search(r"courseId=(\S+)", line)
        if m2 and cur is not None:
            cur["course_id"] = m2.group(1)
    return items


def load_courses() -> dict:
    try:
        d = json.loads(COURSES_CACHE.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("items"), list) and d["items"]:
            d.setdefault("source", "cache")
            return d
    except (OSError, ValueError):
        pass
    items = candidates_from_log()
    return {"ts": 0, "items": items, "source": "log" if items else "",
            "need_refresh": True}


def fetch_courses(force: bool = False) -> tuple[bool, str, list[dict]]:
    if external_active() and not force:
        return False, "刷课实例正在运行，拉取课程要再开一个浏览器，容易触发多终端顶号。等这一轮跑完再拉。", []
    try:
        r = subprocess.run([str(PY), "-u", "main.py", "--config", "config.yaml", "--list-courses"],
                           cwd=str(ROOT), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=240,
                           env=_child_env(), **_win_flags())
        text = _decode_out(r.stdout)
    except subprocess.TimeoutExpired:
        return False, "拉取超时（240 秒），请重试", []
    except OSError as e:
        return False, f"无法运行：{e}", []
    items = parse_courses(text)
    if not items:
        return False, "没有解析到课程，请确认已登录学习通（可能需要扫码）", []
    try:
        COURSES_CACHE.parent.mkdir(exist_ok=True)
        COURSES_CACHE.write_text(json.dumps({"ts": time.time(), "items": items},
                                            ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return True, f"已获取 {len(items)} 门课程", items


_REFRESH_LOCK = threading.Lock()
_last_refresh_try = 0.0


def maybe_refresh_courses() -> bool:
    """缓存缺失/过期时后台拉一次真实课程清单；5 分钟内最多一次，不阻塞页面。"""
    global _last_refresh_try
    now = time.time()
    with _REFRESH_LOCK:
        if now - _last_refresh_try < 300:
            return False
        _last_refresh_try = now

    def _work():
        try:
            fetch_courses(force=False)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_work, daemon=True).start()
    return True


def open_target(target: str) -> tuple[bool, str]:
    if target == "logs":
        path = LOGS
    elif target == "output":
        path = OUTPUT
    elif target == "key":
        path = ROOT / "SetDeepSeekKey.bat"
    elif target == "tiku":
        path = ROOT / "SetTiku.bat"
    elif target.startswith("report:"):
        name = Path(target.split(":", 1)[1]).name
        path = OUTPUT / name
        if not path.exists() or path.parent != OUTPUT:
            return False, "报告不存在"
    else:
        return False, "不允许的目标"
    if not path.exists():
        return False, "目标不存在"
    try:
        os.startfile(str(path))  # noqa: S606 - 本地控制台，白名单目录
        return True, "已打开"
    except OSError as e:
        return False, f"打开失败：{e}"


class Handler(BaseHTTPRequestHandler):
    server_version = "CXConsole/1.0"

    def log_message(self, fmt, *args):
        pass  # 静默，避免刷屏

    def _check_host(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "::1")

    def _check_fetch(self) -> bool:
        """拦浏览器跨站 POST：网页里的 no-cors 表单也能打到本地端口，
        会越权启动任务/改写配置。Sec-Fetch-Site 主流浏览器全量发送，
        缺失视为老客户端放行（same-origin=本页，none=地址栏直接打开）。
        """
        fs = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        return fs in ("", "same-origin", "same-site", "none")

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 2_000_000:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        touch()
        if not self._check_host():
            self._json({"error": "forbidden host"}, 403)
            return
        p = self.path.split("?", 1)[0]
        if p == "/api/ping":
            self._json({"app": APP_TAG, "root": str(ROOT),
                        "port": SERVER.server_address[1] if SERVER else 0})
            return
        if p in ("/", "/index.html"):
            try:
                self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"web_ui.html missing", "text/plain; charset=utf-8")
            return
        if p == "/api/status":
            self._json(status_payload())
            return
        if p == "/api/logs":
            n = 240
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            for kv in q.split("&"):
                if kv.startswith("n="):
                    try:
                        n = max(20, min(800, int(kv[2:])))
                    except ValueError:
                        pass
            log = newest_run_log()
            self._json({"file": log.name if log else "",
                        "lines": tail_text(log, n),
                        "console": tail_text(CONSOLE_LOG, 40) if CONSOLE_LOG.exists() else []})
            return
        if p == "/api/config":
            try:
                self._json({"ok": True, "text": CONFIG.read_text(encoding="utf-8")})
            except OSError as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return
        if p == "/api/courses":
            d = load_courses()
            ts = float(d.get("ts") or 0)
            stale = (not ts) or (time.time() - ts > COURSES_TTL_S)
            refreshing = False
            if stale and not external_active():
                refreshing = maybe_refresh_courses()
            self._json({"items": d.get("items") or [], "ts": ts,
                        "source": d.get("source") or "",
                        "stale": bool(stale), "refreshing": refreshing,
                        "need_refresh": bool(d.get("need_refresh"))})
            return
        if p == "/api/reports":
            items = []
            if OUTPUT.exists():
                for f in sorted(OUTPUT.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)[:30]:
                    items.append({"name": f.name, "size": f.stat().st_size,
                                  "mtime": f.stat().st_mtime})
            self._json({"items": items})
            return
        if p == "/api/version":
            self._json({"version": version()})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        touch()
        if not self._check_host():
            self._json({"error": "forbidden host"}, 403)
            return
        if not self._check_fetch():
            self._json({"error": "cross-site rejected"}, 403)
            return
        p = self.path.split("?", 1)[0]
        data = self._body()
        if p == "/api/run":
            ok, msg = start_job(str(data.get("cmd", "")), bool(data.get("force")),
                                str(data.get("select") or ""), str(data.get("lessons") or ""))
            self._json({"ok": ok, "message": msg}, 200 if ok else 409)
            return
        if p == "/api/courses/fetch":
            ok, msg, items = fetch_courses(bool(data.get("force")))
            self._json({"ok": ok, "message": msg, "items": items}, 200 if ok else 409)
            return
        if p == "/api/stop":
            ok, msg = stop_job()
            self._json({"ok": ok, "message": msg})
            return
        if p == "/api/input":
            text = str(data.get("text", ""))
            with LOCK:
                proc = PROC
            if proc is None or proc.poll() is not None or proc.stdin is None:
                self._json({"ok": False, "message": "没有可接收输入的任务"}, 409)
                return
            try:
                proc.stdin.write(text + "\n")
                proc.stdin.flush()
                self._json({"ok": True})
            except (OSError, ValueError) as e:
                self._json({"ok": False, "message": str(e)}, 409)
            return
        if p == "/api/doctor":
            self._json({"ok": True, "text": run_doctor()})
            return
        if p == "/api/config":
            text = data.get("text")
            if not isinstance(text, str):
                self._json({"ok": False, "message": "内容为空"}, 400)
                return
            try:
                if CONFIG.exists() and not (ROOT / "config.yaml.webbak").exists():
                    (ROOT / "config.yaml.webbak").write_bytes(CONFIG.read_bytes())
                CONFIG.write_text(text, encoding="utf-8")
                self._json({"ok": True, "message": "已保存，原配置备份为 config.yaml.webbak"})
            except OSError as e:
                self._json({"ok": False, "message": str(e)}, 500)
            return
        if p == "/api/open":
            ok, msg = open_target(str(data.get("target", "")))
            self._json({"ok": ok, "message": msg})
            return
        if p == "/api/shutdown":
            self._json({"ok": True, "message": "控制台服务已关闭"})
            if SERVER:
                threading.Thread(target=SERVER.shutdown, daemon=True).start()
            return
        self._json({"error": "not found"}, 404)


def pick_port(start: int = 8765) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("8765-8784 端口都被占用")


def main() -> int:
    global SERVER
    fix_stdio()
    LOGS.mkdir(exist_ok=True)
    if not PY.exists():
        print("找不到 .venv\\Scripts\\python.exe，请先运行 setup.bat")
        return 1
    quiet = "--no-browser" in sys.argv
    if not acquire_instance_mutex():
        print("已有控制台实例正在启动或运行，正在切换到已有窗口")
        if not quiet:
            # 抢锁失败 = 我只是连点产生的多余进程：只等窗口出现并聚焦，
            # 绝不自己启动浏览器。开窗由持锁的首个实例负责，
            # 这样连点再快也不会冒出第二个 Edge / 默认浏览器会话。
            if not wait_for_window(3.0):
                print("另一个实例尚未就绪或窗口未出现，请稍后再点一次")
        return 0
    running = find_running()
    if running:
        url = f"http://127.0.0.1:{running}"
        print(f"控制台已在运行：{url}")
        if not quiet:
            open_app_window(url, wait_s=4.0, allow_launch=False)
        return 0
    port = pick_port()
    SERVER = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print("=" * 58)
    print("  学习通自动化助手 · 控制台已启动")
    print(f"  地址：{url}")
    print("=" * 58)
    touch()
    threading.Thread(target=idle_watchdog, daemon=True).start()
    if not quiet:
        threading.Timer(0.7, lambda: open_app_window(url)).start()
    else:
        print("  --no-browser：不自动打开窗口")
    try:
        SERVER.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        pass
    finally:
        SERVER.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
