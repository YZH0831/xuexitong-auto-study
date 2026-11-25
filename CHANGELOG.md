# 更新日志（CHANGELOG）

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [1.1.9] - 2026-11-25

修复「视频任务点页面已显示完成，程序却还要重新看一遍」。

### 根因

项目里有**两套**任务点完成判定，口径不一致：

| 路径 | 是否看 DOM 的 `ans-job-finished` |
|---|---|
| `unfinished_task_cards`（旧路径） | ✅ 检查（`"ans-job-finished" in cls` 就跳过） |
| `card_jobs`（jobid 路径，**优先走这条**） | ❌ 只看平台接口 `isPassed` |

平台接口的 `isPassed` 经常**滞后于页面显示**：页面 `.ans-attach-ct` 已经带上
`ans-job-finished`（图标显示已完成），接口却仍返回 false。于是 `card_jobs`
把已完成视频判成未完成 → 重播整段 → 播完 `isPassed` 仍是 false → 判 failed
→ 进重试队列**再次打开同一章节**（日志里 10.2 被处理两次正是这个链路）。

### 修复

- `cxlab/course.py card_jobs`：先取到 task card，再把 DOM 的
  `ans-job-finished` 纳入 `passed` 判定——
  `passed = isPassed or dom_done or (video/audio 且无 job)`。
  两条路径口径统一，页面显示已完成就不会再重播。
- 新增 `tests/test_passed_dom_marker.py`（3 项：DOM 标记检查、两路径口径一致、
  passed 行为模拟），确保不会再退回只看 `isPassed`。

### 测试

- 全部既有测试通过：quiz/retry、core、rescan_quiz_fix、
  double_instance_and_quiz_tab、no_unguarded_evaluate、card_jobs_timeout；
  `py_compile` OK。

## [1.1.8] - 2026-11-25

**精确定位并修复卡死 10.2 的真正根因**（v1.1.7 的心跳日志立功）。

### 诊断

v1.1.7 的心跳日志显示：第一次打开 10.2 全部步骤走完；随后**同一章节被第二次
打开**，走到「内容帧已就绪」后停住，**没有出现「读取 mArg.attachments」**。
据此锁定卡点在 `_open_chapter` 返回后、`_process_attachment_cards` 内部心跳之前——
即 `process_chapter` 里那句 `if cards is not None and course_mod.card_jobs(cards)`。

`card_jobs()` 内部 `cards_frame.evaluate("() => window.mArg.attachments")`
**没有超时**（evaluate 不受 default_timeout 约束）。第二次打开时该 frame 的
evaluate 永久挂起。

### 修复

- 新增 `cxlab/course.py` 的 `call_with_timeout` / `eval_with_timeout`：
  后台 daemon 线程执行无超时的 Playwright 调用，主线程 `join(timeout)` 限时；
  超时返回 default，让主流程继续，卡住的线程不阻止进程退出。
- `card_jobs` 的 `evaluate` 与兜底 `content()` 都改为带 8 秒超时；
  超时走 HTML 解析兜底，不再永久挂起。
- 在 `card_jobs` 条件判断前后补心跳日志，下一轮能直接看到这一步是否通过。

### 测试

- 新增 `tests/test_card_jobs_timeout.py`（5 项：helper 存在、card_jobs 用守卫、
  慢调用/卡住 evaluate 能超时返回、正常调用返回值）。
- 全部既有测试通过（quiz/retry、core、rescan_quiz_fix、
  double_instance_and_quiz_tab、no_unguarded_evaluate），`py_compile` OK。

## [1.1.7] - 2026-11-25

继续追查「开始处理 10.2 后卡死」——v1.1.6 修的两处不是全部卡点。

### 修复

- **关键发现：上次加的心跳日志用的是 `log.debug()`，而配置是 INFO 级，
  根本没写进日志**——所以"看不见心跳"不等于没执行到。本次把心跳全部
  提到 `log.info()`，并在 `_open_chapter` 的每一步（定位章节树 → 点击 →
  开放检查 → 内容帧 → 读 attachments → 探测完成度）都埋点。下一轮若再卡，
  日志最后一行会精确指出是哪个调用挂起，不再盲猜。
