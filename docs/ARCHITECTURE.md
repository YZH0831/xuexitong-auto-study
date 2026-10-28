# 架构说明

本文说明"为什么这么写"，比 README 更深一层。

## 1. 分层

```
main.py                CLI 参数 → 加载配置 → 初始化日志 → Runner
  └─ cxlab.runner      主循环：登录 → 课程 → 章节 → 任务点调度
       ├─ login       扫码/密码登录、会话守护、掉线自愈
       ├─ course      课程列表、状态判定、课时选择
       ├─ video       视频/音频播放（真实 1x）
       ├─ document    课件浏览
       ├─ quiz        章节测验：解析 → 作答 → 提交 → 回读
       │    ├─ font_decoder    加密字体还原
       │    ├─ answers         本地题库（分级可信）
       │    ├─ tiku            第三方题库接口
       │    ├─ llm_answer      大模型答题（带熔断）
       │    ├─ online_answer   双源搜题 + 答案解析 + 选项对齐
       │    └─ reasoning       本地推理与兜底
       └─ report      完成报告（MD + JSON）
```

设计原则：**判定一律以平台接口为准，页面标记只作兜底**。
学习通的页面状态会滞后（视频播完后页面可能几秒到几分钟才变绿），
所以 `courselib` 层统一走 `POST mooc1-api.chaoxing.com/job/myjobsnodesmap` 返回的
`total / finish / unfinish` 三计数，再配合延迟复核（0s / 15s / 25s）。

## 2. 主循环与幂等

`Runner` 对每个知识点顺序执行：

1. 读该节点的接口状态 → `unfinish == 0 && total > 0` 已完成跳过；`total == 0` 无任务点跳过；
2. 打开章节（新版 `mooc2-ans` 直接 click + 0.5s 轮询解锁，旧版走跳转）；
3. 枚举页面上的任务点标签（`#prev_tab li`），按文本分类为 `video / quiz / doc / goal`；
4. 逐个执行，每个任务点执行后**重新读接口**复核；
5. 失败进重试队列（`safety.retry_rounds`），本轮末尾再跑一次。

幂等性的关键：所有"是否要做"的判断都来自接口，而不是本地记录。所以中断后重跑会自然续上。

## 3. 视频/音频：为什么不能倍速

平台按**真实时间**核验观看。实测：

| 试验 | 结果 |
|---|---|
| 1x 真实播完 648s 视频 | 平台 `finish=1` ✔ |
| 2x 播完 717s 视频（三次复核） | 平台仍 `unfinish=1` ✘ |
| 注入 Hook 篡改 `Date.now / performance.now / 定时器` | 媒体时钟不受 JS 影响，进度仍是 1.00 倍真实值 ✘ |
| 同一页面注入两个播放器并行 | 平台强制单播，互相抢占 ✘ |

结论：`video.speed` 默认 1.0；若填了 >1 且平台未登记，会自动用 1x 重播一次。

音频（`modules/audio`）跟视频共用 video.js，因此复用同一套播放与进度逻辑；
元素识别时用 `src` 非空且 `duration > 0` 来区分真实媒体与 cards 页占位元素
（`knowledge/cards` 路径下也可能有真实音频，不能一律跳过）。

## 4. 题面加密字体

学习通对部分课程使用 `cxSecretStyle` 混淆：把汉字替换成随机码位并加载自定义字体，
页面上"看得到"但复制出来是乱码/错字。做法：

1. 在页面里取出自定义字体的 base64 / URL；
2. 用 `fonttools` 解析字形轮廓，按归一化后的轮廓哈希与
   `data/font_map_table.json`（30884 条，来自开源项目）比对；
3. 得到"码位 → 真字"映射，回填题面文本。

解码失败时降级：`quiz.on_unknown=pause` 让人工作答，避免用乱码去搜题。

## 5. 答题链路与可信度

```
本地题库(5/4) → 第三方题库(4) → 大模型(2) → 双源搜题(3/1/0) → 兜底推理
```

括号里是 `answers.RANK` 的可信等级，只允许高等级覆盖低等级，
`TRUSTED` 中的来源被视为"已验证"（不会被后续弱证据推翻）。

**选项顺序对齐**是整条链路的地基：任何来源给出的若是"文本答案"，都先经
`online_answer.match_option_by_text()` 对齐到本页选项；对齐不上就丢弃，
**绝不直接采信外部字母**（不同网站选项顺序不同，盲信字母是历史上选择题大量答错的根因）。

**双源核对**：通用搜索（bing/baidu/sogou 自动降级）与作业帮定向检索各自解析出答案，
一致 → `cross_checked`；冲突 → 按"是否带明确答案标记 → 题干命中位置 → 答案类型"仲裁；
单源 → 标记未验证。

**禁止留空**：所有来源都失败时，`reasoning.best_guess()` 依题型惯例给出判断并标注置信度。
理由很实际——留空不只是 0 分，平台会以"还有 X 道题未作答"**拦截整份提交**。

## 6. 风控与安全边界

- 拟人化：贝塞尔鼠标轨迹、点击前后随机延时、随机滚动、每 N 个任务一次长暂停；
- 频率：搜索最小间隔、单次运行搜索上限、掉线重登每小时上限、断网指数退避；
- 不做：伪造平台接口请求、篡改媒体时钟、并发多播放器、绕过验证码；
- 数据：Key 只落本地 `config.yaml`（已 gitignore），日志脱敏，提交前密钥扫描。

## 7. 测试策略

`tests/` 全部是**离线**测试：假 Page/Frame/Element，不联网、不碰学习通账号。

| 测试 | 覆盖 |
|---|---|
| `test_core` | 配置加载、URL/课时解析等基础 |
| `test_new_features` | 选课、课时、报告等新特性 |
| `test_quiz_and_retry` | 作答填充、提交与重试 |
| `test_cross_check` | 双源核对与冲突仲裁 |
| `test_reasoning` | 本地推理打分与结论抽取 |
| `test_judge_multi` | 判断题必答、多选补全 |
| `test_llm_answer` | 大模型 JSON 解析、类型校验、熔断 |
| `test_never_blank` | 极端场景（无题库/无网/无 Key）10 题全答、不留空 |
| `test_tiku` | 四类题库 provider、文本对齐、熔断、回退 |

跑法：

```bat
for %f in (tests\test_*.py) do .venv\Scripts\python.exe %f
```

CI（`.github/workflows/ci.yml`）在 Ubuntu 上装依赖后逐个跑，并额外跑一次密钥扫描。
