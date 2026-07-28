# 上下文管理前三优先级组会讲稿设计

## 1. 目标

为深度报告 `docs/context_management_lifecycle_deep_research_20260728.md` 新建一份 20-25 分钟聚焦式讲稿，只深入三个最优先主题：

1. 上下文选择与按需组装；
2. 任务状态、会话恢复与 ContextCheckpoint；
3. 上下文压缩在完整体系中的位置。

新讲稿输出为：

`docs/context_management_priority_talk_speaker_notes_20260728.md`

现有生命周期讲稿和深度报告保持不变。

## 2. 听众和叙事

听众了解 DeepEM 业务，但不了解上下文管理实现。讲稿从“简单问答慢”和“切换页面丢状态”两个真实问题进入，用前三优先级给出原因、外部机制、DeepEM 现状和下一步，不按四个项目分别介绍。

项目分工：

- Claude Code：progressive disclosure 和用户控制；
- OpenCode：session event/projection；
- Codex：ContextManager、world state、checkpoint 和 resume/fork；
- Pi Agent：合法压缩单元和 split-turn；
- DeepEM：贯穿三条主线对照。

## 3. 时间结构

| 时间 | 内容 | 目标 |
|---|---|---|
| 0-3 分钟 | 问题和结论 | 管理不等于压缩，说明为何只讲三项 |
| 3-6 分钟 | DeepEM 当前基线 | 已完成能力、实测收益和真实缺口 |
| 6-11 分钟 | 优先级一：选择与组装 | general/workspace、progressive disclosure、ContextPlan |
| 11-16 分钟 | 优先级二：状态与恢复 | SSE/worker、session projection、checkpoint、resume |
| 16-19 分钟 | 优先级三：压缩的位置 | Pi split-turn、DeepEM Reducer 和回捞 |
| 19-21 分钟 | 其余能力概览 | scope、长期记忆、ContextTrace 只讲边界 |
| 21-24 分钟 | P0/P1/P2 路线 | P0 可执行项和后续顺序 |
| 24-25 分钟 | 总结 | 三个可带走结论 |

## 4. 内容深度

每个优先级固定回答：

1. 对应哪个 DeepEM 问题；
2. 根因是什么；
3. 最有代表性的外部项目如何处理；
4. DeepEM 已经实现什么；
5. 还缺什么；
6. 下一步具体机制和验收指标。

外部项目不追求平均出场。每个机制讲到对象、流程和状态边界，不展开报告中的全部源码路径和参数。

## 5. 事实边界

- general 的 estimated input tokens P50 为 9374 到 116，下降 98.76%；
- 正式答案开始 P95 为 19.022 秒到 0.508 秒；
- `general/workspace`、工具 envelope、详情回捞、Reducer V2 已实现；
- `.env` 已开启两个上下文开关；
- Reducer V2 使用 65%/80%/90% 分级；
- 同进程页面切换恢复已实现；跨进程、重启和多 worker 恢复待补；
- ContextPolicy、持久化 Job/Event/Checkpoint、受控 MemoryBlock 属于后续路线；
- Claude Code 内部阈值、Prompt 和 schema 未公开。

## 6. 讲稿形式

- 正文可直接朗读；
- 每段包含展示提示和转场；
- 正文目标 4,500-6,500 个汉字；
- 预期问答不计入时长；
- 不朗读大表格，不暴露模型内部思考过程；
- 隔离、记忆和可观测性合计控制在约两分钟。

## 7. 预期问答

至少覆盖：为什么只讲三项、选择为什么优先、页面恢复边界、是否需要继续优化压缩、P0 改动大小、长期记忆何时做、为什么不直接照搬 Codex、如何验收。

## 8. 验收标准

- 三个优先级占正文主要篇幅；
- 四个项目均出现，但只服务对应机制；
- DeepEM 当前事实和建议严格分开；
- 20-25 分钟可讲完；
- 原报告和原讲稿不被修改；
- Markdown 结构有效，无未完成内容。
