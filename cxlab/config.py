# -*- coding: utf-8 -*-
"""配置加载：YAML -> 带默认值的配置对象。"""
import copy
import sys
from pathlib import Path

import yaml

DEFAULTS = {
    "account": {
        "phone": "",
        "password": "",
        "login_method": "qr",
        "sms_code": "",
    },
    "course": {
        "name_contains": "*",
        "url": "",
        "skip_completed": True,
        "skip_no_task_nodes": True,   # 无任务点的知识点（纯图文/未布置任务）直接跳过
        "scope": "chapters",
        "selected_courses": [],     # 精确指定要跑的课程（名称关键词或 courseId），非空时优先
        "exclude_courses": [],      # 排除的课程（名称关键词或 courseId）
        "pick_interactively": False,  # 运行时在控制台勾选课程（字母多选）
        # ---- 截止日期 / 课程状态判定 ----
        "deadline_policy": "skip",       # skip=自动排除已结束/已退课/已过期 | mark=仅标记不排除 | all=不判断
        "skip_ended_courses": True,      # 读取平台「已结束课程/已退课课程」列表（权威依据）
        "skip_past_deadline": True,      # 卡片结束时间已过 → 排除
        "skip_not_started": False,       # 未开课（开课时间在未来）→ 排除
        "unknown_status_policy": "keep",  # 无日期信息时：keep=照常处理 | skip=排除
        "deadline_overrides": {},        # 手工覆盖：{"课程名": "2026-10-31"}；填"忽略"=强制处理
        "lesson_selection": {
            "mode": "all",          # all=全部课时 | index=按序号筛选 | pick=运行时交互选择
            "indexes": "",          # mode=index 时生效，如 "1-5,8"（也支持字母 A,C）
            "per_course": {},       # 按课程分别指定课时，如 {"实验室安全": "1-3"}
        },
    },
    "browser": {
        "channel": "auto",
        "headless": False,
        "slow_mo": 50,
        "window_width": 1600,
        "window_height": 900,
        "page_timeout_ms": 45000,      # 页面操作默认超时（网络慢可调到 60000）
        "nav_retry": 3,                # 页面打开失败重试次数
    },
    "login": {
        "qr_timeout_s": 300,
        "max_retry": 3,
        "nav_timeout_ms": 60000,       # 打开登录页的超时时间
        # ---- 掉线自愈（单端登录：手机端登录会把电脑端顶下线）----
        "auto_relogin": True,            # 检测到掉线后自动重新登录
        "check_interval_s": 60,          # 会话健康探测间隔（秒）
        "relogin_max_attempts": 3,       # 每次掉线的密码重登录尝试次数
        "max_relogin_per_hour": 6,       # 每小时最多自动重登录次数
        "relogin_cooldown_s": 600,       # 触发上限后的冷却时长（秒）
        "net_retry_delays_s": [15, 30, 60, 120, 300],   # 断网重试退避序列（秒）
        "qr_wait_s": 300,                # 需人工扫码时的等待时长（秒）
    },
    # ---- 大模型答题（OpenAI 兼容接口；不填 Key 则自动跳过）----
    "llm": {
        "enabled": False,
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-flash",
        "reasoning_effort": "",
        "min_confidence": 0.55,
        "timeout_s": 30,
        "max_calls": 200,
        "temperature": 0,
        "cross_check_with_search": False,   # 大模型答完后是否再用联网搜索交叉验证（更准但更慢）
    },
    "video": {
        # 实测（2026-10-14）：倍速播放普遍**不被平台登记**（平台按真实时间核验），
        # 因此默认 1.0；设为 >1 时若平台未登记，程序会自动用 1 倍速重播一次。
        "speed": 1.0,
        "duration_required_1x": True,
        "timeout_per_video_min": 120,
        "poll_interval_s": 3,
    },
    "document": {
        "page_delay_ms": 1500,
        "dwell_last_page_s": 10,
        "read_duration_1x": True,
        "max_seconds": 900,
    },
    "quiz": {
        "on_unknown": "pause",
        "pause_timeout_s": 900,
        "max_attempts": 1,
        "answers_file": "data/answers.json",
        "pool_file": "data/question_pool.json",
        "decode_font": True,          # 用加密字体映射还原题面（学习通 cxSecretStyle）
        "font_table": "data/font_map_table.json",   # 标准字形哈希表
        # ---- 第三方题库接口（比网页搜索准得多，命中率 90%+；命中即止）----
        "tiku": {
            "enabled": False,          # 填好 provider/token 后改 true
            "timeout_s": 12,
            "max_calls": 300,          # 单次运行最多查询次数
            "min_stem_len": 6,         # 题干太短不查（容易误命中）
            "providers": [
                # {"type": "icodef", "token": "你的token"},
                # {"type": "enncy", "token": "你的token"},
                # {"type": "tikuadapter", "url": "http://127.0.0.1:8060"},
            ],
        },
        "online_search": {
            "cross_check": True,
            "conflict_policy": "prefer_zuoyebang",
            "enabled": True,          # 本地题库未命中时联网搜题
            "engine": "bing",         # bing | baidu
            "max_searches": 40,       # 单次运行最多联网搜索次数（频率控制）
            "min_interval_s": 8,      # 两次搜索的最小间隔（防触发风控）
            "timeout_s": 20,
            "skip_when_obfuscated": True,  # 题面被字体混淆时跳过联网搜索（搜了也没用）
        },
    },
    "humanize": {
        "enabled": True,              # 拟人化操作（鼠标轨迹/随机延时/滚动）
        "click_before_ms": [200, 900],   # 点击前随机延时
        "click_after_ms": [300, 1200],   # 点击后随机延时
        "mouse_steps": [8, 20],          # 鼠标移动轨迹分段数
        "scroll_times": [1, 3],          # 任务前随机滚动次数
        "scroll_step_px": [200, 600],
        "long_pause_every": 6,           # 每 N 个任务插入一次长暂停（0=关闭）
        "long_pause_s": [15, 45],
    },
    "safety": {
        "max_runtime_min": 0,        # 单次运行最长时长(分钟)，0=不限
        "max_tasks_per_run": 0,      # 单次最多处理任务点数，0=不限
        "min_task_interval_s": 3,    # 任务之间的最小间隔(秒)
        "stop_on_risk": False,       # 遇风控页是否直接停止（False=暂停等人工）
        "retry_rounds": 2,           # 未完成的知识点在本轮末尾重试几轮（不会直接跳过）
        "confirm_before_run": False,  # 是否在执行前要求确认（交互选择模式下自动为 True）
    },
    "homework": {
        "mode": "manual",
        "answer_text": "本人已完成本课程全部内容的学习，现将作业提交。",
    },
    "popup": {
        "auto_close": True,
        "pause_on_antispider": True,
        "check_interval_s": 5,       # 弹窗探测间隔(秒)：越大越省性能，弹窗仍会被捕获
    },
    "output_dir": "output",
    "state_dir": "state",
    "log_dir": "logs",
    "log_level": "INFO",
}