- v1.1.6 漏掉的两处**无超时 evaluate**，正好都在 `_open_chapter` 循环里
  被反复调用（每章 8 次以上），是更可能的真凶：
  1. `_chapter_locked`：遍历所有 frame 做 `f.evaluate(读 innerText)` →
     改为跳过非 http 帧 + `locator("body").inner_text(timeout=1500)`。
  2. `PopupGuard._close_known`：`page.evaluate(JS_DIALOGS)` →
     改为 `locator(选择器) + inner_text(timeout=1500)`，两者都受超时约束。
- 新增 `tests/test_no_unguarded_evaluate.py`（5 项）：守住这三条路径
  不再回退到裸 evaluate，并确认心跳是 INFO 级。

### 说明

- 全部既有测试通过（quiz/retry、core、rescan_quiz_fix、
  double_instance_and_quiz_tab），`py_compile` OK。
- 仍是"有证据指向"的修复；但这次心跳可见了，若再卡能一眼定位，
  请把日志里最后一个 `[心跳]` 行发我。

## [1.1.6] - 2026-11-25

修复「处理到某个视频章节就永久卡住、既不点测验标签也不点下一节」。

### 修复

- **根因：Playwright `frame.evaluate()` 没有内建超时**，而两处代码对页面里
  **所有 iframe** 都做 evaluate。知识点页 iframe 很多（视频/测验/文档），
  一旦某个视频 frame 的 JS 主线程被解码或网络占满，evaluate 就永久阻塞——
  实测日志停在「开始处理：10.2」超过 4 分钟、进程 CPU 几乎为 0（在等 I/O），
  表现就是用户看到的「卡在视频界面不动」。
  1. `cxlab/course.py chapter_element`：JS 标记只在「确实含章节树元素」的
     frame 上执行，跳过视频/测验 frame；`query_selector` 遍历全部 frame 保留不变。
  2. `cxlab/video.py detect_completion_ratio`：改为跳过 `data:`/`about:blank`
     等非 http frame，并用 `locator("body").inner_text(timeout=3000)` 代替
     `frame.evaluate(JS_GET_TEXT)`——前者受 Playwright 超时约束，最多等 3 秒。
- **诊断增强**：`_process_attachment_cards` 在读取 attachments / 探测完成度
  前后各加一条 debug 心跳日志。万一以后仍卡，可立刻从日志判断卡在哪一步，
  不必再猜。

### 说明

- 本次是「有证据指向 + 防御性」修复：卡死位置（开始处理之后、发现小任务点
  之前）与这两处全-frame evaluate 完全吻合，但不能 100% 复现证明。
  v1.1.5 的重扫点测验标签、1x 重播守卫等修复仍保留。
- 已加 `tests/test_double_instance_and_quiz_tab.py`（v1.1.5，12 项）覆盖此前的
  单实例锁和测验标签问题；本版改动通过既有全部测试与 `py_compile`。

## [1.1.5] - 2026-11-25

修复用户实测反馈的两个问题：连点 bat 仍会开两个控制台、重扫不会点「2章节测验」标题做题。

### 修复

- **单实例锁此前形同虚设**（`tools/web_ui.py acquire_instance_mutex`）：
  原来用 `ctypes.windll.kernel32` + `k.GetLastError()` 判断 `ERROR_ALREADY_EXISTS`，
  但 `ctypes.windll` 默认**不保留 Win32 last-error**，`GetLastError()` 恒为 0，
  于是第二个实例永远被当成第一个放行——实测同时存在 2 个 `web_ui.py` 进程
  （一个监听 8765，另一个空转）。现改用
  `ctypes.WinDLL("kernel32", use_last_error=True)` + `ctypes.get_last_error()`，
  并声明 `CreateMutexW` 的 argtypes/restype（64 位句柄不被截断）。
  Win32 实测：首个实例 err=0 → 启动；第二个 err=183 → 切到已有窗口。
