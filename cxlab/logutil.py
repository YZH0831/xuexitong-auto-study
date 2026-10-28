# -*- coding: utf-8 -*-
"""日志：控制台 + 文件双通道（自动脱敏 API Key / 手机号）。"""
import logging
import re
import sys
from datetime import datetime
from pathlib import Path


_MASK_RULES = [
    (re.compile(r"sk-[A-Za-z0-9]{12,}"), "sk-****"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "1**********"),
    (re.compile(r"(?i)(password|passwd|pwd)\s*[=:]\s*\S+"), r"\1=****"),
]


class MaskFilter(logging.Filter):
    """把密钥/手机号从日志里抹掉——日志会被贴到 issue、也会留在本地很久。"""

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        masked = msg
        for rx, rep in _MASK_RULES:
            masked = rx.sub(rep, masked)
        if masked != msg:
            record.msg, record.args = masked, ()
        return True


def setup_logging(log_dir, level="INFO"):
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logfile = log_path / f"run_{ts}.log"

    logger = logging.getLogger("cxlab")
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    mask = MaskFilter()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.addFilter(mask)
    logger.addHandler(console)

    file_handler = logging.FileHandler(logfile, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.addFilter(mask)
    logger.addHandler(file_handler)

    logger.info(f"日志文件: {logfile.resolve()}")
    return logger
