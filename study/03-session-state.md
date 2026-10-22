# 03 · Session、持久化、并发与恢复

> **历史快照说明（2026-10-22 更新）：** 本文比较 v1.2.24 与 2026-10-08 的 `388406238b`，其中“当前/最新/V2”都指当时快照。远端已在 2026-10-10 推广 V2；当前 2.1.3 已支持 subagent 与 shell 后台执行、持久通知及受限启动恢复。当前能力请读 [07](07-current-v2.md) 和 [08](08-current-session.md)，不要将本文的未实现项当成当前结论。

> [返回学习总览](README.md)。A 为 v1.2.24，B 为固定 dev；有 C 标记时指同一 dev 中的 V2。行号按各自提交计算。

比较固定在两个提交：基线 A=`c6262f9d4002d86a1f1795c306aa329d45361d12`（release v1.2.24），当前 B=`388406238bd5ca15564a762840a2362c3a45bd9c`。下面 V1、V2 是 B 中并存的实现路线，不能把 B 的包版本号当作 V2 已全面替代 V1。行号均来自指定提交的真实源码；本报告未执行新增测试。

## 1. 最值得保留的基线心智模型

Session 是一段持久化会话，不是一个 OS 进程，也不是一个始终活跃的线程。基线 A 已经用 SQLite 保存 `session/message/part`，不是仅存内存或 JSON 文件。内存 `state[sessionID]` 管的是“这个会话现在是否正在运行”，数据库管的是“会话说过和做过什么”。两者生命周期不同。

