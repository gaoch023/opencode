# 08 · 当前 V2：Session、agent loop 与恢复

> [返回学习总览](README.md)。当前子 agent、shell 前后台的详细链见 [07](07-current-v2.md)。

本篇固定比较旧快照 B=`388406238bd5ca15564a762840a2362c3a45bd9c` 与当前快照 N=`62c9002819d06dd48b673a1f84e94ce3e51e50e0`。下列行号均属于 N；内容来自实现与注释的静态阅读，未安装新依赖、未运行功能测试。不能将旧快照的未实现项沿用为当前结论。

## 1. 先更新命名和入口心智模型

当前根包已经是 2.1.3，根 `bun dev` 进入 `packages/cli`，而不是旧 `packages/opencode`。新核心 facade 叫 `Session`，主要逻辑拆到 `packages/core/src/session/session.ts`；同名 `session/prompt.ts` 现在负责输入 hook、附件、skill 的准备，不是 v1.2.24 那个大 loop。

| 主题 | 旧 B 的 V2 | 当前 N 的 V2 |
| --- | --- | --- |
| 持久输入 | session_input，保留 admitted/promoted 状态 | session_inbox，只保存未消费工作；交付时删行并同事务插入消息 |
| 重复 user/synthetic 输入 ID | 要求 Session、payload、delivery 精确等价 | 相同 Session+类型，first admission wins；重试新 payload/metadata/delivery 不覆盖原记录 |
| 输入种类 | 主要 prompt admission | user、synthetic 以及 compaction/move 控制项 |
| loop 单位 | provider-turn 编排 | `advanceToStep → runStep → SessionStep.attempt`，一次 step 对应一个逻辑模型调用，失败可有多次 attempt |
| step 上限 | 最后一轮移除工具 definitions | 保留 definitions 维持缓存前缀，设置 toolChoice=none |
| Session 层重试 | 没有完整 V1 式整轮重试策略 | 明确 ModelRetry 策略、RetryScheduled 事件、retry hook 与等待 |
| public compact/shell/skill/wait | OperationUnavailable | 都有实际实现 |
| interrupt | 等活动执行清理与结算 | 接受中断即返回 bool；另用 awaitIdle/wait 等清理 |
| 崩溃后推进 | 明确延后自动恢复 | durable execution claim + 启动恢复；仍非 exactly-once 或集群执行租约 |
| 系统上下文基线 | Context Epoch、来源 snapshot | InstructionState + hash blobs + initial/current values + InstructionsUpdated |

## 2. Inbox：first wins 与 pending-only 是真实契约变化