- **重扫跳过「2章节测验」标签**（`cxlab/runner.py _rescan_current_tasks`）：
  视频做完后若小圆仍橙，旧版重扫**无条件跳过** quiz 标签，日志反复出现
  「重扫跳过题目标签：2章节测验」，然后白等几分钟再靠「下一节」兜底才做题。
  现给重扫加 `allow_quiz` 参数：内容处理后重扫传 `True`，直接点开题目标签做测验；
  提交被拦截后的重扫保持 `False`（不重复交白卷），并用 `_submit_blocked_ids`
  双重防重复。标签式 / 多页 / 单内容帧三条路径都覆盖。

  **注意**：需要重启控制台才会加载新代码——请关掉现有的学习通控制台窗口
  （或结束残留的 `pythonw.exe tools\web_ui.py` 进程）后重新双击桌面 bat。

### 测试

- 新增 `tests/test_double_instance_and_quiz_tab.py`（12 项：mutex use_last_error
  源码守卫 + 真实 Win32 首个/第二个拿锁行为 + 重扫决策行为模拟）。
  既有 `test_quiz_and_retry.py` / `test_core.py` / `test_rescan_quiz_fix.py` 全绿。

## [1.1.4] - 2026-11-25

修复「视频反复刷、章节检测测验迟迟不做」的回归 bug，并完善控制台单实例体验。

### 修复

- **视频 job 处理后补做测验**（`cxlab/runner.py process_chapter`）：章节同时含
  「视频附件 + 章节测验」时，`card_jobs()` 只识别 video/audio/document，测验不在
  jobid 清单里。视频播完小圆仍橙（其实是测验没做），旧逻辑误判为「倍速未被平台
  登记」→ 1x 重播整段视频；而复核重扫又明确跳过题目 → 陷入「一直刷视频、
  永远不做题」。现在 job 处理完、小圆仍橙且有答题入口时主动补做一次测验
  （`_try_open_quiz` → `_handle_quiz`），并把该章节记入
  `_submit_blocked_ids` 语义下的防重复提交保护——补做过的章节不再触发
  无谓的 1x 视频重播。纯视频章节的 1x 重播兜底保持不变。
- 答题入口藏在「下一节」之后的章节，仍由既有的 `_continue_next_block` 兜底覆盖，
  逻辑闭环完整。

### 新增

- **控制台单实例锁**（`tools/web_ui.py`，此前未提交的工作）：进程级互斥锁 +
  窗口枚举聚焦，连点两次 bat 时后启动的实例直接切到已有窗口，不再开第二个
  控制台；新增 `wait_running_port` / `wait_for_window` 等待先例就绪，避免竞态误判。

### 测试

- 新增 `tests/test_rescan_quiz_fix.py`（9 项：源码守卫 + 补做测验/重播守卫行为模拟）；
  `tests/test_quiz_and_retry.py`、`tests/test_core.py` 全绿；改动文件通过 `py_compile`。

## [1.1.3] - 2026-11-23

安全审计专项修复 + 题库防污染；一并提交此前的「大模型二次复核」功能。

### 安全

- **清除配置文件中的明文 DeepSeek API Key**（config.yaml / config.yaml.bak）：
  该 Key 已在排查对话中多次出现，存在泄露风险。**请到 DeepSeek 控制台作废旧 Key**，
  换新 Key 后用 `SetDeepSeekKey.bat` 重新填写，或设环境变量 `DEEPSEEK_API_KEY`。
- `data/answers.json`（手工题库，含真实题目）移出 Git 追踪并入 `.gitignore`，
  防止随开源发布泄露课程题库。

### 修复

- **结果页回读防污染**（`cxlab/quiz.py read_result`）：quiz_result 是最高可信度
  （RANK=5），一旦写入，任何低等级证据都覆盖不了（answers.py 只允许高覆盖低）。
  旧实现的选择器含 `.answer`——「我的答案」布局类同样命中，用户自己填错的文本会以
  最高可信度入库并永久锁死。现在回读值必须逐条通过 `_verify_feedback_value`
  校验（确实是本题的选项 / 选项字母 / 判断词对得上）才沉淀；验不过一律不入库；
  无选项的填空题无法验证归属 → 不沉淀（宁缺勿污染）。
