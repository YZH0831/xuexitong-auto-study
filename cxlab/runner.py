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
        self.exit_code = 0
        # 平台进度（以接口为准，供日志与报告对照）
        self.platform_done = 0
        self.platform_total = 0
        self.done_this_run = []
        self.skipped_this_run = []      # 本次新完成的知识点标题
        self.failed_this_run = []    # 本次失败/需人工的标题
        self.failed_chapters = []    # 失败重试队列（本章节对象）
        self._chapters_cache = []
        self._submit_blocked_ids = set()  # 提交被平台拦截的章节：本 run 不再自动重提
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
                self.exit_code = 1
                return self.exit_code
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
                    self.exit_code = 2
                    log.error(f"触发风控保护，停止后续课程：{exc}")
                    self.report.add(course.name, "风控中止", "manual", str(exc)[:100])
                    break
                except Exception as exc:  # noqa: BLE001
                    self.exit_code = 1
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
            self.exit_code = 130
            log.warning("用户中断运行")
        except RiskControlStop as exc:
            self.exit_code = 2
            log.error(f"触发风控保护，已停止：{exc}")
            if self.report is not None:
                self.report.add("全局", "风控中止", "manual", str(exc)[:120])
        except Exception as exc:  # noqa: BLE001
            self.exit_code = 1
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
        return self.exit_code

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
    @staticmethod
    def _task_counts(status_map):
        """汇总接口的任务点数量，而不是把每个知识点误算成一个任务点。"""
        done = total = 0
        for value in status_map.values():
            count = int(value.get("total", 0) or 0)
            unfinished = int(value.get("unfinish", 0) or 0)
            finished = int(value.get("finish", max(0, count - unfinished)) or 0)
            total += count
            done += max(0, min(count, finished))
        return done, total

    def _refresh_platform_progress(self, chapters, quiet=False):
        """用接口刷新"平台已完成/总任务点"，这是手机端与网页端显示的一致口径。"""
        try:
            st = course_mod.chapter_status(self.session, self.course, [c.id for c in chapters], self.log)
        except Exception:  # noqa: BLE001
            st = {}
        if not st:
            return
        done, total = self._task_counts(st)
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
            # 选择条件无效时不能扩大为全部课时，否则一次手误会执行整门课程。
            self.log.warning(f"【{course.name}】课时表达式「{spec}」未匹配到课时 → 跳过本课程，请检查表达式")
            return []
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
        # 锁定状态只属于本次打开尝试；任何提前返回都不能污染下一章节。
        self._last_locked = False
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
            self.platform_done, self.platform_total = self._task_counts(st)
        else:
            log.info("平台状态：接口不可用，改用章节树标记判定")

        # 小圆是调度权威：绿色=完成，橙色 N=待完成，橙色 0=直接跳过。
        circle = course_mod.discover_chapters(page, log, force=True)
        if circle:
            cmap = {c.id: c.hint for c in circle}
            counts = {"green": 0, "orange": 0, "orange0": 0, "unknown": 0}
            for c in chapters:
                hint = cmap.get(c.id)
                if hint is None:
                    counts["unknown"] += 1
                    continue
                c.hint = hint
                if hint == 0:
                    counts["green"] += 1
                elif hint == -1:
                    counts["orange0"] += 1
                else:
                    counts["orange"] += 1
            log.info(
                f"小圆状态：绿色 {counts['green']} / 橙色待完成 {counts['orange']}"
                f" / 橙色0 {counts['orange0']}"
                + (f" / 未知 {counts['unknown']}" if counts["unknown"] else "")
            )

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
                status = self.process_chapter(ch)
                # 章节之间的最小间隔（带随机抖动）
                self.humanizer.task_interval(int(cfg.safety.min_task_interval_s))

                # 用户指令门控：小圆绿色才允许下一个；橙色 0 才可跳过。
                # 例外：本次打开仍识别为“敬请期待/即将上线”的占位章节，可先跳过；
                # --only 是用户显式只处理某一类任务，也不按整课门控。
                placeholder_skip = bool(status == "skipped" and getattr(self, "_placeholder_skip", False))
                if placeholder_skip:
                    log.info("  占位章节按规则跳过（下次运行仍会重新探测是否能做）")
                elif not getattr(self.args, "only", "") and ch.hint not in (0, -1):
                    state = ("橙色 0" if ch.hint == -1
                             else (f"橙色 {ch.hint}" if ch.hint else "状态未知"))
                    log.error(
                        f"  小圆门控未通过（当前 {state}）→ 不进入下一个任务；"
                        "请人工查看该知识点或等平台登记后重跑"
                    )
                    self.report.add(ch.title, "小圆门控", "manual",
                                    f"小圆未变绿且不是橙色 0（当前 {state}），已停止后续章节")
                    if ch.title not in self.failed_this_run:
                        self.failed_this_run.append(ch.title)
                    if all(c.id != ch.id for c in self.failed_chapters):
                        self.failed_chapters.append(ch)
                    aborted = True
                    break
            # 平台拦截过提交的题目本 run 不再自动重提，避免连续交白卷。
            pending = [c for c in self.failed_chapters
                       if c.hint != 0 and str(c.id) not in self._submit_blocked_ids]
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

    # 答题入口关键词（顺序即优先级：越靠前越像“本章节的测验”）
    QUIZ_ENTRY_KWS = ('章节测验', '章节测试', '单元测验', '随堂测验', '章节作业',
                      '测验', '作业', '考试', '开始答题', '继续答题', '去答题', '去完成')
    JS_QUIZ_CANDIDATES = r"""
    (kws) => {
      // 收集“能打开答题页”的入口并按相似度评分，返回前 6 个。
      // 教训（国家安全教育课实测）：章节名叫「2.5 章节测验」时，页面里到处都是
      // 这几个字——标题栏、侧边目录、顶栏跳转——旧的“第一个命中”逻辑点中的
      // 是标题/目录项，答题页根本打不开，程序却在原地空找两分钟，
      // 用户看到的就是“卡在那边不做题”。
      const bad = ['查看','已完成','已提交','重做','再做','成绩','得分','解析','答案','未开始',
                   '返回','上一节','下一节','目录','讨论','笔记','标注'];
      const banArea = 'header,footer,nav,aside,[class*="nav"],[class*="menu"],'
        + '[class*="footer"],[class*="header"],[class*="sidebar"],[class*="crumb"],'
        + '[class*="tab-bar"],[class*="Catalog"],[class*="catalog"],[class*="posChapter"],'
        + '[class*="subNav"],[class*="prev_title"],[class*="prev_next"],[class*="tips"]';
      const out = [];
      const seen = new Set();
      const els = Array.from(document.querySelectorAll('a,button,div,span,li,p'));
      for (const el of els) {
        const raw = (el.innerText || '').replace(/\s+/g, '').trim();
        if (!raw || raw.length > 16) continue;
        const kw = kws.find(k => raw.includes(k));
        if (!kw) continue;
        if (bad.some(b => raw.includes(b))) continue;
        if (el.offsetParent === null) continue;
        if (el.closest(banArea)) continue;
        let s = 0;
        if (raw === kw) s += 6;                          // 文字恰是“章节测验/作业”本体
        if (/开始|立即|进入|继续|去/.test(raw)) s += 4;   // “开始答题/继续作答/去完成”最像入口
        const lcls = (' ' + (typeof el.className === 'string' ? el.className : '') + ' ')
          .toLowerCase();
        if (/insert|attach|job|exam|test|homework/.test(lcls)) s += 5;  // 任务点卡片容器
        if (el.tagName === 'A' || el.tagName === 'BUTTON' || el.getAttribute('onclick')
            || getComputedStyle(el).cursor === 'pointer') s += 2;
        const childHit = Array.from(el.children).some(c =>
          ((c.innerText || '') + '').replace(/\s+/g, '').includes(kw));
        if (!childHit) s += 2; else s -= 2;              // 叶子优先；大容器降权
        const r = el.getBoundingClientRect();
        if (r.width * r.height > 120000) s -= 4;
        const target = el.querySelector('a, [onclick]') || el;
        if (seen.has(target)) continue;
        seen.add(target);
        out.push({el: target, s});
      }
      out.sort((a, b) => b.s - a.s);
      return out.slice(0, 6).map(o => o.el);
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

    # 内容都做完后，用于推进到下一块/提交的可见入口。
    # 顺序即优先级（评分随下标递减）：“提交”放最后，避免抢在翻块按钮前面误交卷。
    ADVANCE_MARKS = ("下一节", "下一章", "继续学习", "下一步", "提交")
    JS_ADVANCE_CANDIDATES = r"""
    (marks) => {
      // 收集所有可见的推进入口并按"像不像翻块按钮"评分，返回前 6 个。
      // 背景：超星页面多用 jQuery 事件委托绑定点击，按钮本身常常没有
      // onclick/role/cursor 特征；旧版"必须可点击 + 父元素带匹配子元素就跳过"
      // 的硬过滤会把父子全排除，表现为"看得到下一节，程序却找不到"。
      const bad = ['上一节', '上一章', '返回', '关闭', '取消', '上一题', '上一页'];
      const picked = [];
      const seen = new Set();
      const textOf = (el) => {
        let t = el.tagName === 'INPUT' ? (el.value || '') : (el.innerText || '');
        t = (t || '').replace(/\s+/g, '');
        if (!t) t = ((el.getAttribute('title') || '') + (el.getAttribute('aria-label') || ''))
          .replace(/\s+/g, '');
        return t;
      };
      const visible = (el) => {
        const r = el.getBoundingClientRect();
        if (r.width < 4 || r.height < 4) return false;
        const st = getComputedStyle(el);
        return st.display !== 'none' && st.visibility !== 'hidden'
          && st.pointerEvents !== 'none';
      };
      // 1) 文本命中的候选（不再硬性要求"可点击"：点子元素会冒泡到父级委托 handler）
      const els = Array.from(document.querySelectorAll(
        'a,button,input[type=button],input[type=submit],div,span,li'));
      for (const el of els) {
        if (seen.has(el)) continue;
        const txt = textOf(el);
        if (!txt || txt.length > 14) continue;
        if (bad.some(b => txt.includes(b))) continue;
        const mk = marks.findIndex(m => txt.includes(m));
        if (mk < 0) continue;
        if (!visible(el) || el.disabled === true) continue;
        let s = Math.max(0, 8 - mk * 2);              // 关键词越靠前越优先
        if (txt === marks[mk]) s += 6;                // 文字完全等于关键词，最像按钮本体
        const cls = (' ' + (typeof el.className === 'string' ? el.className : '')
          + ' ' + (el.id || '')).toLowerCase();
        if (/prev_next|next|jb_btn/.test(cls)) s += 4;
        if (['A', 'BUTTON', 'INPUT'].includes(el.tagName)) s += 2;
        if (el.getAttribute('onclick') || el.getAttribute('role') === 'button'
            || getComputedStyle(el).cursor === 'pointer') s += 2;
        const childMatch = Array.from(el.children).some(c => {
          const t = textOf(c);
          return t && marks.some(m => t.includes(m));
        });
        if (!childMatch) s += 3; else s -= 2;         // 叶子优先；大容器降权但不再直接排除
        const r = el.getBoundingClientRect();
        const area = r.width * r.height;
        if (area > 120000) s -= 4;                    // 近全屏的大容器基本不是按钮
        else if (area < 60000) s += 1;
        picked.push({el, s});
        seen.add(el);
      }
      // 2) 无文字的结构候选（箭头图标按钮：class/id 词根含 next / prevnext）
      for (const el of Array.from(document.querySelectorAll('a,button,div,span,li'))) {
        if (seen.has(el)) continue;
        const cls = ' ' + (typeof el.className === 'string' ? el.className : '')
          + ' ' + (el.id || '') + ' ';
        const tokens = cls.toLowerCase().split(/[\s_:-]+/).filter(Boolean);
        const hit = tokens.some(tk => tk === 'next' || tk === 'prevnext'
          || tk.startsWith('next') || tk.endsWith('next'));
        if (!hit) continue;
        if (!visible(el) || el.disabled === true) continue;
        const txt = textOf(el);
        if (txt && bad.some(b => txt.includes(b))) continue;  // "上一节"类同样带 next 词根
        seen.add(el);
        picked.push({el, s: 5});                      // 图标按钮排在文字精确命中之后
      }
      picked.sort((a, b) => b.s - a.s);
      return picked.slice(0, 6).map(o => o.el);
    }
    """

    JS_PAGE_STATE = r"""
    () => {
      // 页面指纹：标题 / 激活页签 / 内容 iframe / 正文摘要。
      // 数字统一归为 #：播放器计时文案每秒都在变，否则指纹永远"有变化"。
      const clean = (s) => (s || '').replace(/\s+/g, '').replace(/\d+/g, '#');
      const t = document.querySelector('.prev_title');
      const active = Array.from(document.querySelectorAll('li.active'))
        .map(e => clean(e.innerText).slice(0, 10)).join('|');
      const ifr = Array.from(document.querySelectorAll('iframe'))
        .map(i => clean(i.getAttribute('src')).slice(0, 100)).slice(0, 4).join('||');
      const body = clean(document.body ? document.body.innerText : '').slice(0, 300);
      return (t ? clean(t.innerText) : '') + '@' + active + '@' + ifr + '@' + body;
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

    def _chapter_title(self, page):
        """当前知识点标题，用于区分「下一节」是翻内容块还是跳知识点。"""
        for f in course_mod.all_frames(page):
            try:
                t = f.evaluate(
                    "() => { const e = document.querySelector('.prev_title');"
                    " return e ? (e.innerText || '').trim() : ''; }"
                )
            except Exception:  # noqa: BLE001
                continue
            if t:
                return str(t).strip()
        return ""

    def _advance_candidates(self, page):
        """收集各帧里所有可见的推进入口（下一节/继续/提交/图标箭头），按相似度排序。

        旧版只取文档序第一个命中，既容易漏（过滤过严）也容易抓错（推荐位里的
        "继续学习"大卡片）；现在综合评分、逐个候选点击并验证效果。
        """
        out = []
        for f in course_mod.all_frames(page):
            try:
                arr = f.evaluate_handle(self.JS_ADVANCE_CANDIDATES, self.ADVANCE_MARKS)
                count = int(arr.evaluate("a => a.length") or 0)
            except Exception:  # noqa: BLE001
                continue
            for i in range(count):
                try:
                    el = arr.evaluate_handle("(a, i) => a[i]", i).as_element()
                except Exception:  # noqa: BLE001
                    el = None
                if el is None:
                    continue
                try:
                    desc = (el.inner_text() or el.get_attribute("class") or "?").strip()[:14]
                except Exception:  # noqa: BLE001
                    desc = ""
                out.append((desc or "(图标按钮)", el))
        return out[:8]

    def _page_state(self, page):
        """页面指纹；发生变化即认为点击起了作用（翻块/切页签/弹窗/换内容帧）。"""
        parts = []
        for f in course_mod.all_frames(page):
            try:
                sig = f.evaluate(self.JS_PAGE_STATE)
            except Exception:  # noqa: BLE001
                continue
            if sig:
                parts.append(str(sig))
        return " ;; ".join(parts)

    def _click_next_block(self, page):
        """点「下一节」等推进入口；只有页面确认发生变化才返回 True。

        三层保险：
        1) 多候选评分排序（文本 + 结构图标），不再依赖单一旧版 class；
        2) 常规点击失败（被遮挡/超时）→ JS 合成点击兜底，事件冒泡触发委托 handler；
        3) 点完用页面指纹验证效果；没变化就换下一个候选，不再"点一次就算成功"。
        """
        cands = self._advance_candidates(page)
        if not cands:
            return False
        state0 = self._page_state(page)
        try:
            pages_before = {id(p) for p in self.session.context.pages}
        except Exception:  # noqa: BLE001
            pages_before = set()
        for desc, el in cands:
            clicked = False
            try:
                el.scroll_into_view_if_needed(timeout=2500)
            except Exception:  # noqa: BLE001
                pass
            try:
                if getattr(self.session, "humanizer", None) is not None:
                    clicked = bool(self.session.humanizer.click(page, el, timeout=6000))
                else:
                    el.click(timeout=6000)
                    clicked = True
            except Exception as exc:  # noqa: BLE001
                self.log.debug(f"  推进入口[{desc}]常规点击失败: {str(exc)[:60]}")
            if not clicked:
                # 合成点击：绕开 Playwright 的可点性检查（遮挡/出视口），直接派发 DOM 事件
                try:
                    el.evaluate(
                        "e => { try { e.scrollIntoView({block: 'center'}); } catch (_) {}"
                        " e.click(); }"
                    )
                    clicked = True
                except Exception as exc:  # noqa: BLE001
                    self.log.debug(f"  推进入口[{desc}]合成点击失败: {str(exc)[:60]}")
                    continue
            # 点击后轮询数秒：翻块 / 新标签页 / 弹窗引起的指纹变化都算推进成功
            for _ in range(8):
                page.wait_for_timeout(750)
                self.popup.tick(page)
                try:
                    for p in self.session.context.pages:
                        if id(p) not in pages_before:
                            try:
                                p.bring_to_front()
                            except Exception:  # noqa: BLE001
                                pass
                            self.log.info(f"  「下一节」类入口在新标签页打开（来自 [{desc}]）")
                            return True
                except Exception:  # noqa: BLE001
                    pass
                if self._page_state(page) != state0:
                    self.log.info(f"  已点击推进入口: {desc}")
                    return True
            # 当前候选点了没效果 → 换下一个（可能是页面上另一个"下一节"入口）
        return False

    def _continue_next_block(self, ch, page, max_steps=3):
        """做完测验/视频后用「下一节」继续本章节剩余内容块。

        实测「1.1 导论」：页内先给了答题入口，程序做完 3 题后就在原地复核，
        本知识点后面还有内容却没继续——用户看到的是"答完题卡住不动"。
        部分章节的内容块不用 1/2 页签，只能靠「下一节」切换。
        返回是否处理到了新内容。
        """
        log = self.log
        did = False
        for step in range(1, max_steps + 1):
            before = self._chapter_title(page)
            if not self._click_next_block(page):
                return did
            after = self._chapter_title(page)
            if before and after and before != after:
                log.info(f"  「下一节」跳到了其它知识点（{after}）→ 回到原章节继续")
                self._open_chapter(ch)
                return did
            # 点击生效后内容帧要几秒才重新注册；找不到就直接放弃会误判"没新内容"
            cards = None
            for _ in range(5):
                cards = course_mod.find_content_frame(page)
                if cards is not None:
                    break
                page.wait_for_timeout(1000)
                self.popup.tick(page)
            if cards is None:
                log.info("  等待后仍未找到新内容帧 → 停止继续翻块")
                return did
            entry_host = self._try_open_quiz(page)
            if entry_host is not None:
                log.info(f"  「下一节」第 {step} 块：有答题入口 → 处理")
                s, n = self._handle_quiz(entry_host, ch)
            else:
                t = course_mod.card_type(cards)
                log.info(f"  「下一节」第 {step} 块：类型 {t} → 处理")
                s, n = self._handle_card(page, cards, t, ch)
            log.info(f"     → {s} | {str(n)[:70]}")
            if s == "manual":
                log.info("     当前块需人工处理 → 不继续点击提交/下一节，避免误交")
                return did
            if s in ("done", "failed"):
                did = True
            h = self._verify_chapter(ch, rounds=1)
            if isinstance(h, int) and h == 0:
                return True
        return did

    def _should_handle(self, ctype):
        """所有分派路径共用的 --only 过滤，避免标签页/任务中心绕过限制。"""
        only = getattr(self.args, "only", "")
        if not only:
            return True
        normalized = {"audio": "video", "read": "document"}.get(ctype, ctype)
        return normalized == only

    def _play_video_here(self, page, ch):
        """在当前标签页里播视频（新版知识点页的视频在 knowledge/cards 帧内）。"""
        if not self._should_handle("video"):
            return "skipped", f"被 --only={self.args.only} 过滤"
        live = self._handle_live(page, ch)
        if live is not None:
            return live
        self.task_count += 1
        self.humanizer.scroll(page)
        return video_mod.play_video(
            self.session, self.cfg, self.log, page, self.popup, None,
            keepalive=self._ensure_session,
        )

    def _browse_here(self, page, ch):
        """当前标签页是文档/正文 → 滚动浏览。"""
        if not self._should_handle("document"):
            return "skipped", f"被 --only={self.args.only} 过滤"
        live = self._handle_live(page, ch)
        if live is not None:
            return live
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

    def _quiz_entry_candidates(self, page):
        """收集各帧里的答题入口候选（内容帧优先），按“最像真入口”排序。

        旧的 _find_quiz_entry 按文档序取第一个命中：章节本身就叫「章节测验」时，
        命中的往往是标题/侧边目录/顶栏跳转，点了打不开答题页。
        """
        frames = course_mod.all_frames(page)

        def frame_order(f):
            try:
                if f is page.main_frame:
                    return 2      # 主框架里的“测验”字样多半是标题/目录，放最后
            except Exception:  # noqa: BLE001
                pass
            try:
                url = (f.url or "").lower()
            except Exception:  # noqa: BLE001
                url = ""
            if "cards" in url or "knowledge" in url:
                return 0          # 内容帧里的入口最可能是真卡片
            return 1

        frames.sort(key=frame_order)
        out = []
        for f in frames:
            try:
                arr = f.evaluate_handle(self.JS_QUIZ_CANDIDATES, list(self.QUIZ_ENTRY_KWS))
                count = int(arr.evaluate("a => a.length") or 0)
            except Exception:  # noqa: BLE001
                continue
            for i in range(count):
                try:
                    el = arr.evaluate_handle("(a, i) => a[i]", i).as_element()
                except Exception:  # noqa: BLE001
                    el = None
                if el is None:
                    continue
                try:
                    desc = (el.inner_text() or "").strip()[:12]
                except Exception:  # noqa: BLE001
                    desc = ""
                out.append((desc or "(无文字入口)", el))
        return out[:8]

    def _try_open_quiz(self, page):
        """逐个尝试答题入口，验证答题帧真的出现才算打开成功；返回答题宿主页。

        旧版点一个候选就交给 _handle_quiz 空找 2 分钟；现在点了没反应会自动
        换下一个候选（这就是“卡在那边不做题”的主因）。
        """
        # 0) 答题页可能已经内嵌在当前页（insertwork/work 帧直接加载）→ 无需点击
        host, _ = self._locate_quiz(page)
        if host is not None:
            self.log.info("  答题页已内嵌在当前页 → 直接处理")
            return host
        cands = self._quiz_entry_candidates(page)
        if not cands:
            return None
        for desc, el in cands[:6]:
            try:
                before = {id(p) for p in self.session.context.pages}
            except Exception:  # noqa: BLE001
                before = set()
            try:
                qp, _ = self.session.click_capture(page, el, wait_s=6)
            except Exception as exc:  # noqa: BLE001
                self.log.debug(f"  答题入口[{desc}]点击失败: {str(exc)[:60]}")
                continue
            # 等答题帧加载（慢网时太短会误判“没打开”，进而误关刚开的答题标签）
            qp.wait_for_timeout(3500)
            self.popup.tick(qp)
            host, _ = self._locate_quiz(qp)
            if host is not None:
                self.log.info(f"  已打开答题页（入口: {desc}）")
                return host
            # 这个入口没打开答题页 → 关掉可能误开的新标签，换下一个候选
            try:
                for p in self.session.context.pages:
                    if id(p) not in before:
                        self.session.close_page(p)
            except Exception:  # noqa: BLE001
                pass
            self.log.debug(f"  答题入口[{desc}]未打开答题页 → 换下一个候选")
        return None

    def process_chapter(self, ch):
        log = self.log
        # 每次打开都重新判断；占位状态不跨运行缓存。
        self._placeholder_skip = False
        page, cards = self._open_chapter(ch)
        if getattr(self, "_last_locked", False):
            log.info("  结果: skipped | 章节未开放，跳过（非错误）")
            self.report.add(ch.title, f"[未开放] {ch.title[:50]}", "skipped", "章节未开放（平台限制）")
            self.skipped_this_run.append(ch.title)
            return "skipped"
        rate_used = 1.0
        ctype = "unknown"
        status, note = "manual", "未处理"
        # 记录本知识点处理前已做的任务数：结束时若一个任务都没做成，
        # 说明平台不可能“稍后补登记”，没必要干等 3 分钟复核。
        start_tasks = self.task_count

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
                    # 保留本次点击实际命中的页面，避免随后在旧答题标签页中查找。
                    page_i, _ = self.session.click_capture(page_i, tel, wait_s=6)
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
                # 标签/视频都做完仍未绿 → 章节里还有靠「下一节」切换的内容块
                h_tab = self._verify_chapter(ch, rounds=1)
                if isinstance(h_tab, int) and h_tab > 0:
                    log.info(f"  标签页处理后仍有 {h_tab} 个任务点未完成 → 用「下一节」继续剩余内容")
                    page_c, _ = self._open_chapter(ch)
                    if self._continue_next_block(ch, page_c, 3):
                        status, note = "done", f"{note}；已用「下一节」继续后续内容"
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
                    qp_i = self._try_open_quiz(p_i)
                    if qp_i is not None:
                        log.info(f"   [第 {idx + 1}/{len(pm)} 页] 检测到答题入口 → 处理")
                        s_i, n_i = self._handle_quiz(qp_i, ch)
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
        elif cards is not None and course_mod.card_type(cards) in ("video", "document"):
            # ②-A 视频/文档优先。原实现把「测验入口」排在前面，导致本页面既
            #     有视频又有作业入口时只做了作业、视频永远不播（用户反馈 1.1 导论）。
            ctype = course_mod.card_type(cards)
            status, note = self._handle_card(page, cards, ctype, ch)
            m = re.search(r"rate=([\d.]+)x", note or "")
            if m:
                rate_used = float(m.group(1))
            # 视频看完后，本节若还有测验/作业任务点，补做一次
            # （占位章节直接跳过，不去等那 3 分钟复核）
            h_v = self._verify_chapter(ch, rounds=1) if status != "skipped" else None
            if isinstance(h_v, int) and h_v > 0:
                if self._quiz_entry_candidates(page):
                    log.info(f"  视频完成后仍有 {h_v} 个任务点未完成 → 补做本章节测验/作业")
                    self.humanizer.scroll(page)
                    qpage = self._try_open_quiz(page)
                    if qpage is not None:
                        self.task_count += 1
                        s_q, n_q = self._handle_quiz(qpage, ch)
                        if s_q != "skipped":
                            ctype, status, note = "quiz", s_q, f"{note}；另：{n_q}"
                    else:
                        note += "；测验入口未打开答题页"
        elif (self._locate_quiz(page)[0] is not None or self._quiz_entry_candidates(page)):
            # ②-B 页内有测验/作业入口（或答题页已内嵌）→ 逐个候选尝试打开，
            #     点了没反应自动换下一个，不再“点错一次空找两分钟”。
            log.info("  页内有测验/作业入口 → 逐个尝试打开答题页")
            self.humanizer.scroll(page)
            qpage = self._try_open_quiz(page)
            if qpage is None:
                status, note = "manual", "未找到答题入口，请人工打开该章节测验"
            else:
                # 打开了才算真的做了任务；没打开时 task_count 不变，
                # 结尾才会走“快速复核”而不是干等 3 分钟。
                self.task_count += 1
                status, note = self._handle_quiz(qpage, ch)
            ctype = "quiz"
            # 快速判断即可：以前这里用完整 5 轮复核（约 3 分钟），做完题后干等，
            # 用户看到的现象就是「答完题程序不动了」。
            h0 = self._verify_chapter(ch, rounds=1)
            if isinstance(h0, int) and h0 > 0:
                log.info(f"  测验后仍有 {h0} 个任务点未完成 → 用「下一节」继续本章节剩余内容")
                # 「下一节」在知识点页上；quiz 可能是在新标签里答的，重开页面再找按钮
                page_n, _ = self._open_chapter(ch)
                if self._continue_next_block(ch, page_n, 3):
                    status, note = "done", f"{note}；已用「下一节」继续后续内容"
                else:
                    log.info("  「下一节」未带来新内容 → 退回处理页内视频/文档")
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

        if status == "skipped":
            # 找不到提交按钮 / 章节无需处理 → 不进入 3 分钟复核，直接进入下一节
            hint = None
            log.info("  跳过平台复核，直接进入下一节")
        elif status == "manual" and self.task_count == start_tasks:
            # 本知识点没做成任何任务（题没打开/内容没识别）→ 平台不会“稍后登记”，
            # 只快速复核一次即可；干等 3 分钟就是用户看到的“卡住不动”。
            log.info("  本知识点未执行任何任务 → 快速复核（不长时间等待平台登记）")
            hint = self._verify_chapter(ch, rounds=1)
        else:
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

        # 题目/视频/音频/文章都处理成功后，若小圆仍是橙色，先重扫当前页；
        # 仍有剩余时再找“提交/下一节”入口。manual/failed 不乱点提交。
        if status == "done" and isinstance(hint, int) and hint > 0:
            log.info(f"  内容处理后仍有 {hint} 个任务点未完成 → 重扫当前页剩余任务")
            r_status, r_note = self._rescan_current_tasks(ch, page)
            if r_status == "done":
                note += f"；重扫处理：{r_note}"
                hint = self._verify_chapter(ch)
            if isinstance(hint, int) and hint > 0:
                log.info(f"  重扫后仍有 {hint} 个任务点未完成 → 查找“提交/下一节/继续”入口")
                # page 可能停在答完题的旧标签页上；重开知识点页再找「下一节」
                page_c, _ = self._open_chapter(ch)
                if self._continue_next_block(ch, page_c, 3):
                    note += "；已尝试提交/下一节推进"
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
        # 占位章节（敬请期待/即将上线等）允许本轮跳过，但不持久缓存；下次仍会重开探测。
        self._placeholder_skip = bool(status == "skipped" and self._is_placeholder_note(note))
        # 明确区分"处理过了"和"平台已登记完成"，并刷新平台口径进度
        if status == "done":
            # 汇总只保留最终状态：重试成功后移除首轮失败记录，且同标题不重复计数。
            self.failed_this_run = [t for t in self.failed_this_run if t != ch.title]
            if ch.title not in self.done_this_run:
                self.done_this_run.append(ch.title)
            log.info("  ✔ 本知识点已完成（平台已登记）")
            self.failed_chapters = [c for c in self.failed_chapters if c.id != ch.id]
        elif status in ("failed", "manual"):
            self.done_this_run = [t for t in self.done_this_run if t != ch.title]
            if ch.title not in self.failed_this_run:
                self.failed_this_run.append(ch.title)
            if all(c.id != ch.id for c in self.failed_chapters):
                self.failed_chapters.append(ch)      # 进入重试队列，不会直接跳过
            label = "需人工" if status == "manual" else "未通过复核"
            log.info(f"  ✘ 本知识点{label}：{note[:80]}")
            log.info("     → 已加入重试队列，本轮末尾会再试一次")
        self._refresh_platform_progress(self._chapters_cache or [ch], quiet=False)
        return status

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

    # 占位提示关键词：内容帧只有这类文字，说明任务点还没放内容
    PLACEHOLDER_KW = ("敬请期待", "即将上线", "即将推送", "暂未开放",
                      "尚未开始", "待上传", "正在准备", "稍后开放")

    @classmethod
    def _is_placeholder_note(cls, note):
        """结果说明是否为“敬请期待/即将上线”类占位页。"""
        text = str(note or "")
        return any(k in text for k in cls.PLACEHOLDER_KW)

    def _placeholder_note(self, cards):
        """任务点若是「敬请期待」类占位页，返回提示文字；否则返回 None。

        实测：「2.1 专题讲座直播」内容帧只有一句
        「讲座直播将于9月和10月推送，敬请期待，支持回看。」——没有视频/文档/测验，
        但平台照样算 1 个任务点，程序会白播 + 白等 3 分钟复核，永远完不成。
        注意：占位页的 iframe 本身就是 modules/live，所以 live 不能当「有实质内容」的证据。
        """
        try:
            text = (cards.evaluate(
                "() => (document.body ? document.body.innerText : '')") or "").strip()
            html = (cards.evaluate(
                "() => (document.body ? document.body.innerHTML : '')") or "").lower()
        except Exception:  # noqa: BLE001
            return None
        if not text or len(text) > 200:
            return None
        # 有真正能学的东西就不算占位（live 除外，见上）
        for k in ("<video", "modules/video", "modules/document", "modules/audio",
                  "dohomework", "insertwork", "insertdoc"):
            if k in html:
                return None
        for kw in self.PLACEHOLDER_KW:
            if kw in text:
                return text[:70]
        if len(text) < 40:
            return text[:70] or "(空白页)"
        return None

    def _find_live_entry(self, page):
        """找直播/回放入口（modules/live 里的 a.liveHref）。"""
        for f in course_mod.all_frames(page):
            try:
                if "modules/live" not in (f.url or "").lower():
                    continue
                el = f.query_selector("a.liveHref")
                if el is not None and el.is_visible():
                    return f, el
            except Exception:  # noqa: BLE001
                continue
        return None

    def _handle_live(self, page, ch):
        """点进直播卡片，进入 zhibo.chaoxing.com 回放页后按视频播放。"""
        log = self.log
        entry = self._find_live_entry(page)
        if entry is None:
            return None
        f, el = entry
        try:
            title = (el.inner_text() or "").strip().replace("\n", " ")[:50]
        except Exception:  # noqa: BLE001
            title = "直播"
        log.info(f"  发现直播/回放入口：{title} → 点进去")
        completion_hint = video_mod.detect_completion_ratio(page, default=None)
        if completion_hint is not None:
            log.info(f"  直播完成线：{completion_hint:.0%}")
        else:
            log.info("  直播卡片未写明确完成线 → 由播放器页再检测（找不到则按 100%）")
        try:
            before_pages = {id(p) for p in self.session.context.pages}
        except Exception:  # noqa: BLE001
            before_pages = set()
        target, _ = self.session.click_capture(page, el, wait_s=12)
        # click_capture 有时会把新标签页当作“原页面”返回；这里按 URL/新页显式定位。
        live_page = None
        for _ in range(12):
            try:
                pages = [p for p in self.session.context.pages if not p.is_closed()]
            except Exception:  # noqa: BLE001
                pages = []
            for p in pages:
                try:
                    u = (p.url or "").lower()
                except Exception:  # noqa: BLE001
                    continue
                if "zhibo.chaoxing.com" in u:
                    live_page = p
                    break
                if id(p) not in before_pages and "passport" not in u:
                    live_page = p
            if live_page is not None:
                break
            page.wait_for_timeout(500)
        if live_page is None and "zhibo.chaoxing.com" in (target.url or "").lower():
            live_page = target
        if live_page is None:
            log.warning("  点了直播入口，但没有捕获到直播标签页 → 转人工")
            return "manual", "直播入口已点击，但未打开直播页"
        try:
            live_page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        live_page.wait_for_timeout(6000)
        self.popup.tick(live_page)
        try:
            log.info(f"  直播页已打开: {live_page.url[:100]}")
            status, note = video_mod.play_video(
                self.session, self.cfg, log, live_page, self.popup, None,
                keepalive=self._ensure_session, completion_ratio=completion_hint,
            )
            return status, f"直播回放：{note}"
        finally:
            # 只关闭本次新开的直播页，不关主学习页。
            if id(live_page) not in before_pages:
                self.session.close_page(live_page)

    def _handle_card(self, page, cards, ctype, ch):
        log, cfg = self.log, self.cfg
        if not self._should_handle(ctype):
            return "skipped", f"被 --only={self.args.only} 过滤"
        self.task_count += 1
        # 直播模块外层可能写着“敬请期待”，但内层仍有可回放入口；
        # 必须先点 liveHref，不能直接按占位页跳过。
        live_result = self._handle_live(page, ch)
        if live_result is not None:
            return live_result
        ph = self._placeholder_note(cards)
        if ph:
            log.info(f"  本章节暂无内容（{ph}）→ 跳过，不计失败")
            return "skipped", f"页面只有提示：{ph}"
        if not self._ensure_session("任务点"):
            return "manual", "任务点处理前检测到掉线且未能恢复"
        # 拟人化：处理任务点前随机滚动页面
        self.humanizer.scroll(page)
        try:
            if ctype in ("video", "audio"):
                return video_mod.play_video(
                    self.session, cfg, log, page, self.popup, None,
                    keepalive=self._ensure_session,
                )
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
            # doHomeWorkNew 之外，还有 insertwork / addStudentWork 等内嵌形态；
            # 不匹配 work/list（作业列表页，没有题目可做）
            if ("dohomework" in url or "insertwork" in url
                    or "studentwork" in url or "addstudentwork" in url):
                return f
            if wrapper is None and "modules/work" in url:
                wrapper = f
        return wrapper

    def _quiz_pages(self, page, candidate_pages=None):
        """当前页 + 本次处理期间新开的页面；默认不扫描历史答题标签页。"""
        out = [page]
        for p in candidate_pages or []:
            if p not in out:
                out.append(p)
        return out

    def _locate_quiz(self, page, candidate_pages=None):
        """在当前页和指定新页面里找答题帧，避免命中上一份测验的旧标签页。"""
        for p in self._quiz_pages(page, candidate_pages):
            qf = self._quiz_frame(p)
            if qf is not None:
                return p, qf
        return None, None

    @staticmethod
    def _is_unfinished_note(note):
        """测验提交被拦截/未作答类结果，触发“重扫剩余任务，不重复提交”。"""
        text = str(note or "")
        return any(k in text for k in ("未作答", "尚未作答", "提交被拦截", "未完成"))

    def _run_quiz(self, page, ch):
        """测验统一入口：提交被拦截时先重扫当前页其它任务，但绝不重复提交。"""
        status, note = quiz_mod.run_quiz(
            self.session, self.cfg, self.log, page, self.popup,
            self.bank, self.online, self.humanizer,
        )
        if status in ("manual", "failed") and self._is_unfinished_note(note):
            self._submit_blocked_ids.add(str(ch.id))
            self.log.info("  检测到未完成提示 → 重扫当前页，寻找其它未完成任务（本 run 不再自动重提）")
            r_status, r_note = self._rescan_current_tasks(ch, page)
            if r_status == "done":
                note = f"{note}；重扫处理：{r_note}"
        return status, note

    def _rescan_current_tasks(self, ch, page):
        """提交被拦截后重扫当前章节：只处理视频/音频/文档，不重复提交题目。"""
        log = self.log
        log.info("  重扫当前页：检查视频/音频/文档是否仍未完成；题目项保持人工，不重复提交")
        done, tried = [], []

        # ① 标签式任务点：逐个打开，跳过题目标签
        tabs = self._quiz_tabs(page)
        if tabs:
            for i in range(len(tabs)):
                page_i, _ = self._open_chapter(ch)
                tabs_i = self._quiz_tabs(page_i)
                if i >= len(tabs_i):
                    break
                tframe, tel, ttext, tkind = tabs_i[i]
                if tkind == "quiz":
                    log.info(f"   重扫跳过题目标签：{ttext}（不重复提交）")
                    continue
                if tkind == "goal":
                    continue
                log.info(f"   重扫处理：{ttext}（{tkind}）")
                try:
                    page_i, _ = self.session.click_capture(page_i, tel, wait_s=6)
                except Exception as exc:  # noqa: BLE001
                    log.warning(f"     点击标签失败: {str(exc)[:60]}")
                page_i.wait_for_timeout(3000)
                if tkind == "video":
                    s, n = self._play_video_here(page_i, ch)
                else:
                    s, n = self._browse_here(page_i, ch)
                log.info(f"     重扫结果: {s} | {str(n)[:70]}")
                tried.append(ttext)
                if s == "done":
                    done.append(ttext)
                if self._verify_chapter(ch, rounds=1) == 0:
                    return "done", "已全部完成"

        # ② 多页任务点：逐页检查，题目页跳过
        if not tried:
            pm = self._page_tabs(page)
            if pm:
                for idx in range(len(pm)):
                    p_i, cards_i = self._open_chapter(ch)
                    if idx > 0:
                        if self._goto_page(p_i, idx) is None:
                            continue
                        cards_i = course_mod.find_content_frame(p_i)
                    _, vel = video_mod.find_video_element(p_i, timeout_s=5)
                    if vel is not None:
                        s, n = self._play_video_here(p_i, ch)
                    elif cards_i is not None:
                        t_i = course_mod.card_type(cards_i)
                        if t_i == "quiz":
                            log.info(f"   重扫跳过第 {idx + 1} 页题目（不重复提交）")
                            continue
                        if t_i not in ("video", "audio", "document", "read"):
                            continue
                        s, n = self._handle_card(p_i, cards_i, t_i, ch)
                    else:
                        continue
                    log.info(f"   重扫第 {idx + 1}/{len(pm)} 页结果: {s} | {str(n)[:70]}")
                    tried.append(f"第{idx + 1}页")
                    if s == "done":
                        done.append(f"第{idx + 1}页")
                    if self._verify_chapter(ch, rounds=1) == 0:
                        return "done", "已全部完成"

        # ③ 单内容帧：题目不碰，其它内容处理一次
        if not tried:
            cards = course_mod.find_content_frame(page)
            if cards is not None:
                ctype = course_mod.card_type(cards)
                if ctype == "quiz":
                    log.info("   当前内容帧是题目 → 不重复提交，保持人工")
                elif ctype in ("video", "audio", "document", "read"):
                    s, n = self._handle_card(page, cards, ctype, ch)
                    log.info(f"   重扫结果: {s} | {str(n)[:70]}")
                    tried.append(TYPE_LABEL.get(ctype, ctype))
                    if s == "done":
                        done.append(TYPE_LABEL.get(ctype, ctype))

        if done:
            return "done", "、".join(done)
        return "manual", "重扫完成：未发现可自动处理的非题目任务；题目项保持人工"

    def _handle_quiz(self, page, ch):
        if not self._should_handle("quiz"):
            return "skipped", f"被 --only={self.args.only} 过滤"
        # 进入本函数前打开的标签页都视为历史页；只允许当前目标页和随后新开的页面。
        try:
            initial_pages = list(self.session.context.pages)
        except Exception:  # noqa: BLE001
            initial_pages = [page]
        # 1) 答题页往往已自动在某个 frame/标签页里打开（work/doHomeWorkNew）
        for attempt in range(4):
            try:
                new_pages = [p for p in self.session.context.pages if p not in initial_pages]
            except Exception:  # noqa: BLE001
                new_pages = []
            host, qf = self._locate_quiz(page, new_pages)
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
                    return self._run_quiz(
                        qf, ch,
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
                        return self._run_quiz(
                            target, ch,
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

    # 小圆状态复核：先看章节树的小圆，再用接口兜底；避免固定干等导致“卡住”。
    STATE_WAIT_S = 180
    STATE_POLL_S = 3.0

    def _verify_chapter(self, ch, rounds=5):
        """复核小圆/平台状态：0=绿色已完成；-1=橙色 0（直接跳过）；>0=仍橙色；None=无法判定。

        rounds=1 用于多页/多标签的快速检查；外层完整复核默认最多等 180 秒。
        状态出现即返回，不再按固定 15/25/60/90 秒干等。
        """
        wait_s = {1: 20}.get(int(rounds), self.STATE_WAIT_S)
        deadline = time.time() + max(1.0, wait_s)
        last_hint = None
        page = self._tree_page()
        next_api = 0.0
        while True:
            self.session.keep_alive()
            # 1) 用户要求的权威信号：任务点前的小圆
            try:
                dom = course_mod.chapter_state(page, ch.id)
            except Exception:  # noqa: BLE001
                dom = None
            if dom is not None:
                course_mod.update_chapter_hint(ch.id, dom)
                if dom == 0:
                    return 0
                if dom == -1:
                    return -1
                last_hint = max(int(dom), 0)
            # 2) 平台接口兜底（不覆盖小圆已给出的绿色/橙色 0）
            if time.time() >= next_api:
                try:
                    st = course_mod.chapter_status(self.session, self.course, [ch.id], self.log)
                except Exception:  # noqa: BLE001
                    st = {}
                v = (st or {}).get(ch.id)
                if v:
                    total = int(v.get("total", 0) or 0)
                    unfinish = int(v.get("unfinish", 0) or 0)
                    if total == 0 and unfinish == 0:
                        course_mod.update_chapter_hint(ch.id, -1)
                        return -1
                    if unfinish == 0:
                        course_mod.update_chapter_hint(ch.id, 0)
                        return 0
                    last_hint = max(unfinish, 0)
                next_api = time.time() + 12.0
            if time.time() >= deadline:
                return last_hint
            time.sleep(min(self.STATE_POLL_S, max(0.2, deadline - time.time())))

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
        last_card_key = ""
        same_card_count = 0
        for rnd in range(1, max_rounds + 1):
            self.popup.tick(target)
            self.session.keep_alive()
            card = self._first_actionable_card(target)
            if card is None:
                log.info("任务中心：未发现更多可继续的任务点")
                break
            title = self._card_title(card)
            ptype = course_mod.guess_type(title)
            card_key = f"{ptype}:{title}"
            if card_key == last_card_key:
                same_card_count += 1
                if same_card_count > 1:
                    log.warning("  同一任务复查后仍未从可执行列表移除 → 停止任务中心，避免死循环")
                    break
                log.info("  平台尚未移除上一任务，等待 5 秒后复查一次")
                target.wait_for_timeout(5000)
            else:
                same_card_count = 0
            last_card_key = card_key
            log.info(f"  [第{rnd}轮] 任务点: {title[:50]} (推断类型 {ptype})")
            task_page, is_new = self.session.click_capture(target, card, wait_s=12)
            task_page.wait_for_timeout(3000)
            self.popup.tick(task_page)
            try:
                if not self._should_handle(ptype):
                    status, note = "skipped", f"被 --only={self.args.only} 过滤"
                elif ptype == "video":
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
            if status in ("failed", "manual", "skipped"):
                log.warning("  当前任务未完成/未处理 → 停止任务中心，不重复点击同一卡片")
                break
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