- [A `packages/opencode/src/session/index.ts:291`](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/session/index.ts#L291)：`createNext` 构造元数据，315 插入 `SessionTable`，316 通过 `Database.effect` 发 Created。
- A 同文件 `:679`：`updateMessage` 直接 upsert `MessageTable`，692 在数据库 effect 中发布 Bus 事件。
- [A `packages/opencode/src/session/prompt.ts:65`](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/session/prompt.ts#L65)：`Instance.state` 按 session ID 保存 `AbortController` 和回调数组。Instance 的具体隔离键是 directory：[A `packages/opencode/src/project/instance.ts:103`](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/project/instance.ts#L103)。
- [A `packages/opencode/src/session/status.ts:44`](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/session/status.ts#L44)：idle/busy/retry 是 Instance 内存状态；设为 idle 后删除映射（61–74）。服务重启后没有“仍 busy”的恢复保证。

三层对照：

| 事项 | A：v1.2.24 | B：最新 V1 路线 | B：V2 原生路线 |
|---|---|---|---|
| Session 存储 | 直接更新 SQLite session/message/part，再发 Bus 事件 | 业务发布 V1 领域事件，共享 EventV2 事务投影到 session/message/part | Session 事件写日志，并投影 session_message、session_input、epoch；共用 session 元数据 |
| 输入何时进入模型历史 | createUserMessage 后立即可见 | createUserMessage 后立即可见 | 先 admit，再在安全边界 promote |
| 运行互斥 | Instance 内 state+AbortController | Instance 内 SessionRunState + Runner 状态机 | process-global Coordinator，以 SessionID 为键 |
| 重入 | 返回 Promise，注册 callbacks 等现有 loop | Deferred join 现有 runner | resume join 当前 drain；wake 合并后继提示 |
| 中断 | abort()，删内存状态 | interrupt fiber、等待清理；递归取消相关后台 job | interrupt 当前本进程 drain，清理现有 pendingWake，保留 durable inbox |
| shell 与对话协调 | prompt.ts 中共享手写状态 | Idle/Running/Shell/ShellThenRun | 原生 Session.shell 当前 OperationUnavailable；不能声称完成同等入口 |
| 断线事件重放 | Bus 实时通知，不应假定可按持久事件序列重放 | EventV2 持久事件+旧客户端转发 | durable event cursor replay/tail，与临时 delta 分开 |
| 崩溃后自动续跑 | 内存 loop 消失 | 内存 fiber/job 消失，事件持久化不等于恢复执行 | inbox 持久化，但不自动重试已发送而结果不明的 provider work |

## 2. 从 A 到最新 V1：调度已 Effect 化，语义仍可用旧主线理解

A 的输入链：

```text
SessionPrompt.prompt
  -> createUserMessage（直接可见）
  -> noReply? 返回
  -> loop
      -> start(sessionID) 得到 abort.signal
      -> 已在跑？callbacks.push 等同一 loop
      -> while(true) 每轮读持久历史
      -> defer cancel，终止后回调返回
```

证据：A prompt.ts `:158` prompt；`:238` start；`:256` cancel；`:274` loop；`:277` start/resume；`:278` 注册 callbacks；`:294` while；`:298` 重新读取压缩后历史。这里“新 prompt 加入正在运行的会话”不等于有持久 FIFO inbox；消息已进入普通历史，下一轮重新读取时会看到它。

最新 V1 仍是同样外层链，但以 Effect service/fiber/Deferred 管理控制流：

- [B `packages/opencode/src/session/prompt.ts:1052`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/prompt.ts#L1052)：`prompt` 先 `createUserMessage`，`noReply` 直接返回，否则 `loop`。
- B 同文件 `:1343`：`loop` 委托 `state.ensureRunning(sessionID, lastAssistant(...), runLoop(...))`；取消时可返回最后 assistant。
- [B `packages/opencode/src/session/run-state.ts:35`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/run-state.ts#L35)：InstanceState 中的 Map<SessionID, Runner>；finalizer 取消全部 runner。`:59` 创建 Runner，onBusy/onIdle 对接 status。
- [B `packages/opencode/src/effect/runner.ts:33`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/effect/runner.ts#L33)：四态 `Idle | Running | Shell | ShellThenRun`。
- B runner.ts `:115`：ensureRunning 在 Running/ShellThenRun 时只 await 已有 Deferred；Shell 状态则记一个 pending run，shell 结束再启动（`:100`）；Idle 才 startRun。
- B runner.ts `:140`：startShell 仅 Idle 允许；忙时 RunnerBusy。并非随便让 shell 与相同会话的 foreground loop 同时修改状态。
- B runner.ts `:171`：cancel 中断当前 fiber，并通过 Deferred 将等待者唤醒；结构化清理比 A 的 abort+删映射更明确。
- B run-state.ts `:77`：先 `cancelBackgroundJobs` 再 cancel 当前 runner；`:111` 按 job.id / metadata.sessionId / parentSessionId 形成递归取消链。不要因此把整个后台系统理解成独立 OS worker。

HTTP “异步”也需要区分：[B `packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts:311`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts#L311) 的 promptAsync 使用 `Effect.forkIn(scope)` 执行同一个 V1 prompt service 并立即返回 NoContent；这是 HTTP 请求不等待，不是另开进程或等于 task 工具的 background 模式。

## 3. 持久化升级同时惠及最新 V1，不是 V2 独有

最新 V1 不再在每个 Session 方法里手写表更新。例如 [B `packages/opencode/src/session/session.ts:535`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/session.ts#L535) 的 createNext 发布 `SessionV1.Event.Created`；V1 消息/part 更新同样发布事件。

共享核心 [B `packages/core/src/session/projector.ts:214`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/projector.ts#L214) 注册 V1 Created 投影；`:260` MessageUpdated 投影 upsert MessageTable；`:310` PartUpdated 投影 upsert PartTable 并更新 usage。V2 原生事件在同一 projector 的 `:348` 起注册，但投影目标是 SessionMessageTable 等。

`EventV2` 的 durable commit 不只是 PubSub：[B `packages/core/src/event.ts:205`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/event.ts#L205) 开始提交逻辑；`:320` 执行 projector，`:323` 执行本地 commit hook，`:324` 更新 aggregate sequence，`:336` 写 EventTable；这些都在 `db.transaction(..., {behavior:'immediate'})` 中（`:351`）。事件事实和查询投影一起提交，避免“先通知再写库”的裂缝。

投影对象有四类，应分开记：

- `message/part`：V1 兼容读模型（B sql.ts `:68`、`:82`）。
- `session_message`：V2 类型化消息模型，`seq` 是源 aggregate durable event 顺序（B sql.ts `:119`）。
- `session_input`：接收但可能尚未进入历史的输入，admitted_seq/promoted_seq（B sql.ts `:140`）。
- `session_context_epoch`：baseline/snapshot/baseline_seq（B sql.ts `:168`），不是运行句柄或后台 job。

## 4. V2 的重要新边界：接受输入 ≠ 执行输入

链条直接来自 B 源码：

```text
SessionV2.prompt({id?, sessionID, prompt, delivery?, resume?})
 -> SessionInput.admit
 -> durable PromptAdmitted + session_input
 -> resume !== false 时 execution.wake(sessionID)
 -> 返回 admission receipt

SessionExecutionLocal（process-global）
 -> Coordinator(wake/run)
 -> SessionStore.get(sessionID)
 -> LocationServiceMap.get(session.location)
 -> Location-scoped SessionRunner.run({sessionID, force})
 -> 初始化 ContextEpoch
 -> promote 输入（Prompted + 可见 user message）
 -> 一次 provider turn / 工具 settlement
 -> 下一安全边界再读历史和 pending 输入
```

- [B `packages/core/src/session.ts:360`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session.ts#L360)：`prompt` 整段 uninterruptible，默认 delivery=`steer`。`:368` admit，`:380` 比较精确重试，`:382` wake。
- [B `packages/core/src/session/input.ts:41`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/input.ts#L41)：admit 如 ID 已存在先返回；否则发布 PromptAdmitted；冲突并发后再查既有记录。same ID 必须匹配 Session、编码后的 prompt、delivery（`:191`），否则 facade 抛 PromptConflictError。
- `resume:false` 是仅 admission；不同于 V1 noReply，前者还不进入模型可见历史。V1 noReply 的 createUserMessage 已经发生。
- [B `packages/core/src/session/projector.ts:348`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/projector.ts#L348)：Prompted projector 同事务调用 projectPrompted 标记 promoted_seq，再 run 消息投影。`:362` PromptAdmitted 只建立 inbox 记录。
- B core/session.ts `:208` create： supplied session ID 已存在时返回现有 Session；并发创建丢失投影 race 时同样采用既有身份（`:245`）。身份幂等不是执行 exactly-once 保证。

### steer、queue、wake、resume 四个不同概念

- steer：B input.ts `:245`，取到 captured cutoff 的所有 pending steers，按 admitted_seq 升序 promote。在 provider turn 完成后的安全边界才能生效，不抢断当前 token stream。
- queue：B input.ts `:268`，只取最早一条 pending queue。当前 drain 的 continuation 结束之后才消耗；处理完这一条再判断 continuation/下一条。
- wake：B run-coordinator.ts `:81`，运行中只设 pendingWake=true；不是每个请求都 fork 一轮模型调用。空闲启动 `force=false`。
- resume：B run-coordinator.ts `:67`，已有 owner 则 join Deferred；空闲启动 `force=true`。显式选择“从持久历史继续”与“通知有输入”有不同风险语义。
- B runner/llm.ts `:396`：wake 没有 pending 输入则返回；`:402` 外层循环只在 continuation 结束后取 queue；`:409` continuation 内设置 promotion=steer；`:195` 推入新用户输入时重置 step allowance。

例子：模型刚发工具调用时用户提交 steer A、queue B。当前 provider turn 和已启动工具先结算；下一轮将 A 加入历史，B 继续留 inbox。直到工具续接/模型回答不再要求 continuation，才 promote B，重新进入下一段对话。

## 5. V2 运行协调是“本进程每会话一个 drain”，还不是分布式调度

- [B `packages/core/src/session/execution/local.ts:16`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/execution/local.ts#L16) 创建 coordinator；`:17` drain 时用 SessionStore 查最新 Location；`:20` 在 Location services 中调用 runner。
- B 同文件 `:40` 明确 makeGlobalNode。SessionRunner 的实现 B runner/llm.ts `:423` 是 makeLocationNode。一个 Location runner service 可以执行多个不同 Session；不要每个 Session 都建一整份 service layer。
- [B `packages/core/src/session/run-coordinator.ts:28`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/run-coordinator.ts#L28) active 是进程内 Map。不同 Session key 可并发；相同 key 只有一个 owner。active 返回的是 Map keys 的快照（`:103`），不是 durable busy 标志，不是所有后台子任务列表。
- 同文件 `:51` settle 处理合并 wake 的后继执行；`:94` interrupt 设置 stopping、清除当时已登记 pendingWake，再 Fiber.interrupt(owner)。中断等待清理完成；清理期间新来的 wake 仍能注册后继，因此“取消”不等于永久封禁这个会话。
- `resume` 等待者自身被中断不应取消 owner：对应测试 [B `packages/core/test/session-run-coordinator.test.ts:351`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/test/session-run-coordinator.test.ts#L351)。这解决“断开一个 HTTP 等待者是否杀死整个会话”的所有权问题。
- B runner/llm.ts `:180` 每轮检查 Session 的 directory/workspaceID 是否仍匹配 runner Location，不匹配 interrupt。但这不是完整 distributed lease/epoch fencing。
- B runner/llm.ts `:51` 仍把 durable multi-node ownership 标 TODO；`:52` durable busy/retry/idle/failure 也 TODO。不要将 EventV2 的 replay owner_id 等同于模型执行 owner。

## 6. 取消、崩溃、恢复：不可把 durable 理解成“可安全重做一切”

V2 interrupt 只针对当前进程 active execution；空闲或缺失 Session 不做任何事情。durable inbox 不被删除，后续可 wake/resume。进程重启后 Coordinator 的 active Map 为空；当前 provider 是否已真正接受请求可能不可知。

B runner/llm.ts `:119` 的 failInterruptedTools 扫描历史中 pending/running tool，写入 Tool.Failed，错误文本 `Tool execution interrupted`，保留 provider metadata，并带 assistantMessageID 区分不同轮复用的 callID。它把不明确的历史执行收敛为失败；没有擅自重跑文件修改、shell 命令等副作用。

B runner/llm.ts `:398` 只有有 eligible input 或显式 force 才进入 runner；这能说明：`wake` 不会仅因历史中存在一个未续接的 assistant 就自动发请求。它不是完整 crash-recovery daemon。显式 resume 可以从投影历史继续，但用户/调用方承担继续的决策，而不是系统保证 provider exactly-once。

相关现有测试（只读审阅，未在本分析中运行）：

- [B `packages/core/test/session-run-coordinator.test.ts:9`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/test/session-run-coordinator.test.ts#L9) concurrent resumes join；`:141` wakes coalesce；`:218` interrupt 清 pending wake；`:247` cleanup 期间新 wake；`:372` different keys concurrent。
- [B `packages/core/test/session-prompt.test.ts:342`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/test/session-prompt.test.ts#L342) 并发 exact retry 只有一条 admitted 记录且 messages 仍空；`:362` 并发 promotion 只有一个 Prompted；`:386` captured cutoff。
- [B `packages/core/test/session-runner.test.ts:658`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/test/session-runner.test.ts#L658) initial system context 恢复可用后再运行；`:690` Session move 使源 Location runner interrupt。

## 7. Context Epoch：保存将提供给模型的稳定上下文基线

这是学习 Session 存储时容易漏掉的新内容。基线/最新 V1 的 system/instruction 组装不能直接等同于 V2 epoch。

[B `packages/core/src/session/context-epoch.ts:23`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/context-epoch.ts#L23) initialize，在首次 promote 之前完成；`:80` 不存在 epoch 时加载完整 context 并初始化。不完整则 InitializationBlocked，输入仍 pending，不会变成模型看见但系统基线不存在的半状态。

之后 prepare（`:40`）对比当前观察与 snapshot。未变/暂不可替换保留 baseline（`:63`）；新事实以 ContextUpdated chronological System message 发布，commit hook 原子推进 snapshot（`:72`）；完成 compaction 后，完整观察可用时可按新的 baseline_seq 替换基线（`:59`）。这样稳定 provider cache baseline，同时后来的上下文变化按时间留在历史中，而不是悄悄改写过去。

这里还要分开“外部手动 compact API”与“runner 内自动压缩”：B core/session.ts `:417` 的 public compact 仍返回 OperationUnavailable；但 runner/llm.ts `:109` 已构造 SessionCompaction，`:152`–`:166` 有自动压缩/overflow压缩的转移，后续 runTurn 实际执行。不能因为 public API 未实现就说 V2 没有任何压缩，也不能因为 runner 有压缩就说 public compact 已就绪。

## 8. 事件实时显示与可重放历史应分开学习

V2 的 token/text/reasoning/tool-input delta 是实时事件，不都写 durable log。[B `packages/schema/src/session-event.ts:479`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/schema/src/session-event.ts#L479) 的 Definitions 含 Text.Delta 等；public durable inventory 在 `:448` 左右只列 Started/Ended 等 durable 边界。

B core/session.ts `:346` sessions.events 先 EventV2.durable，再过滤公开 durable SessionEvent；`:352` history 是有限分页 counterpart。前者并不重放每一个历史 token。

B core/event.ts `:565` 给每个 active tail 建 sliding-capacity-1 dirty signal；`:585` 先订阅再读历史，醒来后重新 query SQLite。通知可以合并，因为完整事实在数据库。不能把合并通知理解为丢掉了 durable events。

## 9. V1/V2 bridge 的源码事实与规格差异

当前 [B `packages/opencode/src/event-v2-bridge.ts:19`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/event-v2-bridge.ts#L19) 只替发布的 core events 附加 routed Instance Location；`:35` 监听事件并发 GlobalBus payload，durable 时再发 sync envelope。它没有调用 SessionV2.prompt/resume，不能推导为 V1 输入会驱动第二个模型 loop。

B 最新 V1 prompt 仍直接调用 SessionPrompt.runLoop（prompt.ts `:1052`、`:1343`）；HTTP handler `:295`、`:311` 仍使用这个 service。

需要明确标记：`specs/v2/session.md` 仍有“V1-to-V2 shadow bridge publishes Prompted”描述，但在 B 当前生产源码搜索 `Prompted`，发布点仅见 core/session/input.ts 原生 promote。V1 projector 为 V1 message/part 写兼容表，没有发现把 V1 loop 结果自动重跑 V2 loop 的代码。该段规格不能当作当前实现证据，文档应注明这一不一致。V2 Session.create 用 SessionV1.Created 元数据事件以及共享投影，不等于自动 shadow 执行。

## 10. 适合 v1.2.24 学习者的阅读顺序

1. A prompt.ts:158、:238、:274 + session/index.ts:679：先贯通“普通输入→内存运行占用→重读持久历史”。
2. B prompt.ts:1052、:1343 + run-state.ts:35 + effect/runner.ts:33：只迁移运行控制心智模型，先不碰 V2 inbox。
3. B session/session.ts:535 + core/session/projector.ts:214 + core/event.ts:205：理解最新 V1 已经通过事件事实投影兼容表。
4. B core/session.ts:360 + session/input.ts:41、:245、:268：理解 admission/promotion、steer/queue。
5. B execution/local.ts:16 + run-coordinator.ts:67 + runner/llm.ts:392：理解进程全局协调、Location 服务与安全边界。
6. 最后读 context-epoch.ts、EventV2 durable replay，区分持久化状态、运行时状态与还没实现的 crash recovery。


## 固定提交源码索引

正文中的短文件名和 `:行号` 延续所在段落的版本与文件。下表按完整路径提供两个提交的入口；“—”表示该路径在该提交中不存在，不能用学习分支中的同名文件代替新版源码。

| 文件 | A · v1.2.24 | B · dev（含最新 V1/V2） |
| --- | --- | --- |
| `packages/core/src/event.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/event.ts#L1) |
| `packages/core/src/session.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session.ts#L1) |
| `packages/core/src/session/context-epoch.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/context-epoch.ts#L1) |
| `packages/core/src/session/execution/local.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/execution/local.ts#L1) |
| `packages/core/src/session/input.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/input.ts#L1) |
| `packages/core/src/session/projector.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/projector.ts#L1) |
| `packages/core/src/session/run-coordinator.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/run-coordinator.ts#L1) |
| `packages/core/src/session/runner/llm.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/runner/llm.ts#L1) |
| `packages/core/src/session/sql.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/sql.ts#L1) |
| `packages/core/test/session-prompt.test.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/test/session-prompt.test.ts#L1) |
| `packages/core/test/session-run-coordinator.test.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/test/session-run-coordinator.test.ts#L1) |
| `packages/core/test/session-runner.test.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/test/session-runner.test.ts#L1) |
| `packages/opencode/src/effect/runner.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/effect/runner.ts#L1) |
| `packages/opencode/src/event-v2-bridge.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/event-v2-bridge.ts#L1) |
| `packages/opencode/src/project/instance.ts` | [源码](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/project/instance.ts#L1) | — |
| `packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts#L1) |
| `packages/opencode/src/session/index.ts` | [源码](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/session/index.ts#L1) | — |
| `packages/opencode/src/session/prompt.ts` | [源码](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/session/prompt.ts#L1) | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/prompt.ts#L1) |
| `packages/opencode/src/session/run-state.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/run-state.ts#L1) |
| `packages/opencode/src/session/session.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/session.ts#L1) |
| `packages/opencode/src/session/status.ts` | [源码](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/session/status.ts#L1) | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/status.ts#L1) |
| `packages/schema/src/session-event.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/schema/src/session-event.ts#L1) |
| `specs/v2/session.md` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/specs/v2/session.md#L1) |