- **判断题误判**：题干含「判断」字样的 3+ 选项单选题（如"下列判断正确的是"）
  不再被误判为判断题（要求选项 ≤2；真判断题仍由 `_is_judge` 兜底识别）。
- **WebUI「停止」与实际行为对齐**：`terminate()` 在 Windows 上是硬杀，且杀不到
  Playwright 拉起的浏览器子进程。改用 `taskkill /T` 连进程树清理，防浏览器
  野进程残留；提示文案不再谎称"已发送停止指令"。
- `on_unknown` 增加 `submit_blank` 显式分支，防未来新增枚值时静默落空。

### 新增（此前未提交的工作）

- **大模型二次复核**（`cxlab/llm_answer.py`）：置信低于阈值 / 解析可疑的答案
  再唤起一次 DeepSeek 独立重推；两次一致提置信采纳，冲突降置信交联网双源仲裁。
- `min_confidence` 默认 0.55 → 0.62（滤掉虚高的低质量答案）。
- 新增 `tests/test_accuracy_20261120.py` 精度回归测试。

### 测试

- 全量 16 个测试文件通过；本次新增的防污染校验通过独立行为验证。

## [1.1.2] - 2026-11-17

修复「多终端登录提示」被误判为被顶号的问题。

### 修复

- **顶号判定改为以会话探测为准**：平台弹出的「多终端登录」只是一条"有设备登入"的通知，
  并不代表本机会话已失效——本程序自身重新登录（同为谷歌内核浏览器）也会触发它。
  旧逻辑一见提示就等 60 秒重登，既浪费时间，又因二次登录反过来制造真正的顶号。
  现在收到提示立即 `probe_session()` 核验并给出明确结论：`ok`=未被顶号（继续任务、不重登不空等）、
  `logged_out`=确实被顶号（自动重新登录）、`offline`=网络问题（走断网退避）。
- 视频播放循环同步按该结论决定是否中断：未被顶号时继续播放，确被顶号才结束本知识点稍后重试。
- 弹窗解析出提示里的时间与设备写入日志（如 `22:00 于 谷歌内核浏览器设备`），便于判断登录来源。
- `tools/keepalive.py` 守护的冷却判据由模糊的「多终端登录」改为明确的「结论=确实被顶号」；
  否则平台弹窗原文就含"多终端登录"四字，虚惊一场也会让守护误判被踢、白白冷却 30 分钟。

### 测试

- 新增 `tests/test_mt_verdict.py`：覆盖"提示但会话正常 / 提示且会话失效 / 提示但网络不通 /
  弹窗解析 / 守护虚惊不冷却与真顶号冷却"等情形，不启动真实浏览器，可与运行中的实例并存执行。

## [1.1.1] - 2026-11-14

修复课程/课时执行顺序被强制按字母（平台列表）顺序重排的问题。

### 修复

- `--select` / `course.selected_courses` / Web 控制台勾选：新增 `match_courses_in_order()`，
  按关键词的书写顺序挑课；不再用「遍历平台课程列表 + `any()` 匹配」把顺序固定成 A→B→C。
- 交互 `--pick`（菜单 [5]）：新增 `parse_index_spec_ordered()`，输入 `C A B` 就按 C→A→B 执行，
  去掉 `sorted(idx)` 的字母升序重排；区间按书写方向展开（`C-A` → C、B、A），重复项自动去重。
- 课时范围 `--lessons` / 逐门指定：同样按表达式书写顺序处理，不再按章节原始顺序回放。
- Web 控制台「选择课程」：显示每门课的执行序号并提供 ↑ / ↓ 调整；勾选结果从 `Set` 改为数组保存，
  点「开始学习」时按该顺序传给 `--select`。
- Web 控制台「选择课程」**默认全不选**（原来默认勾选所有可处理课程），避免误启动全部课程；
  需要时用「全选」/「只选可处理」一键勾选，或逐门按想要的顺序点选。

### 兼容

