# -*- coding: utf-8 -*-
"""
学习通（超星）课程自动学习程序 — 示例大学实验室安全定制版

用法：
    python main.py --config config.yaml            # 正式运行
    python main.py --config config.yaml --dry-run  # 只扫描结构，不实际学习
    python main.py --config config.yaml --only video    # 只处理视频
"""
import argparse
import re

from cxlab.config import load_config
from cxlab.logutil import setup_logging
from cxlab.runner import Runner


def parse_args():
    parser = argparse.ArgumentParser(description="学习通课程自动学习（示例大学·实验室安全）")
    parser.add_argument("--version", action="store_true", help="打印版本号后退出")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只扫描章节/任务点结构，不实际学习")
    parser.add_argument("--course", default="", help="覆盖配置中的课程名关键词（等同 --select）")
    parser.add_argument("--select", default="",
                        help="指定要执行的课程：名称关键词或 courseId，逗号分隔可多选；"
                             "如 --select \"实验室安全,高等数学\" 或 --select all")
    parser.add_argument("--pick", action="store_true",
                        help="交互勾选：先按字母多选课程，再选课时范围（全部/指定/逐门指定），最后确认执行")
    parser.add_argument("--lessons", default="",
                        help="只学指定课时。全局统一：--lessons \"1-5,8\"（或 A,C）；"
                             "按课程分别指定：--lessons \"实验室安全=1-3;高等数学=2,4\"")
    parser.add_argument("--list-courses", action="store_true", help="只列出账号下的课程后退出")
    parser.add_argument("--headless", action="store_true", help="无界面运行（扫码/验证码场景请勿使用）")
    parser.add_argument("--only", default="", choices=["", "video", "document", "quiz"],
                        help="只处理指定类型的任务点")
    args = parser.parse_args()
    if args.version:
        from cxlab.version import __release__, __title__, __version__
        print(f"{__title__} v{__version__}（{__release__}）")
        raise SystemExit(0)
    return args


def main():
    args = parse_args()
    cfg = load_config(args.config)
    if args.course and not args.select:
        args.select = args.course
    if args.select:
        cfg["course"]["selected_courses"] = [s.strip() for s in args.select.replace("，", ",").split(",") if s.strip()]
    if args.pick:
        cfg["course"]["pick_interactively"] = True
        cfg["course"]["lesson_selection"]["mode"] = "pick"
    if args.lessons:
        lesson_cfg = cfg["course"]["lesson_selection"]
        if "=" in args.lessons:      # 按课程分别指定
            per = {}
            for part in re.split(r"[;；]", args.lessons):
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k.strip():
                        per[k.strip()] = v.strip()
            lesson_cfg["per_course"] = per
            lesson_cfg["mode"] = "index"
        else:                        # 全局统一规则
            lesson_cfg["mode"] = "index"
            lesson_cfg["indexes"] = args.lessons
    if args.headless:
        cfg["browser"]["headless"] = True

    log = setup_logging(cfg.log_dir, cfg.log_level)
    log.info("=" * 60)
    log.info(f"学习通自动学习程序启动 | 课程关键词: {cfg.course.name_contains or cfg.course.url}")
    log.info(f"运行模式: {'DRY-RUN(仅扫描)' if args.dry_run else '正式运行'}")
    log.info("=" * 60)
    Runner(cfg, log, args).run()


if __name__ == "__main__":
    main()
