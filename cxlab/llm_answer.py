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
import urllib.error
import urllib.request

SYSTEM_PROMPT = (
    "你是严谨的中国高校课程答题助手，会收到一道题、全部选项，题型在用户消息中给出。\n"
    "在内部完成分析与核对，不要输出思考过程：\n"
    "1. 读题定标：通读题干与全部选项，标出关键限定词（时间/主体/范围/程度）和隐含限制，"
    "判断题型、考点和题目真正所问；'错误的是/不属于/不正确'按反向选择。\n"
    "2. 多解交叉验证：不局限于单一解法——先独立推导，再逐项核对每个选项对在哪、错在哪；"
    "多个表述都像对时，选最准确、最完整的那个。\n"
    "3. 按题型作答：严格遵循用户消息给出的题型要求；多选选全不漏，判断留意绝对化措辞"
    "（所有/一定/必须/只要…就/任何）常有误，但'不能/不得/严禁'这类正确表述别误判。\n"
    "4. 交付前核对：逐步检查关键步骤、结果与答案格式——字母是否在选项范围内、"
    "多选是否按字母序、判断是否用'对/错'、填空是否带多余内容，杜绝臆测与漏答。\n"
    "5. 不确定就核查：拿不准时如实给低 confidence，不要硬猜；"
    "程序会据此判断是否需要唤起你再次复核。有把握才给高置信。\n"
    "质量优先：把准确率放在第一位，只给可直接使用的结论。\n"
    "只输出一个 JSON 对象，不输出思考过程、解释文字或 Markdown 代码块。\n"
    'JSON 格式：{"answer": "<答案>", "confidence": <0到1的小数>, "reason": "<不超过30字的核心依据>"}'
)