- `parse_index_spec()` 仍返回 `set`，供只关心「包含哪些」的调用方与既有测试使用；
  需要顺序的路径改调 `parse_index_spec_ordered()`。

## [1.1.0] - 2026-11-14

本地 Web 控制台、无人值守守护，以及章节推进策略调整。

### 新增（功能）

- **本地 Web 控制台** `tools/web_ui.py` / `tools/web_ui.html` + `webui.bat`：零第三方依赖，
  只监听 127.0.0.1、命令白名单；浏览器里看状态/日志/配置/课程/报告，可启动、停止任务、
  跑环境自检、向运行中的程序送入交互输入；`webui.bat` 用 `pythonw` 无黑框启动，已有窗口会被唤到前台。
- **无人值守守护** `tools/keepalive.py` + `keepalive.bat`：主程序退出后按退避策略自动续跑，
  运行期间阻止系统空闲睡眠；`keepalive.bat stop` 发停止信号，日志在 `logs/keepalive.log`。

### 变更

- **灰色「未开放」章节直接跳过**：不点进去、不触发门控、不中断整轮，下一轮重新探测是否已开放；
  橙色 0 且没有「下一节」入口的章节同样按「往下跳」处理并记 `skipped`。
- **章节复核不再阻塞整轮**：处理完复核小圆，未变绿的记为「需人工」，本轮末尾统一重试，
  报告里集中列出待人工条目。
- `safety.local_repair.enabled` 默认改为 `false`（`timeout_s` 180 → 30）：无界面 CLI 拿不到
  正在运行的浏览器会话，实测无法修复只会白等并在 `output/local_repair/` 堆积请求文件；
  确有需要可显式开启，或用 `command` 接自己的修复脚本。

### 修复

- `runner` / `course`：按 `jobid` 逐个处理大任务点下的小任务点、优先处理子任务、多端告警时暂停播放。
- `browser` / `video` / `config` / `local_repair`：配合上述策略的适配与默认项调整。
- `tests/test_new_features.py`、`tests/test_quiz_and_retry.py`：补齐上述场景的离线用例。

## [1.0.0] - 2026-10-28

首个可分发版本。相对早期内部脚本（v0.x），本版把"能跑"提升到"可交付"。

### 新增（功能）

- **第三方题库接口** `cxlab/tiku.py`：支持 `icodef`（GO题）、`enncy`（言溪）、`tikuadapter`（自建聚合）
  与 `custom` 四类 provider；按配置顺序回退、命中即止、单 provider 连续失败 3 次自动熔断；
  题库文本答案自动对齐到本页选项（多选支持 `A#B`、`A、B` 片段拆分）；命中答案计入可信等级 `tiku`。
- **兜底作答，禁止留空** `reasoning.best_guess()`：搜不到答案时按命题惯例给出带置信度的判断
  （单选选优 / 多选取均值以上 / 判断题走绝对化与规范词先验），并标注 `兜底推理(置信x.xx)`。
  根治"空题导致平台拦截提交"的问题。
- **平台拦截提示解析** `quiz._unanswered_hint()`：读取"还有 X 道题未作答"并写日志，不再盲目重试提交。
- **大模型熔断**：遇 401/402/403 直接熔断，本轮不再调用，避免每题都白撞一次。
- **密钥图形界面** `tools/set_key_gui.py`：粘贴即用、预设模型（DeepSeek/Kimi/Ollama）、
  连通性测试、**账户余额查询**（免费接口）、命令行 `--set-key/--balance/--show/--status-line/--clear`。
- **环境自检** `tools/doctor.py`：Python 版本、依赖、浏览器、配置、目录权限、字形表、
  网络可达性、Playwright 实际启动，一次跑完给出可执行的修复建议。
- **密钥/隐私防护**：`tools/check_secrets.py` 扫描 API Key / Cookie / 手机号 / 密码字段，
  配合 `.githooks/pre-commit` 在提交前拦截；日志自动脱敏（Key、手机号、password）。
