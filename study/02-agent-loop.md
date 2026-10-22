# 02 · Agent loop、模型流与上下文

> **历史快照说明（2026-10-22 更新）：** 本文比较 v1.2.24 与 2026-10-08 的 `388406238b`，其中“当前/最新/V2”都指当时快照。远端已在 2026-10-10 推广 V2；当前 2.1.3 已支持 subagent 与 shell 后台执行、持久通知及受限启动恢复。当前能力请读 [07](07-current-v2.md) 和 [08](08-current-session.md)，不要将本文的未实现项当成当前结论。

> [返回学习总览](README.md)。A 为 v1.2.24，B 为固定 dev；有 C 标记时指同一 dev 中的 V2。行号按各自提交计算。

取证范围：

- A = `c6262f9d4002d86a1f1795c306aa329d45361d12`（release: v1.2.24）。
- B = `388406238bd5ca15564a762840a2362c3a45bd9c` 中 `packages/opencode` 的 V1 路径。
- C = 同 B 提交中 `packages/core` 的 V2 路径。

下面 `A:path:line` / `B:path:line` 均指对应不可变提交中的文件行号。未运行测试；所有结论来自实现和现有测试断言的静态阅读。

## 1. 先学什么，再学什么

A 最适合先建立这个概念：**外层业务循环负责决定下一步；processor 负责消费一次模型流并保存消息；LLM 适配器负责 provider 调用和工具分发。** A 并不是没有分层，只是用 Promise/AbortController 和 namespace 做组织，主线较集中。

B 保留这条主线，却将状态和能力迁为 Effect services/layers，消息 Schema 被显式称为 SessionV1；新增中断清理、provider 异常兼容和 native runtime 选择。读懂 Effect 写法后，仍能在 `runLoop` 中找到 A 的同一批分支。

C 是另一套 Session 主干：输入 inbox、执行协调、位置作用域、历史投影、上下文 epoch、工具目录、事件发布各自独立。它没有把 A 的 `SessionPrompt.loop` 改名后直接调用，也没有把一整个多轮 agent loop 委托给 LLM SDK。

特别容易误会的名字：A 文件本来就叫 `message-v2.ts`，这不表示它采用 C 的 Session V2；B 可以选择 native LLM runtime，这也不表示 B 会切换到 C 的 inbox/runner。

## 2. agent loop 对照表

| 观察点 | A v1.2.24 | B 当前 V1 | C 当前 V2 |
|---|---|---|---|
| 主循环 | `SessionPrompt.loop` Promise + `while(true)` | `runLoop`（span 名 `SessionPrompt.run`）Effect generator + `while(true)`，`loop` 通过 `SessionRunState.ensureRunning` 进入 | `SessionRunner.run`：外层依次 drain queue，内层进行 provider continuation |
| 输入进入 | 先 `createUserMessage`，`noReply` 为真只保存，否则 loop | 同样 create/save 用户消息，`noReply` 返回 | 先 durable inbox，safe turn boundary 才 promote；runner 才看到用户消息 |
| 一轮前的历史 | 每次从已保存消息读取，filterCompacted，倒查最后 user/assistant/finished/task | 保留总体模式，用 `MessageV2.latest` 抽出最后几项 | 每个 turn 重读 Session 元数据、模型、Location、epoch、按 seq 排序的投影历史 |
| provider 一轮 | processor 调 `LLM.stream`，消费 AI SDK fullStream | processor 调统一 `Stream<LLMEvent>`；默认 AI SDK，native 可选 | runner 显式调用一次 `llm.stream(request)`；本地工具执行和是否继续由 runner 所有 |
| 继续依据 | assistant finish 为 tool-calls/unknown，processor 的 continue/compact/stop | 还检查 assistant 中确实有未被 orphan 清理的本地工具，容忍 provider 错发 stop | 本轮出现本地 tool-call => needsContinuation；无工具但有新 steer 也继续；当前 continuation 结束后才取下一个 queue |
| 用户中途补充 | 直接保存到历史，下一次循环重读；A 会暂时包裹 system-reminder | 仍由下一 turn 的历史读取 | 明确 steer/queue 两种 admission 语义，边界 promote |
| steps | `step >= agent.steps` 时加 MAX_STEPS 提示，仍提供工具 | 同样主要是提示约束 | 最后一允许 turn 不 materialize 工具、toolChoice none；若 provider 仍发本地调用，失败结算 |
| 工具执行 | AI SDK execute 回调；processor 记录 call/result 事件 | 默认同源；native adapter 自己分派但仍提供同一 LLMEvent 接口 | 先 durable publish 调用，再 FiberSet eager execution；等本轮所有工具结算后继续 |
| 重试 | processor 内 while/try/catch，SessionRetry.delay，retryable 就 continue，无 retry-count 上限 | Effect.retry + Schedule，5 次重试上限，抖动、Retry-After、更多网络和 5xx 判定 | Session runner 未移植 V1 的整轮 retry/status 策略；底层 RequestExecutor 对 retryable HTTP 请求有 2 次重试；另有受限 overflow-compaction 重试 |
| 上下文 | 每轮重新收集环境、instructions，与 agent/provider/user system 合并 | 再加入 skill 和 MCP 指令，仍逐轮重新构造 | epoch 持久化 baseline；后续来源变化形成按时间排列的 system message；compaction 后完整观察可用时重建 baseline |

