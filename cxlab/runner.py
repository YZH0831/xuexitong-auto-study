# -*- coding: utf-8 -*-
"""主流程编排：登录 -> 定位课程 -> 逐章节推进 -> 任务点处理 -> 作业 -> 输出报告。"""
import re
import time

from . import course as course_mod
from . import document as document_mod
from . import homework as homework_mod
from . import login as login_mod
from . import quiz as quiz_mod
from . import video as video_mod
from .answers import QuestionBank
from .browser import BrowserSession
from .humanize import Humanizer
from .online_answer import OnlineAnswer
from .popups import PopupGuard, RiskControlStop
from .report import Report

TYPE_LABEL = {
    "video": "视频",
    "document": "文档/课件",
    "read": "阅读",
    "quiz": "章节测验",
    "live": "直播",
    "unknown": "未知",
}


class Runner:
    def __init__(self, cfg, log, args):
        self.cfg = cfg
        self.log = log
        self.args = args
        self.session = None
        self.popup = None
        self.report = None
        self.bank = None
        self.course = None
        self.humanizer = None
        self.online = None
        self.task_count = 0
        self.start_ts = 0.0
        self.stop_reason = ""
        # 平台进度（以接口为准，供日志与报告对照）
        self.platform_done = 0
        self.platform_total = 0
        self.done_this_run = []
        self.skipped_this_run = []      # 本次新完成的知识点标题
        self.failed_this_run = []    # 本次失败/需人工的标题
        self.failed_chapters = []    # 失败重试队列（本章节对象）
        self._chapters_cache = []
        # 会话健康 / 掉线自愈
        self._last_probe = 0.0
        self._relogin_times = []
        self.relogin_count = 0
        self.relogin_failed = 0
        self.net_wait_count = 0
        # 课时选择状态
        self.lesson_mode = "all"        # all | index | per_course
        self.lesson_rule = ""           # index 模式下的表达式
        self.interactive = False
        self.plan = []                  # [(课程名, 课时数或None, 规则说明)]

    # ---------------- 入口 ----------------
    def run(self):
        cfg, log = self.cfg, self.log
        self.session = BrowserSession(cfg, log)
        self.popup = PopupGuard(cfg, log)
        self.bank = QuestionBank(cfg.quiz.answers_file, cfg.quiz.pool_file, log)
        self.report = Report(cfg.output_dir, cfg.course.name_contains or cfg.course.url, log)
        self.humanizer = Humanizer(cfg, log)
        self.session.humanizer = self.humanizer
        self.online = OnlineAnswer(cfg, log)
        self.start_ts = time.time()
        log.info(
            f"拟人化操作: {'开启' if self.humanizer.enabled else '关闭'} | "
            f"联网搜题: {'开启' if self.online.enabled else '关闭'}"
            f"（上限 {self.online.max_searches or '∞'} 次，间隔 {self.online.min_interval:.0f}s）"
        )

        try:
            if not login_mod.ensure_login(self.session, cfg, log):
                self.report.add("登录", "账号登录", "failed", "登录失败或超时，请检查账号/网络后重试")
                return
            log.info(f"题库条目数: {self.bank.size}")
            if getattr(self.args, "list_courses", False):
                cards = course_mod.list_courses(self.session, cfg, log)
                course_mod.annotate_courses(self.session, cfg, log, cards)
                print("\n===== 你的课程列表（含状态判定）=====")
                for i, c in enumerate(cards, 1):
                    st = course_mod.STATUS_LABELS.get(c.get("status", "unknown"), "")
                    print(f"  [{course_mod.label_for(i):>2}] {c['name']}  [{st}] {c.get('status_reason', '')}")
                    print(f"        courseId={c['course_id']}")
                if not cards:
                    print("  （未读到课程，请检查登录态）")
                print("\n提示：可用 --select \"课程名或ID,逗号分隔\" 指定要跑的课程；--pick 交互勾选。")
                return
            courses = course_mod.choose_courses(self.session, cfg, log, getattr(self.args, "select", ""))
            statuses = cfg.get("_course_status") or []
            if statuses:
                counts = {}
                for _, st, _ in statuses:
                    counts[st] = counts.get(st, 0) + 1
                summary = "、".join(f"{course_mod.STATUS_LABELS.get(k, k)} {v} 门"
                                    for k, v in counts.items())
                log.info(f"课程状态判定汇总：{summary}")
                self.report.add("全局", "课程状态判定", "skipped", summary)
            dropped = cfg.get("_course_dropped") or []
            if dropped:
                self.report.add(
                    "全局", "已排除课程（不再处理）", "skipped",
                    "；".join(f"{n}（{course_mod.STATUS_LABELS.get(s, s)}）" for n, s, _ in dropped),
                )

            self.lesson_mode = str(cfg.course.lesson_selection.mode or "all").lower()
            self.lesson_rule = str(cfg.course.lesson_selection.indexes or "")
            self.interactive = bool(cfg.course.pick_interactively) or self.lesson_mode == "pick"

            # 交互选择：课程（字母多选）→ 课时范围 → 确认执行
            if self.interactive:
                self._ask_lesson_strategy(courses)
                if not self._confirm_plan(courses):
                    log.warning("用户取消执行，未做任何学习动作")
                    self.report.add("全局", "执行计划", "skipped", "用户在选择阶段取消")
                    return
            elif bool(cfg.safety.confirm_before_run):
                if not self._confirm_plan(courses):
                    log.warning("用户取消执行")
                    self.report.add("全局", "执行计划", "skipped", "用户取消")
                    return

            multi = len(courses) > 1
            if multi:
                self.report.course_name = f"{len(courses)} 门课程"
            log.info(f"本次将处理 {len(courses)} 门课程")

            for idx, course in enumerate(courses, 1):
                log.info("=" * 60)
                log.info(f"课程 {idx}/{len(courses)}: {course.name}")
                log.info("=" * 60)
                try:
                    self.course = course_mod.enter_course(self.session, course, cfg, log)
                    self.report.set_course(course.name if multi else "")
                    self.process_one_course()
                except RiskControlStop as exc:
                    self.stop_reason = str(exc)
                    log.error(f"触发风控保护，停止后续课程：{exc}")
                    self.report.add(course.name, "风控中止", "manual", str(exc)[:100])
                    break
                except Exception as exc:  # noqa: BLE001
                    log.exception(f"课程 [{course.name}] 处理异常: {exc}")
                    self.report.add(course.name, "课程整体", "failed", str(exc)[:120])
                finally:
                    self.bank.save_pool()
                if idx < len(courses):
                    # 回到学习空间，准备下一门课程
                    try:
                        self.session.page.goto(course_mod.HOME_URL, wait_until="domcontentloaded", timeout=60000)
                        self.session.page.wait_for_timeout(2000)
                    except Exception:  # noqa: BLE001
                        pass
        except KeyboardInterrupt:
            log.warning("用户中断运行")
        except RiskControlStop as exc:
            log.error(f"触发风控保护，已停止：{exc}")
            if self.report is not None:
                self.report.add("全局", "风控中止", "manual", str(exc)[:120])
        except Exception as exc:  # noqa: BLE001
            log.exception(f"运行异常: {exc}")
            if self.report is not None:
                self.report.add("全局", "运行异常", "failed", str(exc)[:120])
        finally:
            if self.report is not None and self.online is not None:
                st = self.online.stats()
                self.report.add(
                    "全局",
                    "本次运行概况",
                    "skipped",
                    f"处理任务点 {self.task_count} 个；联网搜题 {st['searches']} 次（命中 {st['hits']}）；"
                    f"风控告警 {self.popup.risk_events if self.popup else 0} 次"
                    + (f"；停止原因：{self.stop_reason}" if self.stop_reason else "")
                    + (f"；断网等待 {self.net_wait_count} 次" if self.net_wait_count else "")
                    + (f"；自动重登录 {self.relogin_count} 次"
                       if (self.relogin_count or self.relogin_failed) else "")
                    + (f"（失败 {self.relogin_failed} 次）" if self.relogin_failed else ""),
                )
            if self.bank is not None:
                self.bank.save_pool()
            if self.report is not None:
                self.report.write()
            # 本次结果 vs 平台进度（避免把"处理过"误读成"已完成"）
            if self.done_this_run or self.failed_this_run:
                log.info("=" * 62)
                log.info("本次结果对照（以平台登记为准，等同手机端所见）")
                log.info(f"  ✔ 平台已登记完成 {len(self.done_this_run)} 个"
                         + ("：" + "、".join(t[:22] for t in self.done_this_run[:8]) if self.done_this_run else ""))
                log.info(f"  ✘ 未完成/需人工 {len(self.failed_this_run)} 个"
                         + ("：" + "、".join(t[:22] for t in self.failed_this_run[:8]) if self.failed_this_run else ""))
                if self.platform_total:
                    log.info(f"  平台当前进度：{self.platform_done}/{self.platform_total}"
                             "（手机端显示的就是这个数字）")
                log.info("  说明：日志中的「列表第 N/M 个」是章节序号，不代表已完成数量")
                log.info("=" * 62)
            if self.session is not None:
                self.session.close()
            log.info("运行结束")

    # ---------------- 单门课程 ----------------
    def process_one_course(self):
        scope = self.cfg.course.scope
        if scope in ("chapters", "both"):
            self.process_chapters()
        if scope in ("task_center", "both"):
            self.process_task_center()
        if scope in ("chapters", "both"):
            homework_mod.run_homework(self.session, self.cfg, self.log, self.popup, self.report)

    # ---------------- 平台进度（权威口径） ----------------
    def _refresh_platform_progress(self, chapters, quiet=False):
        """用接口刷新"平台已完成/总任务点"，这是手机端与网页端显示的一致口径。"""
        try:
            st = course_mod.chapter_status(self.session, self.course, [c.id for c in chapters], self.log)
        except Exception:  # noqa: BLE001
            st = {}
        if not st:
            return
        done = sum(1 for v in st.values() if int(v.get("total", 0) or 0) > 0
                   and int(v.get("unfinish", 0) or 0) == 0)
        total = sum(1 for v in st.values() if int(v.get("total", 0) or 0) > 0)
        self.platform_done, self.platform_total = done, total
        for c in chapters:
            v = st.get(c.id)
            if v:
                c.hint = (int(v.get("unfinish", 1) or 0) if int(v.get("total", 0) or 0) > 0
                          else (-1 if int(v.get("unfinish", 0) or 0) == 0 else 1))
        if not quiet:
            self.log.info(f"  平台进度：已完成任务点 {done}/{total}（与手机端显示一致）")

    # ---------------- 会话健康 / 掉线自愈 ----------------
    def _ensure_session(self, context=""):
        """定期探测会话状态：掉线→自动重登录；断网→等待恢复。返回 True 表示可继续。

        单端登录限制下，手机端登录会把电脑端顶下线，这里负责自愈。
        """
        cfg, log = self.cfg, self.log
        if not bool(cfg.login.auto_relogin):
            return True
        now = time.time()
        if now - self._last_probe < float(cfg.login.check_interval_s or 60):
            return True
        self._last_probe = now
        try:
            state = self.session.probe_session()
        except Exception as exc:  # noqa: BLE001
            log.debug(f"[会话] 探测异常（忽略）: {str(exc)[:60]}")
            return True
        tag = f"（{context}）" if context else ""
        if state == "ok":
            return True
        if state == "offline":
            log.warning(f"[会话] 网络不通{tag} → 等待网络恢复…")
            return self._wait_network(context)
        if state == "logged_out":
            log.warning(f"[会话] 电脑端已掉线{tag}（多见于手机端登录顶下线）")
            return self._do_relogin(context)
        log.debug(f"[会话] 探测结果未知{tag}，继续执行")
        return True

    def _wait_network(self, context=""):
        """网络断开：按退避序列等待恢复（不算作登录失败）。"""
        self.net_wait_count += 1
        delays = list(self.cfg.login.net_retry_delays_s or [15, 30, 60, 120, 300])
        for i, delay in enumerate(delays, 1):
            self.log.info(f"  等待 {delay} 秒后重试（{i}/{len(delays)}）…")
            time.sleep(float(delay))
            state = self.session.probe_session()
            if state == "ok":
                self.log.info("  网络已恢复，继续任务")
                self._last_probe = time.time()
                return True
            if state == "logged_out":
                return self._do_relogin(context)
        self.log.error("  网络长时间未恢复 → 本轮暂停；平台侧进度已保存，下次运行会自动续跑")
        return False

    def _do_relogin(self, context=""):
        """自动重新登录（带频率限制与冷却，避免频繁登录触发风控）。"""
        cfg, log = self.cfg, self.log
        hour_ago = time.time() - 3600
        self._relogin_times = [t for t in self._relogin_times if t > hour_ago]
        limit = int(cfg.login.max_relogin_per_hour or 6)
        if len(self._relogin_times) >= limit:
            cooldown = int(cfg.login.relogin_cooldown_s or 600)
            log.error(f"[会话] 1 小时内已自动重登录 {limit} 次（疑似账号在别处频繁登录）"
                      f"→ 冷却 {cooldown} 秒，避免触发风控")
            time.sleep(cooldown)
            self._relogin_times = [t for t in self._relogin_times if t > time.time() - 3600]
        self._relogin_times.append(time.time())
        if not (cfg.account.phone and cfg.account.password):
            log.warning("[会话] 未配置账号密码，只能扫码重登录（需人工）；"
                        "想无人值守请在 config.yaml 填 account.phone / account.password")
        ok = login_mod.relogin(self.session, cfg, log)
        if ok:
            self.relogin_count += 1
            course_mod.reset_caches()
            self._resume_context()
            self._last_probe = time.time()
        else:
            self.relogin_failed += 1
            log.error("[会话] 自动重登录失败；请人工处理后重新运行（已完成进度不会丢失）")
        return ok

    def _resume_context(self):
        """重新登录后重建上下文：回到当前课程页，保证从中断处续跑。"""
        if not self.course:
            return
        try:
            self.log.info(f"重新进入课程以续跑：{self.course.name}")
            course_mod.enter_course(self.session, self.course, self.cfg, self.log)
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"重进课程失败（下一个知识点会自动重试）: {str(exc)[:70]}")

    # ---------------- 课时选择（交互） ----------------
    def _ask_lesson_strategy(self, courses):
        """选择课时范围：全部 / 同一规则 / 逐门指定。"""
        log, cfg = self.log, self.cfg
        if self.lesson_mode != "pick":
            return
        print("\n===== 课时范围选择（已选 %d 门课程）=====" % len(courses))
        print("  [1] 全部课时 —— 每门课程从头学到尾（已完成的会自动跳过）")
        print("  [2] 指定课时 —— 用同一套序号筛选所有已选课程（如 1-5,8 或 A,C）")
        print("  [3] 逐门指定 —— 进入每门课程后单独挑选课时")
        ans = course_mod.read_line(">>> 请输入 1/2/3 后回车（默认 1）: ", 180, log, "1").strip()
        if ans == "2":
            self.lesson_mode = "index"
            self.lesson_rule = course_mod.read_line(
                ">>> 输入课时序号（如 1,3,5-8 或 A,C；all=全部）: ", 180, log, "all")
            log.info(f"课时规则：所有课程统一使用「{self.lesson_rule}」")
        elif ans == "3":
            self.lesson_mode = "per_course"
            log.info("课时规则：逐门课程分别指定")
        else:
            self.lesson_mode = "all"
            log.info("课时规则：全部课时")

    def _confirm_plan(self, courses):
        """执行前确认：打印计划摘要并要求 Y/n。"""
        label = {
            "all": "全部课时（已完成的自动跳过）",
            "index": f"指定课时：{self.lesson_rule or '未指定'}",
            "per_course": "逐门课程分别指定",
        }.get(self.lesson_mode, self.lesson_mode)
        print("\n===== 执行计划确认 =====")
        for i, c in enumerate(courses, 1):
            print(f"  [{course_mod.label_for(i)}] {c.name}")
        print(f"  课程数量：{len(courses)} 门")
        print(f"  课时范围：{label}")
        print("  提示：视频类课时按平台规则需 1 倍速真实播放，整体耗时较长；中途可 Ctrl+C 停止。")
        ans = course_mod.read_line("确认开始？[Y/n]: ", 180, self.log, "y").strip().lower()
        ok = ans in ("", "y", "yes", "是", "1")
        print("  -> " + ("已确认，开始执行" if ok else "已取消"))
        return ok

    def _select_lessons(self, course, chapters):
        """按选择规则筛选本课程的课时；返回要处理的章节列表。"""
        cfg = self.cfg
        per = cfg.course.lesson_selection.get("per_course") or {}
        for key, spec in per.items():
            if course_mod.match_course({"name": course.name, "course_id": course.course_id}, key):
                return self._filter_lessons(chapters, spec, course, source="配置(per_course)")
        if self.lesson_mode == "index" and self.lesson_rule:
            return self._filter_lessons(chapters, self.lesson_rule, course, source="统一规则")
        if self.lesson_mode == "per_course":
            return self._ask_lessons_per_course(course, chapters)
        return chapters

    def _ask_lessons_per_course(self, course, chapters):
        print(f"\n===== 课程【{course.name}】共 {len(chapters)} 个课时 =====")
        course_mod.print_lesson_list(chapters)
        print("\n  输入要学习的课时序号：如 1,3,5-8 或 A,C；all=全部；0=跳过本课程")
        raw = course_mod.read_line(">>> 请输入后回车: ", 300, self.log, "all").strip()
        if raw in ("0", "-", "none", "跳过", "skip"):
            self.log.info(f"【{course.name}】已跳过")
            return []
        return self._filter_lessons(chapters, raw, course, source="逐门指定")

    def _filter_lessons(self, chapters, spec, course, source=""):
        if course_mod.is_all_spec(spec):
            self.log.info(f"【{course.name}】课时范围：全部 {len(chapters)} 个")
            return chapters
        idx = course_mod.parse_index_spec(spec, len(chapters))
        if not idx:
            self.log.warning(f"【{course.name}】课时表达式「{spec}」未匹配到课时 → 按全部处理")
            return chapters
        chosen = [ch for i, ch in enumerate(chapters, 1) if i in idx]
        self.log.info(
            f"【{course.name}】课时筛选{('（' + source + '）') if source else ''}："
            f"{len(chapters)} → {len(chosen)} 个（规则：{spec}）"
        )
        return chosen

    # ---------------- 章节 ----------------
    def _tree_page(self):
        """确保当前页面能看到章节树；必要时重新打开课程页。"""
        page = self.session.page
        if course_mod.find_chapters_frame(page) is not None:
            return page
        url = self.course.url or ""
        for _ in range(2):
            if not url:
                break
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=int(self.cfg.login.nav_timeout_ms))
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"打开课程页失败: {str(exc)[:80]}")
            page.wait_for_timeout(6000)
            if course_mod.find_chapters_frame(page) is not None:
                return page
        return page

    LOCK_MARKS = ("暂未开放", "尚未开放", "不能查看", "章节未开放")

    def _chapter_locked(self, page):
        """判断当前知识点是否因平台限制"未开放"（新版界面会提示开放时间）。"""
        for f in course_mod.all_frames(page):
            try:
                txt = f.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 1200)") or ""
            except Exception:  # noqa: BLE001
                continue
            if any(k in txt for k in self.LOCK_MARKS):
                return True
        return False

    def _open_chapter(self, ch):
        """点击章节树里的知识点进入学习页，返回 (页面, cards 帧)。"""
        log = self.log
        page = self._tree_page()
        frame, el = course_mod.chapter_element(page, ch.id)
        if el is None:
            log.warning(f"  未在章节树中找到知识点元素 (cur{ch.id})")
            return page, None
        # 新版界面(mooc2-ans)点章节不会跳页，内容在同一帧里刷新 → 不必等跳转
        new_ui = "mooc2-ans" in (page.url or "")
        try:
            if new_ui:
                el.click(timeout=8000)
            else:
                page, _ = self.session.click_capture(page, el, wait_s=8)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"  点击章节失败: {str(exc)[:80]}")
            return page, None

        # 轮询"未开放"提示（通常 1~2 秒就出现，锁定章节可立刻收工）
        self._last_locked = False
        for i in range(8):
            page.wait_for_timeout(500 if i else 800)
            self.popup.tick(page)
            if self._chapter_locked(page):
                log.info("  该章节暂未开放（平台限制）→ 跳过")
                self._last_locked = True
                return page, None
        self.session.keep_alive()
        page.wait_for_timeout(1000)
        cards = None
        for _ in range(8):
            cards = course_mod.find_content_frame(page)
            if cards is not None:
                break
            page.wait_for_timeout(2000)
            self.popup.tick(page)
        return page, cards

    def process_chapters(self):
        log, cfg = self.log, self.cfg
        page = self._tree_page()
        tree = course_mod.find_chapters_frame(page)
        chapters = course_mod.discover_chapters(page, log)
        self._chapters_cache = chapters
        if not chapters:
            dump = f"{cfg.output_dir}/structure_chapters.txt"
            course_mod.dump_structure(page, dump, log)
            self.report.add("课程", "章节解析", "manual", f"未解析到章节，结构已导出 {dump}")
            return
        # 权威状态：平台接口（每节点 total/finish/unfinish）；DOM 图标在部分课程不可靠
        st = course_mod.chapter_status(self.session, self.course, [c.id for c in chapters], log)
        if st:
            done = no_task = pending = missing = 0
            for c in chapters:
                v = st.get(c.id)
                if not v:
                    missing += 1
                    continue
                if int(v.get("total", 0) or 0) == 0 and int(v.get("unfinish", 0) or 0) == 0:
                    c.hint = -1                      # 无任务点：无需学习
                    no_task += 1
                elif int(v.get("unfinish", 0) or 0) == 0:
                    c.hint = 0                       # 已完成（对应树上绿色标记）
                    done += 1
                else:
                    c.hint = int(v["unfinish"])      # 待完成（对应树上橙色标记）
                    pending += 1
            log.info(
                f"平台状态（接口）：待完成 {pending} / 已完成 {done} / 无任务点 {no_task}"
                + (f" / 未返回 {missing}" if missing else "")
            )
            self.platform_done, self.platform_total = done, done + pending
        else:
            log.info("平台状态：接口不可用，改用章节树标记判定")
        prog = course_mod.tree_progress(tree) if tree is not None else None
        if prog:
            log.info(f"章节树进度显示：已完成任务点 {prog[0]}/{prog[1]}")

        # 课时级筛选（全部 / 指定序号 / 逐门指定）——试运行也会生效，便于先看选定范围
        chapters = self._select_lessons(self.course, chapters)
        if not chapters:
            self.report.add(self.course.name, "课时选择", "skipped", "按选择跳过该课程")
            return

        if getattr(self.args, "dry_run", False):
            self._dry_run_chapters(chapters)
            return

        self._run_rounds(chapters)

    def _run_rounds(self, chapters):
        """带重试的章节处理循环：未完成的知识点在本轮末尾重试（可单元测试）。"""
        log, cfg = self.log, self.cfg
        retry_rounds = int(cfg.safety.get("retry_rounds", 2) or 0)
        pending = list(chapters)
        aborted = False
        for rnd in range(1, retry_rounds + 2):
            if aborted or not pending:
                break
            if rnd > 1:
                log.info("=" * 62)
                log.info(f"第 {rnd} 轮：重试上一轮未完成的 {len(pending)} 个知识点"
                         f"（最多 {retry_rounds} 轮重试）")
                log.info("=" * 62)
                self.failed_chapters = []
            for i, ch in enumerate(pending, 1):
                if self._limit_reached():
                    log.warning(f"已达安全限制（{self.stop_reason}），停止处理剩余章节")
                    aborted = True
                    break
                # 会话健康检查：掉线则自动重登录，断网则等待恢复
                if not self._ensure_session("章节循环"):
                    log.error("会话不可用 → 暂停本轮（平台侧进度已保存，下次运行自动续跑）")
                    self.report.add(self.course.name, "会话中断", "manual",
                                    "掉线/断网未恢复，本轮暂停（已完成的不会重做）")
                    aborted = True
                    break
                log.info(
                    f"--- [第{rnd}轮 · 列表第 {i}/{len(pending)} 个 · 平台已完成 "
                    f"{self.platform_done}/{self.platform_total}] 开始处理：{ch.title} ---"
                )
                if cfg.course.skip_completed and ch.hint == 0:
                    self.report.add(ch.title, "章节", "skipped", "已完成（树上绿色标记 / 接口 finish=total）")
                    log.info("  已完成，跳过")
                    continue
                if ch.hint == -1 and bool(cfg.course.get("skip_no_task_nodes", True)):
                    self.report.add(ch.title, "章节", "skipped", "无任务点（纯图文/未布置任务），无需学习")
                    log.info("  无任务点，跳过")
                    continue
                # 拟人化：进入章节前随机滚动页面
                self.humanizer.scroll(self.session.page)
                self.process_chapter(ch)
                # 章节之间的最小间隔（带随机抖动）
                self.humanizer.task_interval(int(cfg.safety.min_task_interval_s))
            pending = [c for c in self.failed_chapters if c.hint != 0]
            if pending and not aborted:
                log.info(f"本轮结束：仍有 {len(pending)} 个知识点未完成"
                         f"（{ '、'.join(c.title[:18] for c in pending[:6]) }…）")

        if self.failed_chapters:
            log.warning("=" * 62)
            log.warning(f"注意：以下 {len(self.failed_chapters)} 个知识点仍未完成（平台未变绿），"
                        f"下次运行会自动重试：")
            for c in self.failed_chapters[:12]:
                log.warning(f"    ✘ {c.title}")
            log.warning("=" * 62)

    def _dry_run_chapters(self, chapters):
        log, cfg = self.log, self.cfg
        log.info("=== DRY-RUN：只扫描结构，不会真的学习 ===")
        for ch in chapters:
            flag = "已完成" if ch.hint == 0 else (f"未完成 {ch.hint}" if ch.hint else "状态未知")
            print(f"[章节] {ch.title}  (id={ch.id}, {flag})")
        if chapters:
            try:
                page, cards = self._open_chapter(chapters[0])
                ctype = course_mod.card_type(cards) if cards is not None else "unknown"
                print(f"\n首个知识点任务点类型: {ctype}")
                if cards is not None:
                    subs = cards.evaluate(
                        "() => Array.from(document.querySelectorAll('iframe')).map(x => x.getAttribute('src')||'')"
                    ) or []
                    for s in subs:
                        print("   iframe:", s[:110])
                    course_mod.dump_structure(cards, f"{cfg.output_dir}/structure_cards_example.txt", log)
                course_mod.dump_structure(page, f"{cfg.output_dir}/structure_page_example.txt", log)
            except Exception as exc:  # noqa: BLE001
                log.warning(f"示例结构导出失败: {exc}")
        print("\n确认清单无误后去掉 --dry-run 正式运行。")

    JS_QUIZ_ENTRY = r"""
    () => {
      const kws = ['章节测验','章节测试','单元测验','随堂测验','章节作业','测验','作业','考试'];
      const bad = ['查看','已完成','已提交','重做','再做','成绩','得分','解析','答案','未开始'];
      const els = Array.from(document.querySelectorAll('a,div,span,li,button'));
      for (const el of els) {
        const raw = (el.innerText || '').replace(/\s+/g, '').trim();
        if (!raw || raw.length > 16) continue;
        if (!kws.some(k => raw.includes(k))) continue;
        if (bad.some(b => raw.includes(b))) continue;
        if (el.offsetParent === null) continue;
        // 排除导航栏/页脚/侧栏里的"测验"字样（那是干扰项，点了打不开答题页）
        const badArea = el.closest('header,footer,nav,aside,[class*="nav"],[class*="menu"],'
                                   + '[class*="footer"],[class*="header"],[class*="sidebar"],'
                                   + '[class*="crumb"],[class*="tab-bar"]');
        if (badArea) continue;
        const clickable = el.tagName === 'A' || el.getAttribute('onclick')
                          || (el.style && el.style.cursor === 'pointer');
        if (!clickable) continue;
        const inner = el.querySelector('a, [onclick]');
        return inner || el;
      }
      return null;
    }
    """

    JS_QUIZ_TABS = r"""
    () => {
      // 知识点页的任务点标签：<li id="dct1" onclick="changeDisplayContent(1,...)">1章节测验</li>
      // 除测验外，还要认「微课视频」这类标签（否则只做第一个"学习目标"标签，视频永远不播）
      const li = Array.from(document.querySelectorAll(
          '#prev_tab li, .prev_list li, [id^="dct"]'));
      const out = [];
      li.forEach(e => {
        const t = (e.innerText || '').replace(/\s+/g, '');
        if (!t || t.length > 20) return;
        if (e.offsetParent === null) return;
        // 音频导学也是 video.js 播放器（modules/audio），必须当视频处理，否则永远不播
        if (!/视频|微课|音频|听力|听书|测验|作业|考试|学习目标|文档/.test(t)) return;
        let kind = 'doc';
        if (/视频|微课|音频|听力|听书/.test(t)) kind = 'video';
        else if (/测验|作业|考试/.test(t)) kind = 'quiz';
        else if (/学习目标/.test(t)) kind = 'goal';
        out.push({id: e.id || '', text: t, kind});
      });
      return out;
    }
    """

    # 章节内多页内容（页签是纯数字 1 / 2 / 3，不带"视频/测验"文字）
    # 实测：这类章节 platform 端 totalcount=2，只做第 1 页的视频会永远停在 1/2
    JS_PAGE_TABS = r"""
    () => {
      const lis = Array.from(document.querySelectorAll('.prev_list li, #prev_list li'));
      const vis = lis.filter(e => e.offsetParent !== null);
      return vis.map((e, i) => ({
        idx: i,
        label: (e.innerText || '').replace(/\s+/g, '').slice(0, 4),
        active: /active/.test(e.className || '')
      }));
    }
    """

    def _page_tabs(self, page):
        """章节内多页签信息；只有 2 页以上才返回（单页返回空列表）。"""
        for f in course_mod.all_frames(page):
            try:
                meta = f.evaluate(self.JS_PAGE_TABS) or []
            except Exception:  # noqa: BLE001
                continue
            if len(meta) > 1:
                return meta
        return []

    def _goto_page(self, page, idx):
        """切到章节内第 idx 页（0 基），返回切换后的内容帧。"""
        clicked = False
        for f in course_mod.all_frames(page):
            try:
                els = [e for e in f.query_selector_all(".prev_list li, #prev_list li") if e.is_visible()]
            except Exception:  # noqa: BLE001
                continue
            if len(els) > 1 and idx < len(els):
                try:
                    els[idx].click(timeout=5000)
                    clicked = True
                except Exception:  # noqa: BLE001
                    try:
                        self.session.click_capture(page, els[idx], wait_s=6)
                        clicked = True
                    except Exception as exc:  # noqa: BLE001
                        self.log.warning(f"     切换到第 {idx + 1} 页失败: {str(exc)[:50]}")
                if clicked:
                    page.wait_for_timeout(4000)
                    self.popup.tick(page)
                break
        if not clicked:
            return None
        for _ in range(6):
            cards = course_mod.find_content_frame(page)
            if cards is not None:
                return cards
            page.wait_for_timeout(800)
        return None

    def _play_video_here(self, page, ch):
        """在当前标签页里播视频（新版知识点页的视频在 knowledge/cards 帧内）。"""
        self.task_count += 1
        self.humanizer.scroll(page)
        return video_mod.play_video(
            self.session, self.cfg, self.log, page, self.popup, None,
            keepalive=self._ensure_session,
        )

    def _browse_here(self, page, ch):
        """当前标签页是文档/正文 → 滚动浏览。"""
        self.task_count += 1
        self.humanizer.scroll(page)
        return document_mod.browse_document(self.session, self.cfg, self.log, page, self.popup, None)

    def _quiz_tabs(self, page):
        """取知识点页里的"测验"标签页（返回 [(frame, li元素句柄)]）。"""
        out = []
        for f in course_mod.all_frames(page):
            try:
                meta = f.evaluate(self.JS_QUIZ_TABS) or []
            except Exception:  # noqa: BLE001
                continue
            for m in meta:
                sel = f"#{m['id']}" if m.get("id") else ""
                if not sel:
                    continue
                try:
                    el = f.query_selector(sel)
                    if el is not None:
                        out.append((f, el, m.get("text", ""), m.get("kind", "doc")))
                except Exception:  # noqa: BLE001
                    continue
        # 去重（同一标签可能在多个帧里出现）
        seen, uniq = set(), []
        for item in out:
            if item[2] in seen:
                continue
            seen.add(item[2])
            uniq.append(item)
        return uniq

    def _find_quiz_entry(self, page):
        """在章节页里找"章节测验/作业"入口元素。

        有些课程（如某课程甲）的**任务点就是章节测验**，视频只是内容、不计任务点，
        所以必须主动点开测验去做，而不能只播视频。
        """
        for root in course_mod.all_frames(page):
            try:
                handle = root.evaluate_handle(self.JS_QUIZ_ENTRY)
                el = handle.as_element() if handle is not None else None
                if el is not None:
                    return el
            except Exception:  # noqa: BLE001
                continue
        return None

    def process_chapter(self, ch):
        log = self.log
        page, cards = self._open_chapter(ch)
        if getattr(self, "_last_locked", False):
            log.info("  结果: skipped | 章节未开放，跳过（非错误）")
            self.report.add(ch.title, f"[未开放] {ch.title[:50]}", "skipped", "章节未开放（平台限制）")
            self.skipped_this_run.append(ch.title)
            return
        rate_used = 1.0
        ctype = "unknown"
        status, note = "manual", "未处理"

        # ①-A tab 式任务点（"1学习目标 / 2微课视频 / 3章节测验"）→ 逐个标签处理，按内容分派
        tabs = self._quiz_tabs(page)
        if tabs:
            kinds = "、".join(f"{t[2]}({t[3]})" for t in tabs)
            log.info(f"  发现 {len(tabs)} 个任务点标签：{kinds}")
            handled = 0
            page_i = page
            for i in range(len(tabs)):
                page_i, _ = self._open_chapter(ch)      # 每次重开，保证标签状态干净
                tabs_i = self._quiz_tabs(page_i)
                if i >= len(tabs_i):
                    break
                tframe, tel, ttext, tkind = tabs_i[i]
                if tkind == "goal":
                    log.info(f"   [{i + 1}/{len(tabs)}] {ttext} → 学习目标（非任务点），跳过")
                    continue
                log.info(f"   [{i + 1}/{len(tabs)}] {ttext}（{tkind}）→ 点开处理")
                try:
                    self.session.click_capture(page_i, tel, wait_s=6)
                except Exception as exc:  # noqa: BLE001
                    log.warning(f"     点击标签失败: {str(exc)[:60]}")
                page_i.wait_for_timeout(4000)
                if self._chapter_locked(page_i):
                    log.info("     该标签内容暂未开放 → 跳过")
                    continue
                if tkind == "video":
                    st_i, nt_i = self._play_video_here(page_i, ch)
                elif tkind == "quiz":
                    st_i, nt_i = self._handle_quiz(page_i, ch)
                else:
                    st_i, nt_i = self._browse_here(page_i, ch)
                log.info(f"     → {st_i} | {str(nt_i)[:70]}")
                if st_i in ("done", "failed"):
                    handled += 1
                hint_i = self._verify_chapter(ch)
                if isinstance(hint_i, int) and hint_i == 0:
                    break
            status = "done" if handled else "manual"
            note = f"处理 {handled}/{len(tabs)} 个任务点标签"
            ctype = "quiz"
            page = page_i
            # 测验做完（或本就已完成）但节点仍未完成 → 说明还有视频/文档任务点，补做
            h_after = self._verify_chapter(ch)
            if isinstance(h_after, int) and h_after > 0:
                log.info(f"  测验标签处理后仍有 {h_after} 个任务点未完成 → 检查页内视频/文档")
                page_v, cards_v = self._open_chapter(ch)
                if cards_v is not None:
                    t_v = course_mod.card_type(cards_v)
                    if t_v in ("video", "document"):
                        s_v, n_v = self._handle_card(page_v, cards_v, t_v, ch)
                        if s_v != "skipped":
                            ctype, status = t_v, s_v
                            note = f"{note}；另：{n_v}"
                            m_v = re.search(r"rate=([\d.]+)x", n_v or "")
                            if m_v:
                                rate_used = float(m_v.group(1))
                    else:
                        log.info(f"  页内任务点类型为 {t_v}，未做额外处理")
        elif (pm := self._page_tabs(page)) and not tabs:
            # ①-B 章节内多页（页签是纯数字，无"视频/测验"文字）→ 逐页处理
            # 这类章节平台端 totalcount 可能为 2，只做第 1 页会永远停在 1/2
            # 注意：切页后内容帧里可能暂时看不到视频，card_type 会误判成 quiz，
            # 因此这里不靠 card_type，而是「先探视频元素 → 再找答题入口 → 最后按类型兜底」
            log.info(f"  发现 {len(pm)} 个内容页签（纯数字）→ 逐页处理")
            handled = 0
            last = ("manual", "多页均未处理")
            for idx in range(len(pm)):
                p_i, cards_i = self._open_chapter(ch)
                if idx > 0:
                    if self._goto_page(p_i, idx) is None:
                        log.warning(f"   [第 {idx + 1} 页] 切换失败或内容帧未就绪 → 跳过")
                        continue
                    cards_i = course_mod.find_content_frame(p_i)
                if self._chapter_locked(p_i):
                    log.info(f"   [第 {idx + 1} 页] 暂未开放 → 跳过")
                    continue

                _, vel = video_mod.find_video_element(p_i, timeout_s=10)
                if vel is not None:
                    log.info(f"   [第 {idx + 1}/{len(pm)} 页] 检测到视频 → 播放")
                    s_i, n_i = self._play_video_here(p_i, ch)
                else:
                    entry_i = self._find_quiz_entry(p_i)
                    if entry_i is not None:
                        log.info(f"   [第 {idx + 1}/{len(pm)} 页] 检测到答题入口 → 处理")
                        qp = p_i
                        try:
                            qp, _ = self.session.click_capture(p_i, entry_i, wait_s=12)
                            qp.wait_for_timeout(3000)
                        except Exception as exc:  # noqa: BLE001
                            log.warning(f"     打开答题入口失败: {str(exc)[:60]}")
                        s_i, n_i = self._handle_quiz(qp, ch)
                    elif cards_i is not None:
                        t_i = course_mod.card_type(cards_i)
                        log.info(f"   [第 {idx + 1}/{len(pm)} 页] 无视频/答题入口，类型 {t_i} → 处理")
                        s_i, n_i = self._handle_card(p_i, cards_i, t_i, ch)
                    else:
                        log.info(f"   [第 {idx + 1}/{len(pm)} 页] 未识别到可处理内容 → 跳过")
                        continue
                log.info(f"     → {s_i} | {str(n_i)[:70]}")
                last = (s_i, n_i)
                if s_i in ("done", "failed"):
                    handled += 1
                # 逐页处理时只做一次快速复核（还有下一页要做，不必每页白等 3 分钟）；
                # 全部页处理完后，外层还会再做一次完整复核。
                hint_i = self._verify_chapter(ch, rounds=1)
                if isinstance(hint_i, int) and hint_i == 0:
                    log.info("     平台已标记完成，后续页签无需处理")
                    break
            status, note = last[0], f"多页处理 {handled}/{len(pm)} 页；{last[1]}"
            ctype = "multi"
        elif (entry := self._find_quiz_entry(page)) is not None:
            log.info("  页内有测验/作业入口 → 优先处理（该课任务点可能是测验而非视频）")
            self.task_count += 1
            self.humanizer.scroll(page)
            qpage = page
            try:
                qpage, _ = self.session.click_capture(page, entry, wait_s=12)
                qpage.wait_for_timeout(3000)
            except Exception as exc:  # noqa: BLE001
                log.warning(f"  点击测验入口失败: {str(exc)[:70]}")
            status, note = self._handle_quiz(qpage, ch)
            ctype = "quiz"
            h0 = self._verify_chapter(ch)
            if isinstance(h0, int) and h0 > 0:
                log.info(f"  测验后仍有 {h0} 个任务点未完成 → 继续处理页内视频/文档")
                page2, cards2 = self._open_chapter(ch)
                if cards2 is not None:
                    t2 = course_mod.card_type(cards2)
                    if t2 in ("video", "document"):
                        s2, n2 = self._handle_card(page2, cards2, t2, ch)
                        if s2 != "skipped":
                            ctype, status, note = t2, s2, f"{note}；另：{n2}"
                            m = re.search(r"rate=([\d.]+)x", n2 or "")
                            if m:
                                rate_used = float(m.group(1))
        elif cards is not None:
            ctype = course_mod.card_type(cards)
            status, note = self._handle_card(page, cards, ctype, ch)
            m = re.search(r"rate=([\d.]+)x", note or "")
            if m:
                rate_used = float(m.group(1))
        elif self._type_from_title(ch.title) == "quiz":
            ctype = "quiz"
            self.task_count += 1
            self.humanizer.scroll(page)
            status, note = self._handle_quiz(page, ch)
        else:
            dump = f"{self.cfg.output_dir}/structure_ch{ch.id}.txt"
            course_mod.dump_structure(page, dump, log)
            status, note = "manual", f"未找到任务点容器（结构已导出 {dump}）"

        hint = self._verify_chapter(ch)

        # 兜底：用了倍速但平台没登记（实测平台按真实时间核验）→ 用 1 倍速重播一次
        if rate_used > 1.0 and isinstance(hint, int) and hint > 0:
            log.warning(f"  平台未登记倍速播放（rate={rate_used}x）→ 改用 1 倍速重播一次")
            page2, cards2 = self._open_chapter(ch)
            if cards2 is not None and course_mod.card_type(cards2) == "video":
                self.task_count += 1
                status2, note2 = video_mod.play_video(
                    self.session, self.cfg, self.log, page2, self.popup, None,
                    force_rate=1.0, keepalive=self._ensure_session,
                )
                status, note = status2, f"{note}；1x 重播：{note2}"
                hint = self._verify_chapter(ch)

        if hint is not None:
            if hint == 0:
                note += "；复核：平台已标记完成"
                if status == "failed":
                    status = "done"
            else:
                note += f"；复核：平台仍显示 {hint} 个未完成"
                if status == "done":
                    status = "failed"
        self.report.add(ch.title, f"[{TYPE_LABEL.get(ctype, ctype)}] {ch.title[:50]}", status, note)
        log.info(f"  结果: {status} | {note}")
        # 明确区分"处理过了"和"平台已登记完成"，并刷新平台口径进度
        if status == "done":
            self.done_this_run.append(ch.title)
            log.info("  ✔ 本知识点已完成（平台已登记）")
            self.failed_chapters = [c for c in self.failed_chapters if c.id != ch.id]
        elif status in ("failed", "manual"):
            self.failed_this_run.append(ch.title)
            if all(c.id != ch.id for c in self.failed_chapters):
                self.failed_chapters.append(ch)      # 进入重试队列，不会直接跳过
            label = "需人工" if status == "manual" else "未通过复核"
            log.info(f"  ✘ 本知识点{label}：{note[:80]}")
            log.info("     → 已加入重试队列，本轮末尾会再试一次")
        self._refresh_platform_progress(self._chapters_cache or [ch], quiet=False)

    @staticmethod
    def _type_from_title(title):
        t = title or ""
        if any(k in t for k in ("测验", "测试", "作业", "考试")):
            return "quiz"
        return "unknown"

    def _limit_reached(self):
        """安全限制：任务点数量上限 / 单次运行时长上限。"""
        cfg = self.cfg
        mx = int(cfg.safety.max_tasks_per_run or 0)
        if mx and self.task_count >= mx:
            self.stop_reason = f"已达单次任务点数上限 {mx}"
            return True
        limit_min = float(cfg.safety.max_runtime_min or 0)
        if limit_min and (time.time() - self.start_ts) / 60.0 >= limit_min:
            self.stop_reason = f"已达单次运行时长上限 {limit_min:.0f} 分钟"
            return True
        return False

    def _handle_card(self, page, cards, ctype, ch):
        log, cfg = self.log, self.cfg
        self.task_count += 1
        if getattr(self.args, "only", "") and ctype != self.args.only:
            return "skipped", f"被 --only={self.args.only} 过滤"
        if not self._ensure_session("任务点"):
            return "manual", "任务点处理前检测到掉线且未能恢复"
        # 拟人化：处理任务点前随机滚动页面
        self.humanizer.scroll(page)
        try:
            if ctype in ("video", "audio"):
                return video_mod.play_video(self.session, cfg, log, page, self.popup, None)
            if ctype == "document":
                return document_mod.browse_document(self.session, cfg, log, page, self.popup, None)
            if ctype == "quiz":
                return self._handle_quiz(page, ch)
            return "manual", "无法识别任务点类型（结构可能更新）"
        except TimeoutError as exc:
            return "manual", str(exc)[:100]
        except Exception as exc:  # noqa: BLE001
            log.exception("任务点处理异常")
            return "failed", f"异常: {str(exc)[:100]}"

    def _quiz_frame(self, page):
        """定位真正的答题帧：优先 work/doHomeWorkNew，其次是 modules/work 包装帧。"""
        wrapper = None
        for f in course_mod.all_frames(page):
            try:
                url = (f.url or "").lower()
            except Exception:  # noqa: BLE001
                continue
            if "dohomework" in url:
                return f
            if wrapper is None and "modules/work" in url:
                wrapper = f
        return wrapper

    def _quiz_pages(self, page):
        """当前页 + 所有标签页（答题页可能在新标签里打开）。"""
        out = [page]
        try:
            for p in self.session.context.pages:
                if p not in out:
                    out.append(p)
        except Exception:  # noqa: BLE001
            pass
        return out

    def _locate_quiz(self, page):
        """在所有标签页里找答题帧，返回 (所在页, 答题帧)；找不到返回 (None, None)。"""
        for p in self._quiz_pages(page):
            qf = self._quiz_frame(p)
            if qf is not None:
                return p, qf
        return None, None

    def _handle_quiz(self, page, ch):
        # 1) 答题页往往已自动在某个 frame/标签页里打开（work/doHomeWorkNew）
        for attempt in range(4):
            host, qf = self._locate_quiz(page)
            if host is not None and host is not page:
                page = host          # 答题页在新标签 → 切过去
            if qf is not None:
                ready = False
                for _ in range(3):
                    try:
                        if qf.query_selector(
                            "div.TiMu, div.newTiMu, div.singleQuesId, [class*='num_option'], "
                            "input[type=radio], input[type=checkbox], textarea"
                        ):
                            ready = True
                            break
                    except Exception:  # noqa: BLE001
                        break
                    page.wait_for_timeout(2000)
                if ready:
                    self.log.info(f"  答题页已就绪（{qf.url[:70]}），开始处理")
                    return quiz_mod.run_quiz(
                        self.session, self.cfg, self.log, qf, self.popup,
                        self.bank, self.online, self.humanizer,
                    )
            if attempt and attempt % 4 == 0:
                urls = []
                for p in self._quiz_pages(page):
                    try:
                        urls.append((p.url or "")[:60])
                    except Exception:  # noqa: BLE001
                        urls.append("?")
                self.log.info(f"  等待答题页加载…（第 {attempt} 次，标签页: {urls}）")
            page.wait_for_timeout(2500)
        # 2) 兜底：找答题入口按钮
        for frame in course_mod.all_frames(page):
            for sel in ['a:has-text("开始答题")', 'a:has-text("去答题")', 'a:has-text("答题")',
                        'button:has-text("开始答题")', 'a[href*="/work/"]', 'a:has-text("章节测验")']:
                try:
                    el = frame.query_selector(sel)
                    if el is not None and el.is_visible():
                        target, _ = self.session.click_capture(page, el, wait_s=12)
                        target.wait_for_timeout(3500)
                        self.popup.tick(target)
                        return quiz_mod.run_quiz(
                            self.session, self.cfg, self.log, target, self.popup,
                            self.bank, self.online, self.humanizer,
                        )
                except Exception:  # noqa: BLE001
                    continue
        return "manual", "未找到答题入口，请人工打开该章节测验"

    def _wait_keeping_alive(self, seconds):
        """等待期间保持页面活跃。

        平台按心跳累计观看时长、并按会话状态登记任务点；页面长时间无人操作时
        上报容易缺漏（实测纯视频章节播完后 40 秒内仍显示未完成）。
        这里用鼠标微动 + 轻微滚动模拟"人还在看"，且滚动幅度很小，不会把视频拖离可视区。
        """
        page = self.session.page
        end = time.time() + max(1, seconds)
        i = 0
        while time.time() < end:
            remain = end - time.time()
            time.sleep(max(1.0, min(6.0, remain)))
            i += 1
            try:
                box = page.viewport_size or {"width": 1280, "height": 720}
                page.mouse.move(
                    140 + (i * 89) % max(1, box["width"] - 280),
                    140 + (i * 57) % max(1, box["height"] - 280),
                    steps=8,
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                page.evaluate("(d) => window.scrollBy(0, d)", 5 if i % 2 else -5)
            except Exception:  # noqa: BLE001
                pass

    def _verify_chapter(self, ch, rounds=5):
        """复核知识点状态。

        优先用平台接口（准确），并做延迟重试——实测视频播完后平台登记有延迟，
        只查一次会误判为"未完成"；页面 DOM 标记同样可能滞后，仅作兜底。
        rounds=1 时只查一次（多页章节逐页处理时用，避免每页白等）。
        返回：0=已完成 / >0=未完成数 / -1=无任务点 / None=无法判定
        """
        log = self.log
        delays = (0, 15, 25, 60, 90)[: max(1, int(rounds))]
        # 1) 接口复核：失败时逐步延长等待（0/15/25 秒快速判定，之后 60/90 秒兜底）
        #    实测纯视频章节的登记可达 2 分钟以上；成功的通常第一次就返回，不额外耗时。
        for attempt, delay in enumerate(delays):
            if delay and attempt:
                log.info(f"  平台登记可能延迟，{delay} 秒后再次复核（保持页面活跃）…")
                self._wait_keeping_alive(delay)
            try:
                st = course_mod.chapter_status(self.session, self.course, [ch.id], log)
            except Exception:  # noqa: BLE001
                st = {}
            v = (st or {}).get(ch.id)
            if not v:
                break
            total = int(v.get("total", 0) or 0)
            unfinish = int(v.get("unfinish", 0) or 0)
            if total == 0 and unfinish == 0:
                course_mod.update_chapter_hint(ch.id, -1)
                return -1
            course_mod.update_chapter_hint(ch.id, unfinish)
            if unfinish == 0:
                return 0
            if attempt >= len(delays) - 1:
                return unfinish          # 全部轮次都未完成 → 确实没完成
        # 2) DOM 兜底（可能滞后）
        try:
            page = self._tree_page()
            hint = course_mod.chapter_state(page, ch.id)
            if hint is not None:
                course_mod.update_chapter_hint(ch.id, hint)
            return hint
        except Exception:  # noqa: BLE001
            return None

    # ---------------- 任务中心（教学任务，顺序解锁） ----------------
    def process_task_center(self):
        log, cfg = self.log, self.cfg
        page = self.session.page
        entry = None
        for sel in ['text=任务中心', 'text=教学任务', 'a:has-text("任务中心")', '[class*="taskCenter"]']:
            try:
                el = page.query_selector(sel)
                if el is not None and el.is_visible():
                    entry = el
                    break
            except Exception:  # noqa: BLE001
                continue
        if entry is None:
            log.info("未发现「任务中心/教学任务」入口（本课程可能只有章节任务点）")
            return
        target, _ = self.session.click_capture(page, entry, wait_s=12)
        target.wait_for_timeout(3500)
        self.popup.tick(target)
        center_url = target.url
        log.info(f"进入任务中心: {center_url[:80]}")

        max_rounds = 200
        for rnd in range(1, max_rounds + 1):
            self.popup.tick(target)
            self.session.keep_alive()
            card = self._first_actionable_card(target)
            if card is None:
                log.info("任务中心：未发现更多可继续的任务点")
                break
            title = self._card_title(card)
            ptype = course_mod.guess_type(title)
            log.info(f"  [第{rnd}轮] 任务点: {title[:50]} (推断类型 {ptype})")
            task_page, is_new = self.session.click_capture(target, card, wait_s=12)
            task_page.wait_for_timeout(3000)
            self.popup.tick(task_page)
            try:
                if ptype == "video":
                    status, note = video_mod.play_video(
                        self.session, cfg, log, task_page, self.popup, None,
                        keepalive=self._ensure_session,
                    )
                elif ptype in ("document", "read"):
                    status, note = document_mod.browse_document(self.session, cfg, log, task_page, self.popup, None)
                elif ptype == "quiz":
                    status, note = quiz_mod.run_quiz(self.session, cfg, log, task_page, self.popup, self.bank)
                else:
                    status, note = "manual", "未能判断任务点类型，请人工处理"
            except Exception as exc:  # noqa: BLE001
                status, note = "failed", f"异常: {str(exc)[:100]}"
            self.report.add("任务中心", f"[{TYPE_LABEL.get(ptype, ptype)}] {title[:50]}", status, note)
            log.info(f"     结果: {status} | {note}")
            if is_new:
                self.session.close_page(task_page)
            # 回到任务中心重新扫描（平台按顺序解锁）
            try:
                target.goto(center_url, wait_until="domcontentloaded")
                target.wait_for_timeout(2500)
            except Exception:  # noqa: BLE001
                break

    def _first_actionable_card(self, page):
        for sel in ['[class*="plan"]', '[class*="task"]', '[class*="card"]', 'li']:
            try:
                elements = page.query_selector_all(sel)
            except Exception:  # noqa: BLE001
                continue
            for el in elements:
                try:
                    if not el.is_visible():
                        continue
                    text = (el.inner_text() or "").strip().replace("\n", " ")
                    if not text or len(text) > 120:
                        continue
                    if any(k in text for k in ("去学习", "继续学习", "未完成", "进行中", "开始学习")):
                        return el
                except Exception:  # noqa: BLE001
                    continue
        return None

    @staticmethod
    def _card_title(card):
        try:
            return (card.inner_text() or "").strip().replace("\n", " ")[:80]
        except Exception:  # noqa: BLE001
            return "任务点"
