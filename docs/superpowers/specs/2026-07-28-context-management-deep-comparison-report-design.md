# 上下文管理深度横向调研报告设计

## 1. 目标

新建一份上下文管理深度调研报告，以七个上下文生命周期环节为主体。每个环节完整覆盖 Pi Agent、OpenCode、Codex CLI、Claude Code，并与 DeepEM 当前实现逐项比较。

现有全景报告 `docs/context_management_lifecycle_research_20260727.md` 保持不变，作为概览版。新报告输出为：

`docs/context_management_lifecycle_deep_research_20260728.md`

## 2. 核心原则

采用“全项目覆盖 + 代表项目深挖”：

- 七章均讨论四个外部项目；
- 每章选择一到两个最有代表性的项目深入源码对象、调用流程、状态、持久化、恢复和失败边界；
- 其他项目仍给出证据化结论；
- 没有独立实现、源码中未发现、闭源产品未公开必须分别表述；
- 不为了形式对称推断不存在或未公开的机制；
- 每章以 DeepEM 当前实现、差距、可借鉴机制和验收实验收尾。

## 3. 调研对象与证据

| 对象 | 证据类型 | 固定版本或边界 |
|---|---|---|
| Pi Agent | 源码、类型、调用流程和测试 | pi-mono `5bc1c2c0a6f07e00e8c240304182f213ab8d311f` |
| OpenCode | 源码、session 数据模型、流程和测试 | `7534d23551f665e65080809975b4ca5c7d63807b` |
| Codex CLI | 源码、测试、官方行为 | `95637f7056835fea66bdd0044414af480fc0fd74` |
| Claude Code | Anthropic 官方文档 | 不推断内部阈值、Prompt、模型、算法或 schema |
| DeepEM | 当前代码、测试、`.env` 和评测报告 | 区分已实现、部分实现、已有设计、尚未实现 |

“官方未公开”不能写成“产品没有”；“源码中未发现独立机制”不能写成“系统绝对不具备该能力”。

## 4. 七个生命周期章节

### 4.1 上下文来源、发现与选择

重点问题：系统有哪些来源，如何发现，如何判断相关性、权威性、新鲜度、权限和成本。

- 代表深挖：Claude Code progressive disclosure；Codex ContextManager 输入来源。
- 完整覆盖：Pi Agent session entries、OpenCode session projection。
- DeepEM 对照：general/workspace、意图与模式、来源选择和 fallback。

### 4.2 按需加载、预算与输入组装

重点问题：何时只加载目录，何时加载 schema 或正文；system、状态、历史、工具和证据如何分配预算并形成模型输入。

- 代表深挖：Codex 窗口与 ContextManager；Claude Code MCP/skills/rules 按需加载。
- 完整覆盖：Pi Agent transform/compaction 输入；OpenCode system+messages+tools 估算和 serialize。
- DeepEM 对照：ExecutionPolicy、history projection、PromptCompositionEstimator、ContextBudgetEstimator。

### 4.3 隔离、权限与作用域

重点问题：租户、用户、会话、分支、Case、工作区、工具和子任务边界如何执行。

- 代表深挖：Claude Code subagent 独立窗口；Codex session/workspace/tool/approval 边界。
- 完整覆盖：Pi Agent session/branch；OpenCode session scope。
- DeepEM 对照：general 隔离、工具详情会话校验、统一 scope key 缺口和负向测试。

### 4.4 会话、任务、状态与恢复

重点问题：Conversation、Turn、Run、Task、Event 和 Checkpoint 如何组织；断连、失败、重启、resume、rewind 和 branch 如何处理。

- 代表深挖：OpenCode session event/projection；Codex checkpoint/resume/fork。
- 完整覆盖：Pi Agent reload/branch；Claude Code transcript、continue/resume/rewind/branch 的官方行为。
- DeepEM 对照：ActiveChatStream、generation status、worker 终态、`pageshow`、同进程与跨进程边界。

### 4.5 压缩、淘汰与细节回捞

重点问题：何时压缩、选择什么单元、合法切点、产物结构、失败回退和原始细节恢复。

- 代表深挖：Pi Agent compaction/split-turn；Codex local/remote/token-budget compaction。
- 完整覆盖：OpenCode compaction message；Claude Code 自动、手动和区间总结公开行为。
- DeepEM 对照：tool envelope、`tool_result_id + path`、Reducer V2、65%/80%/90%、semantic cache 和 ContextCheckpoint 缺口。