class Config(dict):
    """支持属性访问的字典：cfg.account.phone。"""

    def __getattr__(self, item):
        try:
            value = self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc
        if isinstance(value, dict) and not isinstance(value, Config):
            value = Config(value)
            self[item] = value
        return value

    def __setattr__(self, key, value):
        self[key] = value


def _deep_merge(base, override):
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _resolve_relative(cfg, base):
    """把相对目录/文件路径解析到配置文件所在目录，保证在任意工作目录下运行都一致。"""
    for key in ("output_dir", "state_dir", "log_dir"):
        p = Path(str(cfg[key]))
        if not p.is_absolute():
            cfg[key] = str((base / p).resolve())
    for key in ("answers_file", "pool_file"):
        p = Path(str(cfg["quiz"][key]))
        if not p.is_absolute():
            cfg["quiz"][key] = str((base / p).resolve())


def load_config(path):
    cfg_path = Path(path)
    if not cfg_path.exists():
        sys.exit(f"[配置错误] 配置文件不存在: {cfg_path.resolve()}")
    user_cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg = Config(_deep_merge(DEFAULTS, user_cfg))
    _resolve_relative(cfg, cfg_path.resolve().parent)

    # 基础校验
    method = cfg.account.login_method
    if method == "password" and (not cfg.account.phone or not cfg.account.password):
        sys.exit("[配置错误] login_method=password 时必须填写 account.phone 和 account.password")
    if not cfg.course.name_contains and not cfg.course.url:
        sys.exit("[配置错误] 请至少填写 course.name_contains（课程名关键词）或 course.url")
    if method not in ("qr", "password"):
        sys.exit(f"[配置错误] 不支持的登录方式: {method}")
    if cfg.course.scope not in ("chapters", "task_center", "both"):
        sys.exit(f"[配置错误] 不支持的 scope: {cfg.course.scope}")
    if cfg.quiz.on_unknown not in ("pause", "skip", "submit_blank"):
        sys.exit(f"[配置错误] 不支持的 quiz.on_unknown: {cfg.quiz.on_unknown}")
    return cfg