- **一键安装与菜单** `setup.bat` / `start.bat`：自动建 venv、装依赖（清华镜像失败回退官方源）、
  生成 config.yaml、自检；菜单显示实时密钥状态，支持 `start.bat run|quiz|video|pick|doctor|key` 直达。
- **题库图形配置** `tools/tiku_gui.py` + `SetTiku.bat` + 菜单 `[T]`：勾选 provider、填 token/地址、
  一键测试连通性，保存时整块替换 `quiz.tiku`（保留其它配置与注释、幂等不重复插入）。
- **跨平台** `setup.sh` / `start.sh`：macOS / Linux / WSL 也能装和跑（Windows 仍用 `.bat`）。
- **打包** `tools/package.py`：白名单收集文件（私人数据不进包）、打包前强制密钥扫描，
  产物 `dist/chaoxing-auto-study-v<版本>.zip`。
- `--version`、`VERSION`、`cxlab/version.py`：统一版本来源。
- CI：`.github/workflows/ci.yml` 在 Ubuntu 上跑全部离线测试 + 密钥扫描。

### 修复

- **分发包缺 `cxlab/__init__.py`**：`tools/package.py` 里"跳过 `_` 开头文件（临时文件）"的规则
  把 `__init__.py` 一并排除了，解压后 `cxlab` 退化为命名空间包（`__version__` 读不到、
  部分环境导入行为异常）。规则收窄为"只跳过单下划线开头"，并在打包后自检必需文件，
  CI 新增「解压即用」验证（import + `--version` + 敏感文件不得入包）。
- `tools/doctor.py` 结尾提示"双击 `ChaoxingAutoStudy.bat`"，但该桌面入口不在分发包内，
  新用户会找不到；改为按平台与 `config.yaml` 是否存在提示 `setup.bat` / `start.bat`
  （Unix 为 `./setup.sh` / `./start.sh`）。
- **音频导学不播放**：任务点标签识别只认 `视频|微课|测验|作业|考试|学习目标|文档`，
  「音频导学」整条被过滤掉，永远不点播放；已把 `音频|听力|听书` 归入播放器类。
  同时放宽播放器元素识别：`knowledge/cards` 路径下只有"时长为 0"才算占位元素，
  避免把真实音频一起跳过。（实测 502 秒音频完整播完并登记成功）
- **章节测验留空提交 → 平台拦截**：改为兜底作答，保证每题都有答案。
- **大模型 402 后每题重试**：加熔断，首次失败即停。
- **置信度不足直接弃答**：仅对"大模型 + 无其它来源"生效，不再让整题空着。
- 修正兜底答案被统计进"联网搜索"的计数错误，新增"第三方题库 / 兜底推理"独立计数。

### 变更

- 默认模型由 `deepseek-chat` 改为 **`deepseek-flash`**（DeepSeek 已下线旧模型名），
  `reasoning_effort` 默认 `low`；界面预设同步更新。
- 兜底打分加入负面词惩罚（唯利是图/损人利己等），提升多选题兜底准确率。
- 桌面入口 `ChaoxingAutoStudy.bat` 改为薄封装，统一调用仓库内 `start.bat`，避免两处逻辑分叉。
- 开发日志（含实测数据）作为本地私人笔记保存，不随仓库分发；README 重写为面向使用者的文档。

### 已知问题

- 「知识图谱」类任务点：文档路径翻页后平台仍可能判未完成，需人工点一下或等平台时序修正。
- 平台按真实时间核验，视频/音频只能 1x 实播，因此整体耗时无法压缩。
- 尚未覆盖考试与作业自动作答。

## [0.x] - 2026-10 之前

内部迭代，无正式版本号。主要里程碑：

- 浏览器驱动骨架、登录与会话保持、章节遍历与任务点调度
- 视频真实 1x 播放与接口复核、课件翻页浏览
- 加密字体解码（移植 30884 条字形表）
- 章节测验作答：选项文本对齐、双源联网搜题（通用 + 作业帮）、冲突仲裁、结果页回读沉淀题库
- 多选补全、判断题必答、拟人化操作与风控频率限制、掉线自愈
- 课程状态判定（已结束/已退课/截止日期 override）、课时选择与交互选课