### 4.6 当前状态重注入与长期记忆治理

重点问题：当前权威状态、session handoff、长期事实、规则和偏好如何区分；记忆如何产生、验证、冲突、失效和删除。

- 代表深挖：Codex world state 重注入；Claude Code CLAUDE.md/rules/skills/auto memory。
- 完整覆盖：Pi Agent handoff/details；OpenCode previous summary 和 session continuity。
- DeepEM 对照：当前 workspace state、进程内 summary cache、尚未形成 MemoryBlock 治理体系。

### 4.7 可观测性、用户控制与评测

重点问题：系统和用户如何看到上下文来源、预算、策略、压缩、状态和记忆；如何证明没有误选、越界和恢复失败。

- 代表深挖：Claude Code `/context`、`/compact`、`/memory` 等控制；Codex usage/window/checkpoint 观测。
- 完整覆盖：Pi Agent CompactionEntry details/tokens；OpenCode compaction part/event。
- DeepEM 对照：execution policy、context budget、context reduction、debug trace、ContextTrace 和来源面板缺口。

## 5. 每章统一分析模板

每个生命周期章节按以下顺序展开：

1. 问题定义、输入输出和评价标准；
2. Pi Agent：数据对象、核心流程、持久化、失败边界、证据；
3. OpenCode：数据对象、核心流程、持久化、失败边界、证据；
4. Codex CLI：数据对象、核心流程、持久化、失败边界、证据；
5. Claude Code：官方公开行为、可确认边界和未公开部分；
6. 四项目机制比较表；
7. DeepEM 当前实现与证据；
8. 成熟度判断：已实现、部分实现、已有设计、尚未实现；
9. 可直接借鉴机制、需要业务适配的机制、不适合照搬的机制；
10. 验收指标、负向测试和失败条件。

每章不要求四个项目等篇幅。代表项目需要给出至少一个具体流程或数据结构；非代表项目至少说明机制位置、能力边界和证据状态。

## 6. 深度标准

关键结论不能停留在“支持某能力”，而要回答：

- 管理的对象是什么；
- 谁触发、在什么时候触发；
- 谁拥有状态和写入权；
- 数据保存在哪里，以什么结构保存；
- 下一轮、断连或重启时如何恢复；
- 失败、overflow、冲突或无权限时如何回退；
- 用户是否可查看、控制或纠正；
- 对应源码路径、类型名、函数名、测试或官方文档是什么；
- 对 DeepEM 的业务对象和多用户环境是否适用。

报告目标是增加机制深度，不以机械扩充篇幅为目标。预计正文约 30,000-40,000 个字符；若某项目证据不足，明确边界而不填充推测。

## 7. DeepEM 比较方法

DeepEM 每章使用同一成熟度标记：

| 标记 | 含义 |
|---|---|
| 已实现 | 当前代码和测试能够证明主路径 |
| 部分实现 | 有局部能力，但作用域、持久化或恢复不完整 |
| 已有设计 | 已有方案文档，当前代码不能证明已落地 |
| 尚未实现 | 当前代码和设计均未形成完整机制 |

比较必须保留以下当前事实：general/workspace 已实现；工具 envelope 和详情回捞已实现；Reducer V2 与语义压缩已开启；页面切换同进程恢复已实现；跨进程恢复和统一持久化 ContextCheckpoint 尚未完成；受控长期记忆尚未形成。

## 8. 总体结构

1. 执行摘要；
2. 调研方法、证据和统一术语；
3. DeepEM 基线与成熟度总表；
4. 七个生命周期深度章节；
5. 全生命周期综合矩阵，只总结不重复正文；
6. DeepEM 目标架构与 P0/P1/P2；
7. 综合评测计划；
8. 风险、证据边界和结论；
9. 源码与官方资料索引。

## 9. 验收标准

- 当前概览报告保持不变；
- 七个生命周期章节均完整覆盖四个外部项目和 DeepEM；
- 每章至少有一个代表项目的具体流程或数据结构；
- 每章均有比较表、DeepEM 成熟度、借鉴判断和验收方法；
- “没有独立机制”“未发现证据”“闭源未公开”用语准确区分；
- DeepEM 当前实现、已有设计和后续建议不混写；
- 关键事实有源码路径、类型、测试或官方资料依据；
- Claude Code 不出现未经官方资料确认的内部参数；
- 总比较章不重复七章正文；
- Markdown 结构有效，无 TODO、TBD、FIXME 或占位内容。