源码入口：[A `packages/opencode/src/session/prompt.ts:158,274,294,318,542,557,658,704`](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/session/prompt.ts#L158)；B 同路径 `1052,1081,1092,1103,1178,1257,1272,1343`；[C `packages/core/src/session/runner/llm.ts:173,241,392`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/runner/llm.ts#L173)。

## 3. 从 A 的 loop 映射到 B/C

### A 的最小可读路径

```text
SessionPrompt.prompt (prompt.ts:158)
  createUserMessage
  noReply? -> 返回
  loop (274)
    start/resume AbortController，若已有 owner 就加入 callbacks
    while true
      读历史、找 lastUser/lastAssistant/tasks
      finish 条件 -> break
      subtask -> TaskTool.execute -> continue
      compaction task -> compaction.process -> continue
      token overflow -> compaction.create -> continue
      准备 agent、tools、assistant message、system
      SessionProcessor.create(...).process (processor.ts:45)
        LLM.stream (llm.ts:46)
          streamText (172)
        for await fullStream -> 保存 text/reasoning/tool/usage
        retryable? -> sleep -> 再尝试
        return continue / compact / stop
    prune，回传最后 assistant，resolve callbacks
```

证据：A `prompt.ts:238` start、`:249` resume、`:256` cancel、`:277` 已在执行时加入 callbacks、`:285` defer cancel；`:350` subtask、`:528` compaction、`:542` overflow、`:567` processor、`:652` system、`:716` prune/resolve。A `processor.ts:45` 内循环消费 stream、`:353` catch、`:401` 工具异常清理、`:420` 结果分支。

### B 保留原理，改变生命周期写法

```text
SessionPrompt.prompt (1052)
  loop (1343)
    SessionRunState.ensureRunning
      runLoop (1081)
        老的“读历史 -> 任务/压缩分支 -> tools -> process”主线
        LLM.Service.stream -> Stream<LLMEvent>
        Scope/fiber 中断，ensuring/onInterrupt 做清理
```

最值得对照的行为修补：

1. A 的退出依据用 `lastUser.id < lastAssistant.id`，B 改为 parentID 精确对应 lastUser（B `prompt.ts:1115`）。
2. 某些 provider 返回 stop 却带本地工具调用；B 通过 `hasToolCalls` 强制继续，排除 orphaned-interrupted 工具（B `prompt.ts:1103`）。
3. B 出现 content-filter finish 时写显式错误并发布事件，不再静默 idle（B `prompt.ts:1295`）。
4. assistant 创建后、processor 创建或执行时的中断会补全 assistant 的 aborted error 与 completed 时间（B `prompt.ts:1203,1219,1332`）。
5. `SessionRunState` 是 instance-scoped runners map，交给 `Runner` 管同一 Session 的 join/cancel；这已经超出 A 的手写 callbacks（B `run-state.ts:35,52,88`）。

### C 的 loop 是 turn orchestration

```text
SessionExecution -> SessionRunner.run({ sessionID, force })
  检查 pending steer/queue；没有输入且非 force 则不调用 provider
  failInterruptedTools：旧 pending/running 工具写失败，绝不盲目重放副作用
  while 有下一工作项
    while 需要 continuation
      runTurn
        检查 Session 当前 Location 仍匹配
        初始化完整 system epoch
        promote steers；queue 只在合适边界取一项
        reconcile epoch、重读模型与投影历史
        可选自动 compaction -> 重建请求
        capture snapshot
        llm.stream(request)（只有一次 provider turn）
          publish 每个 LLMEvent
          本地 tool-call durable publish 后立即启动 tool fiber
        provider stream 关闭后 await 全部工具结算
        end snapshot -> durable Step.Ended
        根据本地工具/新 steer 决定 continuation
    检查 FIFO queue，取下一条
```

证据：C `runner/llm.ts:119` orphan tool repair、`:179` Location fence、`:183` epoch init、`:187` promote、`:195` reset steps、`:197` epoch prepare、`:199` model/history reload、`:224` automatic compaction、`:226` snapshot、`:241` llm.stream、`:251` publish、`:280` FiberSet.run、`:305` await、`:325` step settlement、`:354` return continuation、`:392` main loop。

“每个 provider turn 一次 llm.stream”只描述常规 provider turn；触发压缩时 compaction 自己另外发起无工具的摘要请求。不要误写成整个 Session 生命周期只有一次 LLM 调用。

## 4. SDK 职责的变化

A `LLM.stream` 会拼 agent/provider/user system，构造 tools 和 callbacks，然后 `streamText`。AI SDK 处理本轮 provider stream 与 execute callback；外层多轮仍归 OpenCode loop（A `llm.ts:67,150,172,224`）。

B `LLM.Service.stream` 统一成 Effect `Stream<LLMEvent>`。默认 `streamText` 的 fullStream 通过 `LLMAISDK.toLLMEvents` 转换；native 模式先检查 provider 支持条件，不支持就回落 AI SDK。它的出现是传输与 provider SDK seam，而不是 V2 Session 的开关（B `llm.ts:225,276,354`；`llm/native-runtime.ts:46,74,103`）。native adapter 的本地工具也用 FiberSet，provider stream 后 drain results queue（该文件 `:106,121,131`），仍由 V1 prompt/processor 驱动下一轮。

C 的 runner 使用统一的 `LLM.request` 与 `LLMClient`，只让它消费一个 provider turn。工具 catalog 在准备请求时取得，同一个 materialization 对象保留执行 handler；runner 自己接到完整 call 就 settle，SDK 不接管下一轮 agent loop（C `runner/llm.ts:203,205,241,261,392`）。

## 5. 重试、终止、取消：别混成一件事

- 正常结束：模型没有需继续的本地工具且没有新输入；A/B 的 stop/unknown/tool-calls 历史条件与 C 的 needsContinuation 不是同一实现。
- 工具报错：通常作为工具结果让模型读到，和 provider 失败不是同一个错误通道。用户拒绝权限/问题则可让 loop 停下；C 显式识别 DeclinedError/RejectedError 并中断（C `runner/llm.ts:144,306`）。
- provider 可重试错误：A 会继续其内层 processor 重试；B 最多 5 retries；C 底层 RequestExecutor 有 2 次 HTTP 请求重试，但 Session runner 没有 V1 式全轮重试与 retry 状态策略。
- 用户中断：停止运行中的执行链，清理工具和 assistant；不等于删除 Session 历史。
- 进程崩溃：C 开始新 drain 时把历史残留 running/pending tools 标为 interrupted，不假装工具仍活着，也不静默重放 shell/文件写入。

B Retry policy 详细证据：`retry.ts:26` initial 2s、`:28` jitter、`:31` max 5、`:47` header delay、`:85` retryability、`:183` schedule。A 重试证据：`processor.ts:366-377` 没有最大次数检查，`retry.ts:28` delay。C runner 未采用 V1 式整轮重试的源码证据：`runTurnAttempt` 在 `:288` inspect exit、`:299` 落 assistant error、`:351` 将失败向上透出；主 loop `:405` 未包 V1 Retry policy。overflow 有独立处理，不能遗漏。

## 6. compaction 的进化

A/B：主业务 loop 有显式 compaction task 分支和 tokens-overflow 检查；压缩使用 compaction agent 和 processor，结果记成 summary assistant，历史筛选会跳过已压缩部分；自动压缩还可注入 synthetic user 继续提示。A 另有 prune，标记旧 tool output 的 compacted 时间，而不等于对话摘要。证据：A `compaction.ts:32` isOverflow、`:58` prune、`:101` process、`:131` compaction agent、`:202` summary process、`:235` replay/synthetic；B 主循环 `prompt.ts:1149,1161,1320,1338`。

C：`SessionCompaction.make` 估算即将发送的完整 request（含 system/messages/tools），近上限时将历史序列化成 head+保留 recent；摘要请求不带 tools，写 Compaction.Started/Ended；下一 turn history 读取从最新 compaction 开始，lower 成 `<conversation-checkpoint>`，并在完整上下文观察可用时重建 system baseline。某次 provider turn 在尚未形成 assistant 的阶段遇到 context overflow，还能 forced compaction 后重试一次路径；第二次 overflow 不能再无限递归该 overflow-recovery 路径。证据：C `compaction.ts:137` select、`:176` make、`:191` Started、`:201` no-tools LLM、`:222` Ended、`:232` threshold；`history.ts:24` seq query、`runner/to-llm-message.ts:147` checkpoint；`runner/llm.ts:291,364,378` overflow recovery。

**源码注释过期陷阱：** C `runner/llm.ts:43-90` 的 checklist 仍将 compaction、snapshots、cleanup 若干项列为 unchecked，但同文件下方已有 `compactIfNeeded`、`compactAfterOverflow`、snapshots.capture 与 Step.Ended，不能按这份注释断言都未实现。真实实现优先。

## 7. system context 从“重新读文件”到“可解释历史”

A 每轮都调用 environment + instructions，随后 LLM 层再合并 agent/provider/user system；B 还有 skills 和 MCP instructions。这样读源码容易理解，但外部文件后来改变时，系统没有 C 那种被持久化的 epoch diff 边界。

C 的 epoch 保存 baseline 文本、baseline_seq 与结构化来源 snapshot。第一轮必须完整初始化，否则输入仍留 pending；后续独立观察来源，发生变化就 `ContextUpdated` system 消息与 snapshot advance 原子提交；baseline 不因日常变化而改写，便于缓存和重放。完成压缩后，完整观察可用时替换 baseline，暂不可用则保留旧值；Location 移动则清空 epoch，在目的地重新初始化。agent/model 每 turn 重读；agent 专属 skill guidance 会参与来源组合。

证据：C `context-epoch.ts:23` initialize、`:40` prepareOnce、`:59` compaction replacement decision、`:72` ContextUpdated + commit callback、`:111` reset、`:122` baseline+snapshot insert；runner `:168` Registry+skills+references combine、`:182` agent select、`:183` first init、`:197` prepare。历史在 `history.ts:35-49` 按 durable seq 与 baseline 过滤，不能依赖用户自定义 ID 或 timestamp 排序。

模型切换并非所有 provider metadata 都能继续复用：C `to-llm-message.ts:70` 检查原 assistant 模型和当前模型相同，错误 assistant 不重用 provider metadata；跨模型 reasoning 降为普通 text，provider 内部延续字段被省略。hosted tool 的 call metadata 与 resultMetadata 分别还原（`:88,90,104`）。这解释了为什么 V2 的 durable history 不能简单 JSON 原样传回任何模型。

## 8. 建议跟读/断点与已存在测试

按 A `prompt -> processor -> llm -> compaction` 读懂一个小完整循环后，再看 B `prompt.runLoop -> run-state -> processor -> llm`，最后看 C `runner/llm -> publish-llm-event -> history -> context-epoch`。

可手动画四种轨迹：

1. 用户提问 -> 一个 text 回答 -> 无工具结束。
2. 模型连续发两个本地 tool-call -> 两个结果 -> 下一 provider turn。
3. 执行期间收到新 steer 和 queue -> steer 边界进入；queue 等当前 continuation 结束。
4. shell 已有副作用但进程崩溃 -> 新 drain 标失败；不自动重演 shell。

已有测试作为阅读材料，不宣称本轮执行：[C `packages/core/test/session-runner.test.ts:658`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/test/session-runner.test.ts#L658) 初始 system 不可用后再试、`:741` baseline 稳定、`:852` agent switch 更新 skill guidance、`:941` 来源删除形成 system message、`:1007` unavailable 保留 baseline、`:1039` compaction 后重建 baseline、`:1214` overflow recovery。其他 runner tool/recorded tests 有实际多 turn fixture。

## 9. V2 API 可用性与 runner 能力必须分开

公开 `SessionV2.compact(input)` 当前只检查 Session 存在，然后返回 `OperationUnavailableError({ operation: "compact" })`（[C `packages/core/src/session.ts:417-419`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session.ts#L417)）。但 runner 在 `runner/llm.ts:224` 已调用自动压缩，且 `:291-297` 有 context-overflow recovery。正确结论：**内部自动压缩与 overflow recovery 已实现，公开手动 compact 操作尚不可用**。不能把“公开 API 不可用”推导成“V2 没有压缩”，也不能把“自动压缩已实现”推导成“客户端可调用手动 compact”。同文件 `shell/skill/wait` 的 unavailable 也不应泛化成不存在 bash 工具或不能通过 resume 等待执行。


## 10. 重试还有一层：HTTP 请求重试 ≠ Session turn 重试

进一步沿 LLMClient 进入传输层：[C `packages/llm/src/route/executor.ts:36`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/llm/src/route/executor.ts#L36) 的 `MAX_RETRIES = 2`，`:345` 支持 Retry-After 与指数抖动，`:353` retryStatusFailures，`:378` executeOnce 外包该重试。它处理 HTTP execute/status 失败，不能恢复已经消费一半的 provider stream，也不等于 V1 processor 的“重新完整调用 LLM.stream 并写 retry Session 状态”。所以不要笼统说 V2 完全没有重试；应分为 **HTTP 请求重试已有，Session 编排层通用 provider turn 重试/持久 retry 状态未做，context-overflow-compaction 专门恢复已做**。

A/B 另有 repeated-tool doom-loop 提示权限：A `processor.ts:151-175`、B `processor.ts:356-373` 对近期 3 个相同工具与相同输入请求 `doom_loop`；这也不是 provider retry。C runner 顶部 TODO 中 repeated identical tool calls 尚未实现，实际调用分支也没有对应历史重复检测。


## 固定提交源码索引

正文中的短文件名和 `:行号` 延续所在段落的版本与文件。下表按完整路径提供两个提交的入口；“—”表示该路径在该提交中不存在，不能用学习分支中的同名文件代替新版源码。

| 文件 | A · v1.2.24 | B · dev（含最新 V1/V2） |
| --- | --- | --- |
| `packages/core/src/session.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session.ts#L1) |
| `packages/core/src/session/compaction.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/compaction.ts#L1) |
| `packages/core/src/session/context-epoch.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/context-epoch.ts#L1) |
| `packages/core/src/session/history.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/history.ts#L1) |
| `packages/core/src/session/runner/llm.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/runner/llm.ts#L1) |
| `packages/core/src/session/runner/publish-llm-event.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/runner/publish-llm-event.ts#L1) |
| `packages/core/src/session/runner/to-llm-message.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/runner/to-llm-message.ts#L1) |
| `packages/core/test/session-runner.test.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/test/session-runner.test.ts#L1) |
| `packages/llm/src/route/executor.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/llm/src/route/executor.ts#L1) |
| `packages/opencode/src/session/compaction.ts` | [源码](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/session/compaction.ts#L1) | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/compaction.ts#L1) |
| `packages/opencode/src/session/llm.ts` | [源码](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/session/llm.ts#L1) | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/llm.ts#L1) |
| `packages/opencode/src/session/llm/native-runtime.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/llm/native-runtime.ts#L1) |
| `packages/opencode/src/session/processor.ts` | [源码](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/session/processor.ts#L1) | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/processor.ts#L1) |
| `packages/opencode/src/session/prompt.ts` | [源码](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/session/prompt.ts#L1) | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/prompt.ts#L1) |
| `packages/opencode/src/session/retry.ts` | [源码](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/session/retry.ts#L1) | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/retry.ts#L1) |
| `packages/opencode/src/session/run-state.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/run-state.ts#L1) |
