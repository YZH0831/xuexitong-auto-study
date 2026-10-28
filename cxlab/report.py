# -*- coding: utf-8 -*-
"""完成状态汇总：Markdown 报告 + JSON 明细。"""
import json
from datetime import datetime
from pathlib import Path

STATUS_LABEL = {
    "done": "已完成",
    "failed": "失败",
    "manual": "需人工",
    "skipped": "跳过",
}
STATUS_ICON = {
    "done": "[OK]",
    "failed": "[XX]",
    "manual": "[!!]",
    "skipped": "[--]",
}


class Report:
    def __init__(self, output_dir, course_name, log):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.course_name = course_name or "未指定课程"
        self.log = log
        self.rows = []
        self.start = datetime.now()
        self.current_course = ""

    def set_course(self, name):
        """多课程模式下给分组加课程前缀。"""
        self.current_course = name or ""

    def add(self, group, title, status, note=""):
        if self.current_course:
            group = f"{self.current_course} / {group}"
        self.rows.append({
            "group": group,
            "title": title,
            "status": status,
            "status_label": STATUS_LABEL.get(status, status),
            "note": note,
            "time": datetime.now().strftime("%H:%M:%S"),
        })

    def counts(self):
        c = {"done": 0, "failed": 0, "manual": 0, "skipped": 0}
        for r in self.rows:
            c[r["status"]] = c.get(r["status"], 0) + 1
        return c

    def write(self):
        ts = self.start.strftime("%Y%m%d_%H%M%S")
        c = self.counts()
        total = sum(c.values())

        md = [
            f"# 学习通课程完成情况汇总 — {self.course_name}",
            "",
            f"- 开始时间：{self.start.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 结束时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 任务条目：{total}（已完成 {c['done']} / 失败 {c['failed']} / 需人工 {c['manual']} / 跳过 {c['skipped']}）",
            "",
            "## 分章节明细",
            "",
            "| 章节/分组 | 任务点 | 状态 | 说明 | 时间 |",
            "| --- | --- | --- | --- | --- |",
        ]
        # 按章节分组输出
        groups = {}
        for row in self.rows:
            groups.setdefault(row["group"], []).append(row)
        for group, rows in groups.items():
            for row in rows:
                md.append(
                    f"| {group} | {row['title'][:60]} | {STATUS_ICON.get(row['status'], '')} "
                    f"{row['status_label']} | {row['note'][:60]} | {row['time']} |"
                )
        md += [
            "",
            "## 统计",
            "",
            f"- 已完成：{c['done']}",
            f"- 失败：{c['failed']}",
            f"- 需人工处理：{c['manual']}",
            f"- 跳过（已完成/无任务）：{c['skipped']}",
            "",
            "> 说明：「需人工」的条目已在浏览器中打开或给出提示，请手动完成后再运行一次程序复核。",
            "",
        ]
        md_path = self.output_dir / f"完成报告_{ts}.md"
        md_path.write_text("\n".join(md), encoding="utf-8")

        json_path = self.output_dir / f"完成报告_{ts}.json"
        json_path.write_text(
            json.dumps({
                "course": self.course_name,
                "start": self.start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "counts": c,
                "rows": self.rows,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.log.info(f"完成报告: {md_path.resolve()}")
        self.log.info(f"汇总统计: 已完成 {c['done']} / 失败 {c['failed']} / 需人工 {c['manual']} / 跳过 {c['skipped']}")
        return md_path, json_path