[`packages/core/src/session/session.ts:155–185`](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/core/src/session/session.ts#L155) 的 prompt 链为：先 get Session，生成/取 message ID，`admission.reconcile` 先处理重试；没有既有记录才运行 `SessionPrompt.prepare`（hook/附件/skills），成功后 admit，最后 `execution.wake` 并返回 receipt。

[`packages/core/src/session/inbox.ts:163–175`](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/core/src/session/inbox.ts#L163) 明确 first-admission-wins。匹配的是 Session 和 item type；不是旧版的 payload/delivery 精确一致。`:113–151` 能从已经交付的 user/synthetic message 恢复幂等身份，所以删除 inbox row 后，重复 ID 也不会再创建一条输入。跨 Session 或跨类型冲突仍失败；compaction 控制项有独立语义，不能把 user/synthetic 规则泛化给全部控制项。

`:183–210` 发布 `session.inbox.enqueued`；`:328–344` 的 projectDelivered 删除 pending row 并返回内容，由同一事件事务写入可见消息。因此 `session_inbox` 只代表未交付工作，不是已完成任务档案。

当前还支持取消 pending 输入、改 steer/queue，见 `session/session.ts:136–153` 与 `inbox.ts:239–269`。`resume:false` 仍是只接收，尚不推进模型执行。

## 3. steer/queue 还在，但控制项参与边界调度

`session/runner/llm.ts:73–181` 的 advanceToStep 检查 lifecycle、待交付输入和控制项，完成 instruction preparation 后才 promote 输入。初始 instruction 不可用时，输入仍留 pending（`:195–199`）。

普通 steer 在安全 step 边界交付；queue 在会话将进入 idle 时取一项，再重新判断是否还需继续。现在 compaction/move 也是 inbox 控制项：`:90–103` 原子交付控制项并发布 Compaction.Started 或 Moved，不能把控制当成普通 user 文本处理。steered compaction 对同一边界有优先规则，且不能跨过 move 控制（`inbox.ts:501–560`）。

`:175` 交付新用户输入会把 step 计数重置为 1，重新获得所选 agent 的步数额度。多条同边界 steer 不等于各自启动一个独立并行模型 loop。

## 4. 单 step 的模型流、工具与重试

当前主循环 `session/runner/llm.ts:185–192` 为 advanceToStep → runStep → 按 needsContinuation 决定续步。`runStep:203–265` 准备上下文、自动压缩、step 上限和本次模型请求。

`session/runner/step.ts:107–133` 的一次 attempt 调用 `llm.stream`，完整本地 tool-call 先发布后立即 `forkScoped` 执行；`:143–151` 结束 provider stream 后等待所有已启动工具。工具 parallel Fiber 仍不是 model-facing background；真正后台由 shell/subagent 工具自己的 Job 链处理。

上限行为也变了：`runner/llm.ts:220–238` 在最后允许 step 添加 MAX_STEPS 提示、保留工具 definitions、设置 toolChoice none；不要沿用旧版本“最后一轮工具目录为空”的结论。

Session 重试已经实际接入：

- `runner/retry.ts:32–54` 经 ModelRetry 得到决策，允许 session retry hook 修改/否决重试。
- `runner/retry.ts:57–75` 发布 RetryScheduled，然后按计划等待。
- `model-retry.ts:22–40` 是 2s 起指数退避，单间隔基础上限 10s、jitter，最多 10 次重试；可采用 Retry-After，限制为 15min；timeout 单独最多 3 次重试。
- `runner/step.ts:189–203` 对尚无 durable output 的可重试失败返回 Retry；已有部分输出则可能返回 Continue（`:266–271`），外层在 `runner/llm.ts:275–282` 注入继续提示并创建新 assistant ID。不能把所有失败都理解成无条件重播整轮。
- 输出截断也有单独 continuation，连续耗尽会明确失败（`runner/llm.ts:284–312`）。

## 5. public compact、shell、skill、wait 都已实现

`session/session.ts:256–273` 的 compact 接受一个 inbox compaction control，默认 steer，然后 wake；runner 边界执行实际手动压缩。自动压缩在 `runner/llm.ts:214`，overflow recovery 在 `:259–264`。旧文档“公开 compact 不可用”只适用于 B。

`session/session.ts:189–231` 的用户 shell 命令启动执行、记录 Shell.Started/Ended，并将结果 synthetic 写入会话；它与模型调用 shell 工具是不同入口。`:233–254` 激活 skill 并按参数 resume；`:275–277` wait 调用 execution.awaitIdle。

## 6. 中断接受与资源清理现在是两个等待点

`session/execution.ts:26–39` 明确 interrupt 返回是否接受中断；活动 Fiber 后续异步执行 finalizer 和 settled hook。`run-coordinator.ts:142–162` 标 stopping、清 doorbell、fork Fiber.interrupt，endpoint 不被慢清理阻塞。`awaitIdle:166–170` 才等待结束；`Session.wait` 使用它。

`execution.ts:150–166` 的 `{resume:true}` 在中断后可重新通知 steered 输入和控制项；queued 下一段输入保留，不会被这次 steer-scoped 续接带走。未知 Session 的公开 API 仍有身份校验；本地未拥有活动执行时 interrupt 是 no-op，不是跨机器强制取消。

## 7. 当前已有启动恢复，不能继续说“V2 完全不恢复”

`session/execution.ts:73–83` 为 execution 写 ahead claim，`:108–142` 将其与 Execution.Started 及 terminal 事件事务钩子联系：成功、失败、用户中断释放 claim；shutdown interruption 保留。claim 是数据库中的恢复标记，不是分布式 lease/fencing，更不是外部副作用 exactly-once。

`session/store.ts:197–250` 提供 listSuspended、claim、release 与 durable resume_attempts。新进程通过 `execution/restart.ts:193–230` 扫描悬挂执行并 resume；`:33` 默认恢复预算 10，`:77–98` 在恢复之前持久计数，预算耗尽发布失败并释放 claim，防止跨多次重启无限循环。

恢复分类型处理：

- `restart.ts:101–135`：之前仍 running 的 shell 被标记 cancelled，给父 Session 补“server restarted”通知；不恢复原 PID，也不自动重跑命令。
- `restart.ts:138–188`：后台子 agent 校验 parent-child 关系、执行恢复预算，再 `execution.resume(child)` 并交付完成通知。
- 主会话按 execution claim 恢复。后台 Job 的 recovery/notification 信息有持久化机制，不再能统称“只有内存状态”。

启动调用点也存在：[`packages/server/src/process.ts:241`](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/server/src/process.ts#L241)、[`packages/server/src/fetch.ts:47`](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/server/src/fetch.ts#L47)、[`packages/sdk/src/internal/host.ts:56`](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/sdk/src/internal/host.ts#L56)。`restart.ts:52–61` 明确 at-least-once，并要求恢复者确认旧 owner 已死亡；不能将其描述为任意多个共享数据库进程都可同时扫表恢复。

## 8. 系统上下文从旧 ContextEpoch 演进为 InstructionState

当前不是简单保留旧 `context-epoch.ts` 文件。`session/instruction-state.ts:21–59` 读取 Instructions sources、比较 hash values，把 delta 与冻结的可呈现文本写 InstructionsUpdated；首次同步只建立 baseline，后续 delta 才形成 chronological history。

`:89–115` 保存 `initial_values/current_values` 与 through_seq；`:139–153` 在 epoch advance 时把 current 变成新 initial；`:172` 起渲染每次模型请求的稳定 baseline。schema 见 `session/sql.ts:163–176` 的 instruction_blob 与 instruction_state。这延续“稳定基线+后续变化留痕”的思想，但表、事件、插件 instruction hooks 已变，不能继续拿旧 ContextEpoch 的字段名当当前实现。

## 建议给学习者的更新阅读顺序

仍先用 v1.2.24 读懂 prompt/processor/llm；然后在 N 按 `session/session.ts → inbox.ts → execution.ts → runner/llm.ts → runner/step.ts` 走主线。接着补 `runner/retry.ts`、`instruction-state.ts`、`execution/restart.ts`。旧 B 的 V2 笔记可解释重构过程，但不能再当当前功能缺失列表。


## 当前固定提交源码索引

短文件名与 `:行号` 延续所在段落的源文件；以下链接均固定到当前 N 提交。

| 文件 | 源码 |
| --- | --- |
| `packages/core/src/model-retry.ts` | [N 源码](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/core/src/model-retry.ts#L1) |
| `packages/core/src/session/execution.ts` | [N 源码](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/core/src/session/execution.ts#L1) |
| `packages/core/src/session/execution/restart.ts` | [N 源码](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/core/src/session/execution/restart.ts#L1) |
| `packages/core/src/session/inbox.ts` | [N 源码](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/core/src/session/inbox.ts#L1) |
| `packages/core/src/session/instruction-state.ts` | [N 源码](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/core/src/session/instruction-state.ts#L1) |
| `packages/core/src/session/run-coordinator.ts` | [N 源码](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/core/src/session/run-coordinator.ts#L1) |
| `packages/core/src/session/runner/llm.ts` | [N 源码](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/core/src/session/runner/llm.ts#L1) |
| `packages/core/src/session/runner/max-steps.ts` | [N 源码](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/core/src/session/runner/max-steps.ts#L1) |
| `packages/core/src/session/runner/retry.ts` | [N 源码](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/core/src/session/runner/retry.ts#L1) |
| `packages/core/src/session/runner/step.ts` | [N 源码](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/core/src/session/runner/step.ts#L1) |
| `packages/core/src/session/session.ts` | [N 源码](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/core/src/session/session.ts#L1) |
| `packages/core/src/session/sql.ts` | [N 源码](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/core/src/session/sql.ts#L1) |
| `packages/core/src/session/store.ts` | [N 源码](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/core/src/session/store.ts#L1) |
| `packages/sdk/src/internal/host.ts` | [N 源码](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/sdk/src/internal/host.ts#L1) |
| `packages/server/src/fetch.ts` | [N 源码](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/server/src/fetch.ts#L1) |
| `packages/server/src/process.ts` | [N 源码](https://github.com/gaoch023/opencode/blob/62c9002819d06dd48b673a1f84e94ce3e51e50e0/packages/server/src/process.ts#L1) |
