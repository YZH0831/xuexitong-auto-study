# -*- coding: utf-8 -*-
"""第三方题库接口（比网页搜索准、快，而且不触发搜索风控）。

支持的 provider（可配置多个，按顺序尝试，命中即止）：

  enncy        言溪题库  https://tk.enncy.cn/query   （GET，需免费 token）
  icodef       GO题/网课小工具 https://cx.icodef.com/wyn-nb?v=4 （POST 表单）
  tikuadapter  自建 https://github.com/.../tikuAdapter（POST JSON，可聚合多题库）
  custom       任意兼容 tikuAdapter 响应格式的自建接口

为什么值得接：本项目早期的答题链路是「本地题库 → 大模型 → 网页搜索 → 兜底猜」，
网页搜索实测只有 60%~80% 的命中率，而且 bing/sogou 很容易被限流；
题库接口是**结构化返回**（自带答案字母/答案文本），命中率通常 90%+，还不占搜索配额。

安全设计：
- 只发题干和选项，不发账号信息；
- 单个 provider 失败不影响其它 provider；
- 连续失败 3 次自动熔断该 provider（本轮不再试），避免每次都等超时。
"""
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 我方题型 → 题库通用题型编号（tikuAdapter 规范：0单选 1多选 2填空 3判断 4问答）
TYPE_CODE = {"single": 0, "multi": 1, "text": 2, "judge": 3}

DEFAULT_URLS = {
    "enncy": "https://tk.enncy.cn/query",
    "icodef": "https://cx.icodef.com/wyn-nb?v=4",
}

_LETTER_RE = re.compile(r"^[A-Ha-h]{1,8}$")
_PREFIX_RE = re.compile(r"^\s*(?:正确)?答案\s*[:：是为]?\s*")


def _clean_answer(raw):
    """把 "答案：A" / "A" / "特殊侵权责任" 归一成 (答案, 'letter'|'text')。"""
    s = str(raw or "").strip()
    if not s:
        return None, ""
    s = _PREFIX_RE.sub("", s).strip()
    s = s.strip("。.、，,；; 　")
    if not s:
        return None, ""
    if _LETTER_RE.match(s):
        return s.upper(), "letter"
    return s, "text"