# 二次复核专用提示词：换个"独立复核员"视角重新作答，避免第一次的思维定势。
RECHECK_SYSTEM_PROMPT = (
    "你是课程答题的独立复核员。你只会看到一道题和选项，请以完全独立的视角重新作答。\n"
    "方法：先逐字读题干，找限定词与反向词（错误/不属于/不正确），再逐项排除，最后才给结论。"
    "不要揣测出题人偏好，只依据题干和常识。信息不足或存在歧义时如实给低 confidence。\n"
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
        # 二次复核（程序自主判断触发）：开关 + 触发阈值 + 额外调用预算
        self.recheck_enabled = bool(c.get("recheck_enabled", True))
        self.recheck_below = float(c.get("recheck_below", 0.80) or 0)
        self.max_rechecks = int(c.get("max_rechecks", 40) or 0)
        self.rechecks = 0
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
        """作答 + 程序自主判断是否需要二次唤起 DeepSeek 复核。

        返回 (answer, confidence, reason) 或 (None, 0, 原因)。
        两次独立作答一致 → 提置信采纳；冲突 → 降置信，交上层联网搜题仲裁。
        """
        ans, conf, reason = self._solve_once(stem, options, qtype)
        if not ans:
            return ans, conf, reason
        # ★ 程序自己判断：该题值不值得再唤起一次 DeepSeek
        need, why = self._should_recheck(conf, reason, ans, qtype)
        if not need:
            return ans, conf, reason
        self.log.info(f"  二次复核触发（{why}）→ 再唤起 DeepSeek 独立重推一次")
        ans2, conf2, reason2 = self._recheck(stem, options, qtype)
        if not ans2:                # 复核调用失败 → 保留首答，但降置信交上层兜底
            return ans, min(conf, self.recheck_below - 0.01), reason + "（复核未成功）"
        same = self._same(ans, ans2, qtype)
        if same:
            boosted = max(conf, conf2, self.recheck_below + 0.05)
            self.log.info(f"  二次复核一致 → {ans}，置信 {conf:.2f}→{boosted:.2f}")
            return ans, boosted, f"两次独立作答一致：{reason}"
        # 冲突：不擅自选边，降置信让上层联网双源仲裁（证据强于单模型）
        self.log.warning(f"  二次复核冲突：首答 {ans} / 复核 {ans2} → 降置信交联网仲裁")
        return ans, min(conf, self.recheck_below - 0.20), f"复核冲突：首答{ans}/复核{ans2}"

    def _solve_once(self, stem, options, qtype):
        """单次作答（原 solve 逻辑）：含解析失败重试一次。"""
        ok, why = self.available()
        if not ok:
            return None, 0.0, why
        prompt = self._build_prompt(stem, options, qtype)
        last_reason = ""
        for attempt in (1, 2):
            ok, why = self.available()
            if not ok:
                return None, 0.0, last_reason or why
            if attempt == 2:
                self.log.info(f"  大模型返回不可用（第 1 次）：{last_reason or '未给出可用答案'}，重试一次")
            raw = self._chat(prompt)
            ans, conf, reason = self._parse(raw, options, qtype)
            if ans:
                self.hits += 1
                return ans, conf, reason
            last_reason = reason or last_reason
            if self.fatal:          # Key/余额问题，重试也是同样结果
                return None, 0.0, self.fatal
        return None, 0.0, last_reason or "模型未给出可用答案"

    def _should_recheck(self, conf, reason, ans, qtype):
        """程序自主判断：置信低 / 解析可疑 / 高错题型的题才值得二次唤起 DeepSeek。

        返回 (bool, 原因)。清晰高置信的题直接放行，不浪费 API。
        """
        if not self.recheck_enabled:
            return False, "复核已关闭"
        ok, why = self.available()
        if not ok:
            return False, f"不可复核（{why}）"
        if self.max_rechecks and self.rechecks >= self.max_rechecks:
            return False, f"本轮复核次数已达上限（{self.max_rechecks}）"
        # 信号1：解析层标出的可疑答案（漏选 / 题型混淆 / 存疑降置信）
        if reason and any(k in reason for k in ("漏选", "存疑", "多个字母", "复核冲突")):
            return True, f"解析可疑（{reason[:16]}）"
        # 信号2：自报置信低于阈值（灰区/低置信）
        if conf < self.recheck_below:
            return True, f"置信 {conf:.2f} < {self.recheck_below:.2f}"
        return False, "高置信，无需复核"

    def _recheck(self, stem, options, qtype):
        """换独立复核视角再唤起一次 DeepSeek（不复用第一次的 prompt 上下文）。"""
        self.rechecks += 1
        ok, why = self.available()
        if not ok:
            return None, 0.0, why
        # 复核同样是一次真实 HTTP 调用，必须计入统一预算。
        self.calls += 1
        prompt = self._build_prompt(stem, options, qtype)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": RECHECK_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            raw = data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            self.last_error = self._explain_http(exc.code, detail)
            if exc.code in (401, 402, 403):
                self.fatal = self.last_error
            self.log.warning(f"  二次复核调用失败：{self.last_error}")
            return None, 0.0, self.last_error
        except Exception as exc:  # noqa: BLE001
            self.last_error = self._explain_conn(exc)
            self.log.warning(f"  二次复核调用失败：{self.last_error}")
            return None, 0.0, self.last_error
        ans, conf, reason = self._parse(raw, options, qtype)
        if ans:
            self.hits += 1
        return ans, conf, reason

    @staticmethod
    def _same(a, b, qtype):
        """判断两次作答是否为同一答案（字母集合 / 判断题 / 填空文本）。"""
        if qtype == "text":
            norm = lambda s: re.sub(r"[\s，,。.；;]", "", str(s or "")).lower()
            return norm(a) == norm(b) and bool(norm(a))
        la = "".join(sorted(set(re.sub(r"[^A-Za-z]", "", str(a or "")).upper())))
        lb = "".join(sorted(set(re.sub(r"[^A-Za-z]", "", str(b or "")).upper())))
        if la and lb:
            return la == lb
        # 判断题等可能给"对/错"文本
        na, nb = str(a or "").strip(), str(b or "").strip()
        return na == nb and bool(na)

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
        conf = max(0.0, min(1.0, conf))
        reason = str(obj.get("reason") or "")[:60]
        if not ans:
            return None, 0.0, reason

        # 填空题的 answer 是正文而不是选项字母；即使页面没有 options 也应保留。
        if qtype == "text":
            return ans, conf, reason

        n = len(options or [])
        if qtype == "judge":
            # 模型若直接给选项字母，优先精确匹配，不能再按语义猜成“错”。
            la = ans.strip().upper()
            exact = next((o for o in (options or [])
                          if str(o.get("letter") or "").strip().upper() == la), None)
            if exact is not None:
                return exact.get("letter") or la, conf, "判断题：" + ans
            truthy = bool(re.search(r"对|正确|√|是|true", ans, re.I))
            falsy = bool(re.search(r"错|不正确|不对|×|否|false", ans, re.I))
            if truthy == falsy:  # 都命中或都不命中 → 无法判定，拒绝猜测
                return None, 0.0, f"判断题答案无法识别：{ans}"
            idx = 0 if truthy else 1
            letter = None
            for i, o in enumerate(options or []):
                t = str(o.get("text") or "")
                if (idx == 0 and re.search(r"对|正确|√|是|true", t, re.I)) or \
                   (idx == 1 and re.search(r"错|不正确|不对|×|否|false", t, re.I)):
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
        if qtype == "multi":
            if len(uniq) < 2:
                # 多选题只给了 1 个字母 → 可能是漏选，按低置信处理但仍返回
                return uniq, min(conf, 0.4), "多选题仅给出 1 项，可能漏选"
            return uniq, conf, reason
        if len(uniq) > 1:
            # 单选题却返回多个字母：说明模型没分清题型。若原样交给 fill_answer，
            # 它会连点多个选项 → 单选必错。降为低置信，让上层 min_confidence
            # 门槛拦下并转联网搜题；不直接放弃是为了不再浪费一次 API 调用重试。
            return uniq[0], 0.3, f"单选题返回多个字母（{uniq}），存疑降置信待复核"
        return uniq, conf, reason

    def stats(self):
        return {"calls": self.calls, "hits": self.hits, "rechecks": self.rechecks,
                "model": self.model}
