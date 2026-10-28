# -*- coding: utf-8 -*-
"""大模型答题（OpenAI 兼容接口）：DeepSeek / OpenAI / Kimi / 本地 Ollama 均可。

设计要点
- 只用标准库（urllib），不引入额外依赖；
- 强制"只输出 JSON"，解析失败重试一次；
- 答案会做**类型校验**（字母必须在选项范围内；判断题必须是对/错）；
- 有单次运行调用上限，避免异常情况下刷爆额度；
- Key 优先取配置，其次读环境变量（DEEPSEEK_API_KEY / OPENAI_API_KEY）。
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

SYSTEM_PROMPT = (
    "你是严谨的中国高校课程答题助手。你会收到一道题目与全部选项。\n"
    "请按下面的方式思考（思考过程不要输出）：\n"
    "1. 先找出题目在问什么，标出关键限定词（时间、主体、范围）。\n"
    "2. 逐项判断：这个选项为什么对/为什么错，错在哪。\n"
    "3. 若题目问'错误的是/不属于/不正确'，注意反向选择。\n"
    "4. 多个选项看似都对时，选'最准确、最完整'的那个；多选题则选全，别漏。\n"
    "5. 判断题注意绝对化措辞（所有/一定/必须/只要…就/任何）通常暗示表述有误；\n"
    "   但也要警惕'不能/不得/严禁'这类正确表述被误判。\n"
    "6. 如果确实拿不准，请如实给低 confidence，不要硬猜。\n"
    "只输出一个 JSON 对象，不要输出思考过程、解释文字或 Markdown 代码块。\n"
    'JSON 格式：{"answer": "<答案>", "confidence": <0到1的小数>, "reason": "<不超过30字的核心依据>"}'
)

TYPE_HINT = {
    "single": "题型：单选题。answer 只给一个选项字母（如 \"A\"）。",
    "multi": "题型：多选题。answer 给出**全部**正确选项字母并按字母序拼接（如 \"ABD\"）；漏选算错，不要只给一个。",
    "judge": "题型：判断题。answer 只能是 \"对\" 或 \"错\"。",
    "text": "题型：填空题。answer 直接给填空内容（不要带题号）。",
}


class LLMAnswer:
    def __init__(self, cfg, log):
        c = cfg.quiz.get("llm", {}) or {}
        self.log = log
        self.enabled = bool(c.get("enabled"))
        self.base_url = str(c.get("base_url") or "https://api.deepseek.com/v1").rstrip("/")
        self.api_key = str(c.get("api_key") or os.environ.get("DEEPSEEK_API_KEY")
                           or os.environ.get("OPENAI_API_KEY") or "")
        self.model = str(c.get("model") or "deepseek-flash")
        self.timeout = float(c.get("timeout_s") or 30)
        self.max_calls = int(c.get("max_calls") or 200)
        # K3 等推理模型固定了 temperature/top_p，显式传会被拒 → 配置为空就不发
        _t = c.get("temperature", None)
        self.temperature = float(_t) if _t not in (None, "") else None
        self.reasoning_effort = str(c.get("reasoning_effort") or "").strip()
        self.calls = 0
        self.hits = 0
        self.last_error = ""
        self.fatal = ""          # 遇到 401/402/403 这类"再试也没用"的错误后熔断

    # ---------------- 可用性 ----------------
    def available(self):
        if not self.enabled:
            return False, "未启用（config.yaml 的 quiz.llm.enabled 设为 true 后生效）"
        if not self.api_key:
            return False, "缺少 api_key（config.yaml 的 quiz.llm.api_key，或环境变量 DEEPSEEK_API_KEY）"
        if self.fatal:
            return False, f"已熔断，本轮不再调用：{self.fatal}"
        if self.max_calls and self.calls >= self.max_calls:
            return False, f"已达单次运行调用上限（{self.max_calls} 次）"
        return True, ""

    # ---------------- 主流程 ----------------
    def solve(self, stem, options, qtype="single"):
        """返回 (answer, confidence, reason) 或 (None, 0, 原因)。"""
        ok, why = self.available()
        if not ok:
            return None, 0.0, why
        prompt = self._build_prompt(stem, options, qtype)
        last_reason = ""
        for attempt in (1, 2):
            raw = self._chat(prompt)
            ans, conf, reason = self._parse(raw, options, qtype)
            if ans:
                self.hits += 1
                return ans, conf, reason
            last_reason = reason or last_reason
            if self.fatal:          # Key/余额问题，重试也是同样结果
                return None, 0.0, self.fatal
            if attempt == 1:
                self.log.info(f"  大模型返回不可用（第 1 次）：{reason or str(raw)[:50]}，重试一次")
                time.sleep(1.0)
        return None, 0.0, last_reason or "模型未给出可用答案"

    def _build_prompt(self, stem, options, qtype):
        lines = [TYPE_HINT.get(qtype, TYPE_HINT["single"]), "", f"【题目】{stem}"]
        if options:
            lines.append("【选项】")
            for i, o in enumerate(options):
                letter = o.get("letter") or chr(65 + i)
                lines.append(f"{letter}. {o.get('text') or ''}")
        lines.append("")
        lines.append("请只输出 JSON。")
        return "\n".join(lines)

    def _chat(self, prompt):
        self.calls += 1
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        if self.temperature is not None:          # 仅显式配置时才发送
            payload["temperature"] = self.temperature
        if self.reasoning_effort:                 # Kimi K3: low/high/max（默认 max，答题用 low 更快更省）
            payload["reasoning_effort"] = self.reasoning_effort
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            self.last_error = self._explain_http(exc.code, detail)
            self.log.warning(f"  大模型调用失败：{self.last_error}｜原始响应：{detail[:120]}")
            # 余额不足 / Key 无效 / 无权限：后面每道题都会同样失败，直接熔断，别再白撞
            if exc.code in (401, 402, 403):
                self.fatal = self.last_error
                self.log.warning("  已熔断：本轮剩余题目不再调用大模型，改走题库+搜题。"
                                 "（按 K 修好 Key/充值后重跑）")
        except Exception as exc:  # noqa: BLE001
            self.last_error = self._explain_conn(exc)
            self.log.warning(f"  大模型调用失败：{self.last_error}｜{str(exc)[:90]}")
        return ""

    @staticmethod
    def _explain_http(code, detail):
        """把常见 HTTP 错误翻译成可执行的建议。"""
        d = (detail or "").lower()
        if code == 401:
            return "Key 无效或未生效（检查 api_key 是否填对、是否多了空格）"
        if code == 402 or "insufficient balance" in d:
            return "账户余额不足 → 到 platform.deepseek.com 充值（充 10 元能用很久）"
        if code == 404:
            return "接口路径不对（检查 base_url 是否包含 /v1，模型名是否存在）"
        if code == 429:
            return "触发限流 → 稍后再试，或调小 max_calls"
        if code >= 500:
            return "服务端异常 → 稍后重试"
        return f"HTTP {code}"

    @staticmethod
    def _explain_conn(exc):
        msg = str(exc).lower()
        if "refused" in msg or "10061" in msg:
            return "连不上服务 → 检查 base_url；若用本地模型请先启动 Ollama（ollama serve）"
        if "timed out" in msg or "timeout" in msg:
            return "请求超时 → 检查网络，或调大 timeout_s"
        if "name or service" in msg or "getaddrinfo" in msg or "11001" in msg:
            return "域名解析失败 → 检查 base_url 拼写与网络"
        return "网络异常"

    def _parse(self, raw, options, qtype):
        """解析模型输出 → (answer, confidence, reason)。"""
        if not raw:
            return None, 0.0, ""
        txt = str(raw).strip()
        txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
        obj = None
        try:
            obj = json.loads(txt)
        except Exception:  # noqa: BLE001
            m = re.search(r"\{[\s\S]*\}", txt)
            if m:
                try:
                    obj = json.loads(m.group(0))
                except Exception:  # noqa: BLE001
                    obj = None
        if not isinstance(obj, dict):
            return None, 0.0, ""
        ans = str(obj.get("answer") or "").strip()
        try:
            conf = float(obj.get("confidence", 0.6))
        except Exception:  # noqa: BLE001
            conf = 0.6
        reason = str(obj.get("reason") or "")[:60]
        if not ans:
            return None, 0.0, reason

        n = len(options or [])
        if qtype == "judge":
            idx = 0 if re.search(r"对|正确|√|是|true|T", ans) and not re.search(r"错|不正确|不对|×|否|false|F", ans) else 1
            letter = None
            for i, o in enumerate(options or []):
                t = str(o.get("text") or "")
                if (idx == 0 and re.search(r"对|正确|√|是|true", t)) or \
                   (idx == 1 and re.search(r"错|不正确|不对|×|否|false", t)):
                    letter = o.get("letter") or chr(65 + i)
                    break
            if letter is None:
                letter = chr(65 + idx) if idx < n else None
            return (letter, conf, "判断题：" + ans) if letter else (None, 0.0, reason)

        letters = re.sub(r"[^A-Za-z]", "", ans).upper()
        if not letters or not n:
            return None, 0.0, reason
        if any(ord(c) - 64 > n for c in letters):
            return None, 0.0, f"模型给出的字母超出选项范围：{ans}"
        uniq = "".join(sorted(set(letters)))
        if qtype == "multi" and len(uniq) < 2:
            # 多选题只给了 1 个字母 → 可能是漏选，按低置信处理但仍返回
            return uniq, min(conf, 0.4), "多选题仅给出 1 项，可能漏选"
        return uniq, conf, reason

    def stats(self):
        return {"calls": self.calls, "hits": self.hits, "model": self.model}