class Tiku:
    """题库查询器。一次 run 内复用，自带调用上限与熔断。"""

    def __init__(self, cfg, log):
        c = (cfg.quiz.get("tiku") or {}) if hasattr(cfg, "quiz") else {}
        self.log = log
        self.enabled = bool(c.get("enabled"))
        self.timeout = float(c.get("timeout_s") or 12)
        self.max_calls = int(c.get("max_calls") or 300)
        self.min_len = int(c.get("min_stem_len") or 6)
        self.providers = [p for p in (c.get("providers") or [])
                          if isinstance(p, dict) and p.get("type")]
        self.calls = 0
        self.hits = 0
        self._fail = {}          # provider type -> 连续失败次数
        self.fatal = ""
        self.auto_start = bool(c.get("auto_start", True))
        self._local_start_attempted = False

    # ---------------- 状态 ----------------
    def available(self):
        if not self.enabled:
            return False, "未启用（config.yaml 的 quiz.tiku.enabled）"
        if not self.providers:
            return False, "未配置 providers"
        if self.fatal:
            return False, self.fatal
        if self.max_calls and self.calls >= self.max_calls:
            return False, f"已达单次运行调用上限（{self.max_calls} 次）"
        return True, ""

    def describe(self):
        return "、".join(str(p.get("type")) for p in self.providers) or "（空）"

    # ---------------- HTTP ----------------
    def _http(self, url, *, data=None, headers=None, method=None, timeout=None):
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        parsed = urllib.parse.urlparse(url)
        # 本机服务必须绕过系统 HTTP 代理；否则服务未启动时得到的是代理 502，
        # 既掩盖了“连接失败”，也会让自动拉起永远不触发。
        if parsed.hostname in ("127.0.0.1", "localhost", "::1"):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=timeout or self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    # ---------------- 各 provider ----------------
    def _ask_enncy(self, p, stem, options, qtype):
        url = p.get("url") or DEFAULT_URLS["enncy"]
        params = {
            "token": p.get("token") or "",
            "title": stem[:120],
            "options": "\n".join((o.get("text") or "").strip()
                                 for o in options if (o.get("text") or "").strip()),
            "type": TYPE_CODE.get(qtype, 0),
        }
        body = self._http(url + "?" + urllib.parse.urlencode(params),
                          headers={"User-Agent": "Mozilla/5.0"})
        obj = json.loads(body)
        if obj.get("code") not in (0, "0"):
            return None, ""
        ans = ((obj.get("data") or {}).get("answer")) or ""
        return _clean_answer(ans)

    def _ask_icodef(self, p, stem, options, qtype):
        url = p.get("url") or DEFAULT_URLS["icodef"]
        form = {"question": stem[:300], "type": TYPE_CODE.get(qtype, 0)}
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0",
            "Authorization": p.get("token") or "",
        }
        body = self._http(url, data=urllib.parse.urlencode(form).encode(),
                          headers=headers, method="POST")
        obj = json.loads(body)
        if obj.get("code") not in (1, "1"):
            return None, ""
        ans = ((obj.get("data") or {}).get("answer")) or ""
        return _clean_answer(ans)

    def _start_local_adapter(self, base):
        """本地 tikuAdapter 未启动时自动拉起，避免用户手工开服务。"""
        if not self.auto_start or self._local_start_attempted:
            return False
        parsed = urllib.parse.urlparse(base or "")
        if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            return False
        self._local_start_attempted = True
        port = parsed.port or 8060
        exe = Path(__file__).resolve().parent.parent / "tools" / "tikuAdapter" / "tikuAdapter.exe"
        if not exe.exists():
            self.log.warning(f"  本地题库服务未启动，且未找到 {exe}")
            return False
        self.log.info("  本地题库服务未启动 → 自动启动 tikuAdapter…")
        try:
            flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                     | getattr(subprocess, "DETACHED_PROCESS", 0))
            with (exe.parent / "service.auto.log").open("ab") as out:
                subprocess.Popen(
                    [str(exe)], cwd=str(exe.parent), stdin=subprocess.DEVNULL,
                    stdout=out, stderr=subprocess.STDOUT, creationflags=flags,
                )
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"  自动启动本地题库服务失败: {str(exc)[:100]}")
            return False
        host = "127.0.0.1" if parsed.hostname in ("localhost", "::1") else parsed.hostname
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.8):
                    self.log.info(f"  本地题库服务已就绪: http://{host}:{port}")
                    return True
            except OSError:
                time.sleep(0.5)
        self.log.warning("  本地题库服务启动超时（15 秒），本次改走其它答题链路")
        return False

    def _ask_adapter(self, p, stem, options, qtype):
        """tikuAdapter / custom：统一 POST JSON，响应里取 answerKeyText / answerText。"""
        base = (p.get("url") or "").rstrip("/")
        if p.get("type") == "tikuadapter":
            base = base or "http://127.0.0.1:8060"
            url = base + "/adapter-service/search"
        else:
            url = base
        params = {}
        if p.get("use"):
            params["use"] = str(p["use"])
        for key in ("icodefToken", "enncyToken", "wannengToken", "aidianYToken",
                    "lemonToken", "tikuhaiToken"):
            if p.get(key):
                params[key] = str(p[key])
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        payload = {
            "question": stem[:300],
            "options": [(o.get("text") or "").strip() for o in options
                        if (o.get("text") or "").strip()],
            "type": TYPE_CODE.get(qtype, 0),
        }
        try:
            body = self._http(url, data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json",
                                       "User-Agent": "Mozilla/5.0"}, method="POST")
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            # 只有本地服务连接失败才尝试拉起；远端 502/超时不在这里启动进程。
            if not self._start_local_adapter(base):
                raise
            body = self._http(url, data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json",
                                       "User-Agent": "Mozilla/5.0"}, method="POST")
        obj = json.loads(body)
        ans = obj.get("answer")
        if isinstance(ans, dict):
            for key in ("answerKeyText", "answerText"):
                got, kind = _clean_answer(ans.get(key))
                if got:
                    return got, kind
            best = ans.get("bestAnswer")
            if isinstance(best, list) and best:
                return _clean_answer("#".join(str(x) for x in best))
        return _clean_answer(ans if isinstance(ans, str) else "")

    # ---------------- 对外 ----------------
    def query(self, stem, options, qtype="single"):
        """查一道题。返回 (答案, 'letter'|'text', provider名, 备注) 或 (None, '', '', 原因)。"""
        ok, why = self.available()
        if not ok:
            return None, "", "", why
        stem = (stem or "").strip()
        if len(stem) < self.min_len:
            return None, "", "", "题干过短，跳过题库"

        for p in self.providers:
            ptype = str(p.get("type") or "").strip().lower()
            if self._fail.get(ptype, 0) >= 3:      # 该 provider 连续失败 → 本轮熔断
                continue
            if ptype == "enncy":
                fn = self._ask_enncy
            elif ptype == "icodef":
                fn = self._ask_icodef
            elif ptype in ("tikuadapter", "custom"):
                fn = self._ask_adapter
            else:
                continue
            # 多 provider 回退也共享同一额度，必须在每次真实请求前检查。
            if self.max_calls and self.calls >= self.max_calls:
                return None, "", "", f"已达单次运行题库调用上限（{self.max_calls} 次）"
            self.calls += 1
            try:
                ans, kind = fn(p, stem, options, qtype)
                if ans and qtype == "single" and kind == "letter":
                    letters = set(re.findall(r"[A-Za-z]", ans.upper()))
                    if len(letters) != 1:
                        self.log.warning(f"  题库[{ptype}] 单选返回多个答案 {ans} → 不采用，继续下一源")
                        continue
                if ans:
                    self.hits += 1
                    self._fail[ptype] = 0
                    return ans, kind, ptype, ""
                self._fail[ptype] = 0               # 请求成功但没答案：不算失败
            except urllib.error.HTTPError as exc:
                self._fail[ptype] = self._fail.get(ptype, 0) + 1
                self.log.warning(f"  题库[{ptype}] HTTP {exc.code}（连续 {self._fail[ptype]} 次）")
            except Exception as exc:  # noqa: BLE001
                self._fail[ptype] = self._fail.get(ptype, 0) + 1
                self.log.warning(f"  题库[{ptype}] 查询失败：{type(exc).__name__}"
                                 f"（连续 {self._fail[ptype]} 次）")
            if self._fail.get(ptype, 0) >= 3:
                self.log.warning(f"  题库[{ptype}] 连续失败 3 次 → 本轮不再使用")
            time.sleep(float(p.get("interval_s") or 0))
        return None, "", "", "所有题库均未命中"
