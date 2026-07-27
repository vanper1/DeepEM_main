# 上下文管理生命周期调研讲稿设计

## 1. 目标

为 `docs/context_management_lifecycle_research_20260727.md` 新建一份 20-25 分钟组会讲稿。讲稿采用上下文生命周期主线，不再按 Pi Agent、OpenCode、Codex CLI、Claude Code 分别讲解，也不再以上下文压缩作为主要叙事。

原讲稿 `docs/context_management_final_research_speaker_notes_20260727.md` 保持不变。新讲稿输出为：

`docs/context_management_lifecycle_research_speaker_notes_20260727.md`

## 2. 听众与表达方式

听众了解 DeepEM 业务，但不了解上下文管理内部实现。讲稿应先用业务问题解释机制，再引入必要的对象和术语；避免逐行讲源码、连续罗列数据结构或把报告正文直接缩写成提纲。

正文使用可以直接朗读的口语化文本，并保留：

- 每段明确时间范围；
- 对应报告章节的展示提示；
- 生命周期环节之间的自然转场；
- 会后预期问题与简洁回答。

## 3. 核心叙事

讲稿围绕一个中心判断展开：上下文管理负责决定“本轮应该知道什么、为什么可以知道、如何持续和纠正”，压缩只负责窗口压力下的降载。

主线为：

```text
选择与路由
  -> 按需加载与预算组装
  -> 隔离与权限边界
  -> 执行状态与事件
  -> 压缩与细节回捞
  -> 会话恢复与记忆治理
  -> 可观测、用户控制与评测
```

四个外部项目嵌入对应环节：

- Pi Agent：合法压缩单元和结构化 handoff；
- OpenCode：session event/projection；
- Codex CLI：ContextManager、world state、checkpoint 和 resume/fork；
- Claude Code：progressive disclosure、分层记忆、subagent 和用户控制。

## 4. 时间结构

| 时间 | 内容 | 核心结论 |
|---|---|---|
| 0-2 分钟 | 问题定义 | 上下文管理不等于压缩 |
| 2-5 分钟 | 统一生命周期模型 | 模型输入是一次计算视图，不是事实库 |
| 5-8 分钟 | DeepEM 当前全景 | 已有路由、压缩、恢复和 trace 基础，但缺统一策略与治理 |
| 8-11 分钟 | 选择与按需组装 | 是否注入优先于如何压缩 |
| 11-13.5 分钟 | 隔离 | 权限必须在数据进入模型前执行 |
| 13.5-16.5 分钟 | 状态生命周期与恢复 | worker/后端状态是事实源，checkpoint 支撑恢复 |
| 16.5-19 分钟 | 记忆治理 | 原始记录、工作记忆和长期事实必须分层 |
| 19-21 分钟 | 压缩 | 压缩是管理体系中的一个降载环节 |
| 21-23 分钟 | 可观测与用户控制 | 策略需要可解释、可纠正和可评测 |
| 23-25 分钟 | 路线与总结 | P0 先统一策略、隔离和持久化恢复 |

## 5. DeepEM 事实边界

讲稿必须准确说明：

- general/workspace 路由已实现；
- general 的 estimated input tokens P50 从 9374 降至 116，下降 98.76%；
- 工具结果 compact envelope 和 `tool_result_id + path` 回捞已实现；
- `.env` 已开启 semantic compression 和 Reducer V2；
- 65%/80%/90% 是当前 Reducer V2 分级；
- 同进程页面切换恢复一期能力已实现，包括 generation status 和 `pageshow` 同步；
- 跨进程、重启和多 worker 的持久化任务恢复仍待补；
- `main/state/memory.py` 中的 InMemory repositories 不等于业务长期记忆；
- 受控 MemoryBlock、统一 scope、ContextPlan 和持久化 ContextCheckpoint 属于后续建议，不得写成已实现。

## 6. 压缩内容边界

压缩仅安排约两分钟，说明：

1. 先选择和按需加载，再压缩；
2. Pi Agent 代表合法切点，OpenCode 代表持久化 compaction，Codex 代表生产恢复闭环，Claude Code 代表产品控制；
3. DeepEM 保留工具 envelope、history unit、tool pair、结构化摘要和详情回捞；
4. 详细阈值、摘要字段和源码路径由原压缩专题报告承接。

## 7. 可讲性要求

- 正文朗读内容目标约 4,500-6,500 个中文字符，展示提示和问答不计入；
- 每个时间段只保留一个主结论和最多三组关键机制；
- 专有名词首次出现时用业务语言解释；
- 不朗读大表格，只指出需要观察的行列；
- 每一段结束均提供连接下一个生命周期环节的转场；
- 不展示或声称模型内部思考过程。

## 8. 预期问答

至少覆盖：

1. 为什么不再按四个项目分别讲？
2. 为什么上下文选择优先于压缩？
3. DeepEM 当前上下文管理做到什么程度？
4. 页面切换恢复是否已经解决？
5. `main/state/memory.py` 为什么不算长期记忆？
6. 长期记忆是否会造成错误事实积累？
7. P0 改动规模和优先级如何？
8. 是否需要知识图或向量数据库？

## 9. 验收标准

- 原讲稿不被修改；
- 新讲稿严格对应生命周期报告章节；
- 正文在正常讲解和翻页下适配 20-25 分钟；
- 选择、组装、隔离、状态、记忆、压缩、恢复、可观测和用户控制均被覆盖；
- 四个项目均出现，但不恢复为四个独立项目章节；
- 当前事实、后续建议和 Claude Code 公开证据边界清晰；
- Markdown 标题和段落结构有效，无 TODO、TBD 或占位文本。
