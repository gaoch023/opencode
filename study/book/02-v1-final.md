# 02 · 最终 V1：把一次任务从输入追到模型、工具与后台子会话

本章独立讲解提交 `055d95bb7e278c94baf06235a52cac79dd13ba67` 的 V1 实现，包版本为 `1.18.35`。这里的“最终 V1”指本书固定的 V2 推广前快照，不推断它是今天上游的最新版本。本章所有源码片段和行号都属于这个提交。你可以保留 v1.2.24 在工作区，通过固定提交链接或 `git show` 阅读新版；不需要为了理解架构重新安装依赖。

我们要走通一个具体任务：“读登录实现，让 explore 找出调用入口，运行相关测试，最后总结。”以下 ID 为便于阅读而缩写：父会话 `S-main`，真实用户消息 `U-01`，第一轮 assistant `A-01`，read 调用 `call-read-01`，task 调用 `call-task-01`，子会话 `S-explore`，shell 调用 `call-test-01`。实际运行会生成符合项目 ID schema 的值；这些名字不是可以直接提交 API 的样例参数。

## 1. 先把对象摆正：有七种“正在做事”

Agent 是配置身份，Session 是会话身份，assistant message 是某次模型回答的记录，tool part 是该回答中一次工具调用的记录，Runner 是同一会话当前执行的协调器，Fiber 是可等待和取消的 Effect 执行，OS process 是 shell 等工具实际启动的系统进程。它们可以关联，但没有一一对应关系。一个 explore 子 agent 通常意味着新的 child Session 和同进程里的执行链，不意味着创建新容器、独立代码副本或新 OS 进程。

`S-main` 可以有上百条 message，但空闲时没有活动 Runner；同一 Worker 里的多个 Session 可以各有活动 Fiber；一个没有调用 shell 的子会话可能从未创建命令子进程。反过来，单个 `bash` 调用可以启动测试运行器及其后代进程。源码中的 `parentID` 至少有两种用途：Session.parentID 表示父子会话，assistant.parentID 指向它回答的用户消息。调试时若把两者混淆，很快就会把工具归属或取消链画错。

本章的主线可以先记成：

```text
用户 U-01 -> SessionPrompt.prompt -> 保存用户消息
  -> SessionRunState -> Runner.ensureRunning
  -> runLoop 重读历史 -> 选 agent/model -> 构造 assistant A-01
  -> SessionTools 包装 execute -> SessionProcessor.process
  -> LLM.stream -> AI SDK 或 native adapter
  -> 工具实际执行 -> Processor 将结果写回 message/part
  -> runLoop 再读历史 -> 下一轮或结束
```

这是一条业务编排链。下面的 Layer、Fiber 和共享事件设施让这条链有明确的依赖、资源所有者和取消行为，但没有把“何时继续问模型”的决定交给某个隐藏后台调度系统。

## 2. 不先学完整 Effect，也能读懂这里的代码

`Effect.Effect<A, E, R>` 可以理解成“成功产生 A、可能以 E 失败、运行前还需要 R 服务的计算描述”。拿到它还不代表执行过它。`Effect.gen(function*(){...})` 让代码按顺序描述这些计算；`yield* sessions.get(...)` 才把前一步结果接进当前执行。`yield* Agent.Service` 则从当前 Effect context 取得服务，类似显式依赖容器查询，不是在创建一个 agent 任务。

`Context.Service` 定义服务的身份和接口，`Layer.effect` 提供服务的构造方式，`LayerNode` 声明它依赖哪些节点。它们解决“谁向谁提供 config、database、permission”等问题。服务可复用不等于服务内部所有状态都全局共享：V1 广泛使用 `InstanceState`，在服务内部再按当前 directory 缓存状态。`InstanceRef` 给出当前项目目录、worktree 等上下文，因此同一个服务接口能服务多个已打开的项目而不把运行映射混在一起。

Fiber 才是已经运行起来的计算。`forkIn(scope)` 启动执行并让指定 scope 拥有它；`forkScoped` 把执行绑定到当前资源范围。Scope 是清理边界，不是操作系统沙箱：关闭 scope 可以中断它拥有的 Fiber、关闭句柄、运行 finalizer，却不能把已经写入文件或提交到外部服务的操作撤销。`Deferred` 是一次性结果信号：多个等待者可以等同一个结果，完成者只需结算一次。它取代手写 Promise callbacks，让“同一 Session 已经在跑，新请求该等谁”变成可组合的控制流。

最后，EffectBridge 用来穿过 AI SDK 或事件监听器的普通 JavaScript 回调边界。外部库期待 `execute(): Promise`，项目内部工具返回 Effect；bridge 捕获必要运行上下文，再把 Effect 变成 Promise。这样不会因为跳进 SDK 回调就丢掉 Instance、服务依赖和资源作用域。`Effect.orDie` 也值得辨认：它把类型化错误转为 defect，不是忽略失败；上层仍可通过 Cause 观察和清理它。

## 3. Agent 保存能力配置，Session 保存具体对话

先读 Agent.Info。它没有 pid、线程号或执行队列；有 name、mode、permission、model、prompt、options、steps。这就说明 build、explore、compaction 的区别首先是“模型收到什么指令、允许使用什么能力”，而不是三个常驻进程。

<!-- EXCERPT 055d95bb7e278c94baf06235a52cac79dd13ba67 packages/opencode/src/agent/agent.ts 35 55 -->
**真实源码 · B · V1 1.18.35** — [`packages/opencode/src/agent/agent.ts`，L35–L55](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/agent/agent.ts#L35-L55)

```typescript
export const Info = Schema.Struct({
  name: Schema.String,
  description: Schema.optional(Schema.String),
  mode: Schema.Literals(["subagent", "primary", "all"]),
  native: Schema.optional(Schema.Boolean),
  hidden: Schema.optional(Schema.Boolean),
  topP: Schema.optional(Schema.Finite),
  temperature: Schema.optional(Schema.Finite),
  color: Schema.optional(Schema.String),
  permission: PermissionV1.Ruleset,
  model: Schema.optional(
    Schema.Struct({
      modelID: ModelV2.ID,
      providerID: ProviderV2.ID,
    }),
  ),
  variant: Schema.optional(Schema.String),
  prompt: Schema.optional(Schema.String),
  options: Schema.Record(Schema.String, Schema.Unknown),
  steps: Schema.optional(Schema.Finite),
}).annotate({ identifier: "Agent" })
```

出处：[agent/agent.ts:35](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/agent/agent.ts#L35)。`mode` 区分 primary/subagent/all；`steps` 是 agent 编排的步数额度，不是 shell 超时或总 token 预算。Agent 服务把内置定义、用户配置等组合起来，再按名称查询。内置 explore 允许 grep、glob、read、bash 等搜索能力，默认通用拒绝规则再叠加允许项与用户配置；不能因为名字叫 explore 就推导出强制只读沙箱，尤其 bash 仍有宿主进程权限。

Session 元数据记录目录、项目、parentID、agent/model、标题和权限等；V1 的对话仍主要由 `message` 加 `part` 表表达。`U-01` 可以有 text、file、agent 等 parts；`A-01` 可以有 text、reasoning、tool、step、patch 等 parts。工具调用不只是打印一条日志，而是带 `callID`、输入、状态、输出和时间的可查询记录。会话是持久对象；当前 busy、运行 Fiber、等待 Deferred 是运行时对象，两者不能用一张“会话状态”图包办。

## 4. prompt：先保存输入，再决定是否进入执行

用户发来 `U-01` 后，`prompt` 先取得 `S-main`，清理待处理 revert，再展开并保存用户消息，更新时间，处理旧版 tools 参数的兼容权限，最后才进入 loop。

<!-- EXCERPT 055d95bb7e278c94baf06235a52cac79dd13ba67 packages/opencode/src/session/prompt.ts 1052 1071 -->
**真实源码 · B · V1 1.18.35** — [`packages/opencode/src/session/prompt.ts`，L1052–L1071](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/session/prompt.ts#L1052-L1071)

```typescript
    const prompt: (input: PromptInput) => Effect.Effect<SessionV1.WithParts, Image.Error> = Effect.fn(
      "SessionPrompt.prompt",
    )(function* (input: PromptInput) {
      const session = yield* sessions.get(input.sessionID).pipe(Effect.orDie)
      yield* revert.cleanup(session)
      const message = yield* createUserMessage(input)
      yield* sessions.touch(input.sessionID)

      const permissions: PermissionV1.Rule[] = []
      for (const [t, enabled] of Object.entries(input.tools ?? {})) {
        permissions.push({ permission: t, action: enabled ? "allow" : "deny", pattern: "*" })
      }
      if (permissions.length > 0) {
        session.permission = permissions
        yield* sessions.setPermission({ sessionID: session.id, permission: permissions })
      }

      if (input.noReply === true) return message
      return yield* loop({ sessionID: input.sessionID })
    })
```

出处：[session/prompt.ts:1052](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/session/prompt.ts#L1052)。注意 `noReply` 检查在 `createUserMessage` 之后：它表示保存这条消息但不主动开启回复，不表示消息留在另一个不可见输入箱。正在运行的 loop 下一轮重读历史时仍可能看见它。这里没有 V2 那套显式 steer/queue admission 契约。

`createUserMessage` 也不是只把一个字符串塞进数组。它处理模型和 agent 选择、文件和引用展开、消息 parts，最后 `updateMessage(info)`，逐个 `updatePart(part)`。所以调试“为什么模型看到了某段文本”，应查看真正保存的 user parts 和转换后的模型消息，不能只盯 HTTP 请求体。一个用户请求可以生成额外 synthetic 内容；文件内容和提示词展开都影响实际上下文。

HTTP 的 `promptAsync` 只是 fork 同一个 V1 prompt service 后尽早返回响应。它不会变成后台 task 参数，也不会自动启动另一个 OS 进程。要区分请求方不等回复、模型工具不等子任务、会话本身继续运行这三个不同层次。

## 5. RunState 与 Runner：同一个 Session 只有一个前台执行所有者

进入 loop 不是直接无条件执行 while。`loop` 会把 `runLoop(S-main)` 交给 `SessionRunState.ensureRunning`。RunState 为当前 Instance 保存 runners Map，按 SessionID 找 Runner；这个 Map 的 scope 关闭时取消所有已登记执行。

<!-- EXCERPT 055d95bb7e278c94baf06235a52cac79dd13ba67 packages/opencode/src/session/run-state.ts 35 59 -->
**真实源码 · B · V1 1.18.35** — [`packages/opencode/src/session/run-state.ts`，L35–L59](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/session/run-state.ts#L35-L59)

```typescript
    const state = yield* InstanceState.make(
      Effect.fn("SessionRunState.state")(function* () {
        const scope = yield* Scope.Scope
        const runners = new Map<SessionID, Runner.Runner<SessionV1.WithParts>>()
        yield* Effect.addFinalizer(
          Effect.fnUntraced(function* () {
            yield* Effect.forEach(runners.values(), (runner) => runner.cancel, {
              concurrency: "unbounded",
              discard: true,
            })
            runners.clear()
          }),
        )
        return { runners, scope }
      }),
    )

    const runner = Effect.fn("SessionRunState.runner")(function* (
      sessionID: SessionID,
      onInterrupt: Effect.Effect<SessionV1.WithParts>,
    ) {
      const data = yield* InstanceState.get(state)
      const existing = data.runners.get(sessionID)
      if (existing) return existing
      const next = Runner.make<SessionV1.WithParts>(data.scope, {
```

出处：[session/run-state.ts:35](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/session/run-state.ts#L35)。Map 中有 `S-main` 才说明这里拥有这个会话当前的 Runner；数据库里有 `S-main` 不说明它正在运行。finalizer 并发取消各 Runner，目的在于回收本 Instance 的资源，不是让不同会话共用一个全局互斥锁。

Runner 有四个显式状态，先把它们读成业务语义：

| 状态 | 正在进行的事 | 新 prompt 对应 ensureRunning 的行为 |
| --- | --- | --- |
| Idle | 没有占用前台执行 | 创建 Deferred，启动 run Fiber |
| Running | runLoop 正在推进 | 等现有 run.done，不启动第二个 loop |
| Shell | 用户直接 shell 路径占用会话 | 记录一个待执行 run，进入 ShellThenRun |
| ShellThenRun | shell 在执行，后面已排一个 run | 加入同一个待执行 run.done |

<!-- EXCERPT 055d95bb7e278c94baf06235a52cac79dd13ba67 packages/opencode/src/effect/runner.ts 115 138 -->
**真实源码 · B · V1 1.18.35** — [`packages/opencode/src/effect/runner.ts`，L115–L138](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/effect/runner.ts#L115-L138)

```typescript
  const ensureRunning = (work: Effect.Effect<A, E>) =>
    SynchronizedRef.modifyEffect(
      ref,
      Effect.fnUntraced(function* (st) {
        switch (st._tag) {
          case "Running":
          case "ShellThenRun":
            return [awaitDone(st.run.done), st] as const
          case "Shell": {
            const run = {
              id: next(),
              done: yield* Deferred.make<A, E | Cancelled>(),
              work,
            } satisfies PendingHandle<A, E>
            return [awaitDone(run.done), { _tag: "ShellThenRun", shell: st.shell, run }] as const
          }
          case "Idle": {
            const done = yield* Deferred.make<A, E | Cancelled>()
            const run = yield* startRun(work, done)
            return [awaitDone(done), { _tag: "Running", run }] as const
          }
        }
      }),
    ).pipe(Effect.flatten)
```

出处：[effect/runner.ts:115](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/effect/runner.ts#L115)。`SynchronizedRef.modifyEffect` 把“读状态、选分支、更新状态”作为受协调的操作，避免两个请求都看见 Idle 各起一个 run。返回的第一项是接下来要等待的 Effect，第二项是新状态。外面的 flatten 才执行所选等待。`Deferred` 不保存另一份用户消息，它只保存同一次执行的完成结果。

这里的 Shell 状态专指 `SessionPrompt.shell` 这条用户直接命令入口，不是每次模型调用 bash 都把 Runner 从 Running 改成 Shell。模型 bash 是当前 run 内的一个工具；把两者等同会错误预测“模型调用 bash 时，prompt 是否排在 shell 后面”。直接 shell 只能在 Idle 启动；shell 结束时 `finishShell` 会启动之前排好的 run，见 runner.ts:93–106、140–169。

假设 `S-main` 的 `U-01` 正在 Running，用户又发 `U-02`：“只运行登录相关测试。”`prompt` 会先保存 U-02，然后第二个调用加入现有 run.done。原 loop 下一轮会从持久历史选出最新 user。它并没有把两条消息分别排成两个独立执行单元，也没有专门保证“等 U-01 全部回答完才处理 U-02”。这正是学习旧版 callbacks 后，理解新状态机仍须保留的语义边界。

还要留意等待者与所有者的区别。真正的 run Fiber 由 Instance 的 scope 持有；某个请求只是等待它的 Deferred。一个调用者不再等结果，并不自动取得中断所有共享执行的权利；会话取消必须沿 RunState 的 cancel 路径明确发起。反过来，Instance 关闭可以取消它拥有的执行，即使等待者还想继续等。读到“请求返回”“连接断开”“用户按停止”时，应分别追踪哪一条控制路径实际发生，避免把它们都画成同一个 abort 箭头。

## 6. runLoop：每一步都从历史重新建立判断

runLoop 起步取得当前 Instance context、保存本轮局部变量 `structured` 与 `step`，然后反复设置 busy、读取压缩后的历史、提取最新 user/assistant/finished 和待处理 tasks。不要把这个 while 理解成把模型所有 token 一次次拉出来；token 消费在 Processor 里面。它的一次迭代负责准备并处理一个正常模型步骤，或优先处理 subtask/compaction。

<!-- EXCERPT 055d95bb7e278c94baf06235a52cac79dd13ba67 packages/opencode/src/session/prompt.ts 1096 1116 -->
**真实源码 · B · V1 1.18.35** — [`packages/opencode/src/session/prompt.ts`，L1096–L1116](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/session/prompt.ts#L1096-L1116)

```typescript
          const { user: lastUser, assistant: lastAssistant, finished: lastFinished, tasks } = MessageV2.latest(msgs)

          if (!lastUser) throw new Error("No user message found in stream. This should never happen.")

          const lastAssistantMsg = msgs.findLast(
            (msg) => msg.info.role === "assistant" && msg.info.id === lastAssistant?.id,
          )
          // Some providers return "stop" even when the assistant message contains
          // tool calls. Keep the loop running so tool results can be sent back to
          // the model, but ignore cleanup-marked interrupted orphans.
          const hasToolCalls =
            lastAssistantMsg?.parts.some(
              (part) => part.type === "tool" && !part.metadata?.providerExecuted && !isOrphanedInterruptedTool(part),
            ) ?? false

          if (
            lastAssistant?.finish &&
            !["tool-calls", "unknown"].includes(lastAssistant.finish) &&
            !hasToolCalls &&
            lastAssistant.parentID === lastUser.id
          ) {
```

出处：[session/prompt.ts:1096](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/session/prompt.ts#L1096)。几个变量回答不同问题：`lastUser` 决定当前要回应谁和使用的 agent/model；`lastAssistant` 表示最近模型步骤；`lastFinished` 用于已完成步骤及溢出判断；`tasks` 可以让显式子任务或压缩先被处理。退出不只看 provider 的 finishReason：有些 provider 明明发了本地工具调用却返回 stop，因此代码还检查真实 tool parts；被 cleanup 标记 interrupted 的孤儿工具不能触发无限 assistant-prefill。

没有待处理 subtask/compaction 时，loop 解析 agent、计算 steps、加提醒、先保存新的 assistant `A-01`，再建立 Processor handle。随后解析本轮工具，收集 environment、instructions、skills、MCP instructions，把历史 lower 成 provider 消息，最后调用 `handle.process`。标题生成、摘要统计等辅助工作可 fork 到拥有它们的 scope；它们不是本轮工具结果，也不应被算成“主 loop 多启动了一个父回答”。

正常步骤完成后，Processor 返回 continue/compact/stop；loop 根据结构化输出、错误、content-filter 等结果决定 break 或续步。达到配置步数时，代码在 messages 后面加入 `MAX_STEPS_PROMPT` 提示模型收尾，但同一次 `handle.process` 仍传入完整 tools（prompt.ts:1279–1285）。因此 `steps` 不是“达到上限就禁止后续工具”的硬限制，更不是操作系统级调用次数限制。理解谁设置 step、何时重置、哪条路径绕过普通步骤，比只找到一个 maxSteps 常量更有价值。

## 7. 工具为何会执行：execute 回调、上下文与结算分别归谁

先看 `SessionTools.resolve`。registry 给出模型可见的工具定义，按 provider 转换 schema，再用 AI SDK `tool(...)` 包装；每个 execute 回调进入 EffectBridge，再执行真实工具。

<!-- EXCERPT 055d95bb7e278c94baf06235a52cac79dd13ba67 packages/opencode/src/session/tools.ts 99 128 -->
**真实源码 · B · V1 1.18.35** — [`packages/opencode/src/session/tools.ts`，L99–L128](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/session/tools.ts#L99-L128)

```typescript
    tools[item.id] = tool({
      description: item.description,
      inputSchema: jsonSchema(schema),
      execute(args, options) {
        return run.promise(
          Effect.gen(function* () {
            const ctx = context(args, options)
            yield* plugin.trigger(
              "tool.execute.before",
              { tool: item.id, sessionID: ctx.sessionID, callID: ctx.callID },
              { args },
            )
            const result = yield* item.execute(args, ctx)
            const output = {
              ...result,
              attachments: result.attachments?.map((attachment) => ({
                ...attachment,
                id: PartID.ascending(),
                sessionID: ctx.sessionID,
                messageID: input.processor.message.id,
              })),
            }
            yield* plugin.trigger(
              "tool.execute.after",
              { tool: item.id, sessionID: ctx.sessionID, callID: ctx.callID, args },
              output,
            )
            if (options.abortSignal?.aborted) {
              yield* input.processor.completeToolCall(options.toolCallId, output)
            }
```

出处：[session/tools.ts:99](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/session/tools.ts#L99)。工具拿到的 ctx 包含 `S-main`、`A-01`、`call-read-01`、AbortSignal、agent、历史和 metadata/ask 回调。它不是模型自由构造的一组可信权限；权限请求由宿主将 agent.permission 与 session.permission 合并，并带上 messageID/callID 关联。工具目录过滤“是否向模型展示”，执行阶段 ask“这次具体资源/动作是否批准”，两者都要看。

在默认 SDK 路径中，SDK 根据完整 tool call 调用 execute；Processor 消费的 `tool-call` 事件主要把 part 推到 running，而不是在 switch 分支里再次执行一次 `read`。否则很容易写出重复副作用。before/after 插件钩子包住真实 execute；metadata 可以在工具尚未完成时持续更新 UI；输出附加文件带父会话和 assistant 身份。正常返回后，LLM 事件把结果带回 Processor 结算；但取消存在竞态：若 execute 已经拿到 output、这时 abortSignal 已标记 aborted，tools.ts:126–128 会直接调用 `processor.completeToolCall`，以免流已关闭而丢掉实际完成的结果。它是取消边界的补偿结算，不是又执行了一遍工具，也不是保证所有被取消的操作都完成。

Processor 的 `ctx.toolcalls` 以 provider call ID 找本次消息中的 part 和 done Deferred；文本、推理、snapshot 和 compaction 标记是同一个 ProcessorContext 的其他维度。一个 `tool-input-start` 可先创建 pending part；完整 `tool-call` 转 running；成功结果写 completed；异常写 error。下面这段看的是成功结算，不是工具调用的发起：

<!-- EXCERPT 055d95bb7e278c94baf06235a52cac79dd13ba67 packages/opencode/src/session/processor.ts 160 184 -->
**真实源码 · B · V1 1.18.35** — [`packages/opencode/src/session/processor.ts`，L160–L184](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/session/processor.ts#L160-L184)

```typescript
      const completeToolCall = Effect.fn("SessionProcessor.completeToolCall")(function* (
        toolCallID: string,
        output: {
          title: string
          metadata: Record<string, any>
          output: string
          attachments?: SessionV1.FilePart[]
        },
      ) {
        const match = yield* readToolCall(toolCallID)
        if (!match || match.part.state.status !== "running") return
        yield* session.updatePart({
          ...match.part,
          state: {
            status: "completed",
            input: match.part.state.input,
            output: output.output,
            metadata: output.metadata,
            title: output.title,
            time: { start: match.part.state.time.start, end: Date.now() },
            attachments: output.attachments,
          },
        })
        yield* settleToolCall(toolCallID)
      })
```

出处：[session/processor.ts:160](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/session/processor.ts#L160)。`input` 保留模型调用参数；`output/metadata/title/attachments` 是工具结果；start/end 用于观察生命周期；`settleToolCall` 从活动映射删除并结算 done。这样“持久记录已更新”和“等待清理的人可以继续”都有明确发生点。工具失败会保留进度 metadata；权限或问题被拒绝还会影响 blocked/shouldBreak，不能一律当成普通错误文本让模型无限再试。

Processor 消费流时按 LLMEvent 更新 reasoning/text/tool/usage；cleanup 会补结束时间、记录 patch，并短暂等待尚未结算的 done，再把剩余工具标为 `Tool execution aborted` 且加 interrupted metadata。这里的 250ms 是收尾等待窗，不是每个工具的运行上限；正常 shell 可以跑更久。提前 snapshot 在流开始前捕获，因为 SDK 可能比 start-step 事件更早执行工具。snapshot/patch 支持变更观察，不意味着取消会自动恢复文件。

## 8. LLM：默认 SDK 与 native 是两个后端，不是两个 Session loop

LLM 层先解析 provider language/config/auth，准备消息、系统提示、工具与模型参数。默认分支明确调用 AI SDK `streamText`，传入 tools、abortSignal、headers、messages 等。模型选择、provider 请求和 SDK 工具分发在这里接起来；Processor 不必理解每家 provider 的原始 chunk 结构。

`OPENCODE_EXPERIMENTAL_NATIVE_LLM=true` 仅开启 V1 内的可选 native adapter。它尝试使用 `@opencode-ai/llm`；支持的模型/选项走 native，具体条件不支持时给出原因并回落 SDK。这个开关不把 SessionPrompt 换成 V2 SessionRunner，也不改变 `S-main` 的存储模型。native adapter 自己管理本地工具 Fiber 和结果队列，使它最终也产出相同 LLMEvent 流。

<!-- EXCERPT 055d95bb7e278c94baf06235a52cac79dd13ba67 packages/opencode/src/session/llm.ts 357 378 -->
**真实源码 · B · V1 1.18.35** — [`packages/opencode/src/session/llm.ts`，L357–L378](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/session/llm.ts#L357-L378)

```typescript
    const stream: Interface["stream"] = (input) =>
      Stream.scoped(
        Stream.unwrap(
          Effect.gen(function* () {
            const ctrl = yield* Effect.acquireRelease(
              Effect.sync(() => new AbortController()),
              (ctrl) => Effect.sync(() => ctrl.abort()),
            )

            const result = yield* run({ ...input, abort: ctrl.signal })

            if (result.type === "native") return result.stream

            // Adapter seam: both runtimes expose the same LLMEvent stream. Native
            // already returns one; AI SDK streams are converted here.
            const state = LLMAISDK.adapterState()
            return Stream.fromAsyncIterable(result.result.fullStream, (e) =>
              e instanceof Error ? e : new Error(String(e)),
            ).pipe(
              Stream.mapEffect((event) => LLMAISDK.toLLMEvents(state, event)),
              Stream.flatMap((events) => Stream.fromIterable(events)),
            )
```

出处：[session/llm.ts:357](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/session/llm.ts#L357)。scoped stream 创建 AbortController，并在资源释放时 abort；native 已经是标准流，SDK fullStream 则逐项转换。上层看到同一套 `tool-call/tool-result/text-delta/step-finish`，因此 agent loop 无需为传输后端复制一套。

“同一 assistant 里多个工具”也不等于串行调用。SDK 的 execute 和 native 的工具 Fiber 都可以产生重叠执行；单个 Effect 的 yield 等待不会阻塞操作系统所有线程或所有 Session。反过来，不应声称换成 Fiber 就必然更快：模型耗时、网络、SQLite 写入、工具本身的成本和并发上限共同决定表现。本章没有做这两种后端的性能测量。

## 9. EventV2 已服务 V1：事实、投影、实时通知要分开

最终 V1 的 `Session.updateMessage` 不再自己到处 upsert 表，而是发布 `SessionV1.Event.MessageUpdated`；`updatePart` 同样发布领域事件。核心 SessionProjector 注册 V1 事件投影，把它们写回 V1 的 MessageTable/PartTable。名称中出现 EventV2，不代表一定执行了 V2 模型循环。

<!-- EXCERPT 055d95bb7e278c94baf06235a52cac79dd13ba67 packages/core/src/event.ts 316 349 -->
**真实源码 · B · V1 1.18.35** — [`packages/core/src/event.ts`，L316–L349](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/core/src/event.ts#L316-L349)

```typescript
                          const committed = {
                            ...event,
                            durable: { aggregateID, seq, version: durable.version },
                          } as Payload
                          for (const projector of list) {
                            yield* projector(committed)
                          }
                          if (commit) yield* commit(seq)
                          yield* db
                            .insert(EventSequenceTable)
                            .values([{ aggregate_id: aggregateID, seq, owner_id: input?.ownerID }])
                            .onConflictDoUpdate({
                              target: EventSequenceTable.aggregate_id,
                              set: {
                                seq,
                                ...(input?.ownerID && row?.ownerID == null ? { owner_id: input.ownerID } : {}),
                              },
                            })
                            .run()
                            .pipe(Effect.orDie)
                          yield* db
                            .insert(EventTable)
                            .values([
                              {
                                id: event.id,
                                aggregate_id: aggregateID,
                                seq,
                                type: versionedType(definition.type, durable.version),
                                data: encoded,
                              },
                            ])
                            .run()
                            .pipe(Effect.orDie)
                          return { aggregateID, seq }
```

出处：[core/event.ts:316](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/core/src/event.ts#L316)。外层是 immediate database transaction：先生成该 aggregate 的 seq，执行投影与可选 commit hook，再更新 sequence 并写 EventTable；失败一起回滚。这里说“先投影”是同一个事务内的调用次序，不是让客户端看见尚未写事件的半状态。提交后才唤醒 durable tail 等消费者。

`S-main` 的 MessageUpdated、PartUpdated 等持久事实因而可以同步/回放；GlobalBus 负责把观察消息转给客户端。`PartDelta` 本身明确不是 durable 事件：schema 定义没有 durable 声明（[schema/src/v1/session.ts:632](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/schema/src/v1/session.ts#L632)）。Processor 的 text-delta 分支只把文字累加到内存 `ctx.currentText.text` 并发 PartDelta（processor.ts:513–524），text-end 才调用 updatePart 保存完整文本（526–545）；有序 cleanup 也会保存尚在内存里的部分 text/reasoning（569–583）。因此正常结束或可完成清理的取消，与突然崩溃不同：未 flush 的文字可能已经显示在界面，却没有进入持久 part。客户端重连要依赖已保存状态和重放契约，不能假定每个 token 都会逐字符重发。进程重启保留会话与已提交事件，却不会保存 Runner、Fiber、Deferred 或 SDK 当前流连接。

这也解释“事件持久化能否自动恢复测试工具”的答案。若命令已改文件、结果尚未记录时进程崩溃，数据存储不能证明副作用没发生；重放事件投影是重建读模型，不是再次执行所有 tool-call。没有明确所有权和恢复策略，就不应自动重演未知副作用。

## 10. Task 前半段：授权、深度、child Session 和模型选择

父模型在 `A-01` 调用 task，请 explore 找出登录入口。Task 先读取当前配置，检查后台开关，再沿 Session.parentID 计算深度，最后请求“是否可委派给该 agent”的权限。

<!-- EXCERPT 055d95bb7e278c94baf06235a52cac79dd13ba67 packages/opencode/src/tool/task.ts 98 128 -->
**真实源码 · B · V1 1.18.35** — [`packages/opencode/src/tool/task.ts`，L98–L128](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/tool/task.ts#L98-L128)

```typescript
      if (runInBackground && !flags.experimentalBackgroundSubagents) {
        return yield* Effect.fail(
          new Error("Background subagents require OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true"),
        )
      }

      const parent = yield* sessions.get(ctx.sessionID)
      let current = parent
      let depth = 0
      while (current.parentID) {
        depth++
        current = yield* sessions.get(current.parentID)
      }
      if (depth >= (cfg.subagent_depth ?? 1)) {
        return yield* Effect.fail(
          new Error(
            `Subagent depth limit reached (${cfg.subagent_depth ?? 1}). Increase "subagent_depth" to allow nested subagents.`,
          ),
        )
      }

      if (!ctx.extra?.bypassAgentCheck) {
        yield* ctx.ask({
          permission: id,
          patterns: [params.subagent_type],
          always: ["*"],
          metadata: {
            description: params.description,
            subagent_type: params.subagent_type,
          },
        })
```

出处：[tool/task.ts:98](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/tool/task.ts#L98)。默认 `subagent_depth` 为 1，父 Session 深度 0 可以创建一层 child；child 再创建 grandchild 会遇到深度限制。显式用户 @agent/命令可以通过 bypassAgentCheck 跳过 task 选择询问，但不能因此绕过深度、子工具具体权限或操作系统权限。

权限继承更细：`deriveSubagentSessionPermission` 传下父 **Session** 中的 deny 与 external_directory 规则；父 **Agent** 的专属能力限制不整体复制过去，child 使用自己选中 agent 的规则。随后补 todowrite/task 默认 deny，以及 primary_tools 等限制。这样用户对本会话明确禁止的动作仍约束后代，同时不同 agent 可承担不同职责。权限合并顺序决定覆盖关系；配置检查不是不可绕过的 OS 沙箱。

`task_id` 可用时会查找并续用既有 Session，没找到则创建 `S-explore`，其 parentID=`S-main`。task 返回的 ID 首先就是 child Session ID，不是随机新 Worker ID。子模型若有自己的 model 配置就用它，否则继承发起 task 的 assistant 所用 provider/model，并相应继承 variant。`runTask` 解析 prompt parts，再调用 `ops.prompt({sessionID:S-explore,...})`，所以 child 有自己的历史和完整 V1 loop；它不会自动获得父对话的全部历史文本，父模型必须通过任务 prompt 传递所需背景。

子任务返回时检查 assistant error 和终末 tool error，不能把失败静默变成空文本成功；正常情况下抽取最后 text 作为工具结果。于是父 Session 能把“找到 src/login/handler.ts”记成 task 的工具输出，而 child 里面的 grep/read 过程仍保留在 `S-explore`。父 task 工具和 child loop 是两个相关但不相同的等待层。

## 11. Task 后半段：前后台共用同一 job，区别在等待和结果送达

即使参数里没有 `background:true`，Task 也会调用 BackgroundJob.start。job ID 选 `S-explore`，metadata 带 parentSessionId/sessionId/model，run 才是真正的 `runTask()`；这让前台任务也能被观察和取消。

<!-- EXCERPT 055d95bb7e278c94baf06235a52cac79dd13ba67 packages/opencode/src/tool/task.ts 284 318 -->
**真实源码 · B · V1 1.18.35** — [`packages/opencode/src/tool/task.ts`，L284–L318](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/tool/task.ts#L284-L318)

```typescript
      const info = yield* background.start({
        id: nextSession.id,
        type: id,
        title: params.description,
        metadata,
        onPromote: Effect.all([
          ctx.metadata({
            title: params.description,
            metadata: { ...metadata, background: true, jobId: nextSession.id },
          }),
          notify(nextSession.id),
        ]),
        run: runTask().pipe(Effect.onInterrupt(() => ops.cancel(nextSession.id))),
      })

      function backgroundResult() {
        return {
          title: params.description,
          metadata: {
            ...metadata,
            background: true,
            jobId: info.id,
          },
          output: renderOutput({
            sessionID: nextSession.id,
            state: "running",
            summary: "Background task started",
            text: BACKGROUND_STARTED,
          }),
        }
      }

      if (runInBackground) {
        yield* notify(info.id)
        return backgroundResult()
```

出处：[tool/task.ts:284](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/tool/task.ts#L284)。默认前台继续走下面的 acquireUseRelease，等待 job.done 或 promotion；因此父 task 尚未结算，父 loop 要拿到 explore 结果才能进入基于该结果的下一步。后台分支则登记 notify 并立即返回 `<task ... state="running">`，父模型收到的是“已启动”，不是探索结论。

后台能力由 `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS` 控制：未显式设置时可继承总 `OPENCODE_EXPERIMENTAL`，显式 false 可以覆盖总开关。工具向模型展示的 JSON schema 在未启用时隐藏 background 字段，执行层仍再次检查，避免把 schema 当唯一保障。native LLM 开关则独立默认 false，不继承总实验开关。

完成通知不是修改已经返回给模型的那次 tool result。notify 等 job 最终完成或 error，然后向当前父 Session 提交一条 synthetic user text；它携带 child ID、状态和摘要，通过正常 prompt 路径重新唤起或加入父 loop。

<!-- EXCERPT 055d95bb7e278c94baf06235a52cac79dd13ba67 packages/opencode/src/tool/task.ts 227 253 -->
**真实源码 · B · V1 1.18.35** — [`packages/opencode/src/tool/task.ts`，L227–L253](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/tool/task.ts#L227-L253)

```typescript
      const inject = Effect.fn("TaskTool.injectBackgroundResult")(function* (
        state: "completed" | "error",
        text: string,
      ) {
        const currentParent = yield* sessions.get(ctx.sessionID)
        yield* ops
          .prompt({
            sessionID: ctx.sessionID,
            agent: currentParent.agent ?? ctx.agent,
            variant,
            parts: [
              {
                type: "text",
                synthetic: true,
                text: renderOutput({
                  sessionID: nextSession.id,
                  state,
                  summary:
                    state === "completed"
                      ? `Background task completed: ${params.description}`
                      : `Background task failed: ${params.description}`,
                  text,
                }),
              },
            ],
          })
          .pipe(Effect.ignore, Effect.forkIn(scope, { startImmediately: true }))
```

出处：[tool/task.ts:227](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/tool/task.ts#L227)。它会重新读取父 Session 当前 agent，而不是硬编码使用任务启动时的旧身份；synthetic 表明内部送达，不是用户真的重新敲了字。`Effect.forkIn(scope)` 说明通知计算也有资源所有者。完成结果进入新消息后，父模型才能把它与当时已经完成的其他工作一起总结。

这里还有两条容易遗漏的路径。第一，前台 job 可被 promotion 改成后台：等待者在 job.done 与 promoted Deferred 之间竞赛，promotion 只改变等待与通知策略，不重启 child，不换一套 Session。第二，对已有后台 child 再提交任务可能走 `background.extend`：新 run 排在已有工作的 tail 后，pending 计数保证最终通知等所有追加工作结算，输出使用较新的序号。它不是偷偷创建第二个同 ID job；但上层 child Session 的 prompt 仍遵循 RunState 的串行协调。

## 12. BackgroundJob 的边界：有状态管理，没有跨重启耐久性

核心实现明确选择进程内 scoped registry。V1 外层 adapter 又用 InstanceState 为每个项目目录创建这套引擎。源码里出现共享 core 引擎，不能直接推断所有 V1 项目任务共用一个跨目录队列。

<!-- EXCERPT 055d95bb7e278c94baf06235a52cac79dd13ba67 packages/core/src/background-job.ts 113 137 -->
**真实源码 · B · V1 1.18.35** — [`packages/core/src/background-job.ts`，L113–L137](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/core/src/background-job.ts#L113-L137)

```typescript
/**
 * Makes one scoped, process-local registry. Entries are intentionally not
 * durable: process restart or owner-scope closure loses status and interrupts
 * live work. Persisted observation, restart recovery, and remote workers need a
 * separate durable ownership slice rather than pretending this registry has
 * those semantics.
 */
export const make = Effect.gen(function* () {
  const state: State = {
    jobs: yield* SynchronizedRef.make(new Map()),
    scope: yield* Scope.Scope,
  }

  const settle = Effect.fn("BackgroundJob.settle")(function* (
    id: string,
    token: object,
    sequence: number,
    exit: Exit.Exit<string, unknown>,
  ) {
    const completed_at = yield* Clock.currentTimeMillis
    const result = yield* SynchronizedRef.modify(state.jobs, (jobs): readonly [FinishResult, Map<string, Active>] => {
      const job = jobs.get(id)
      if (!job) return [{}, jobs]
      if (job.token !== token) return [{}, jobs]
      if (job.info.status !== "running") return [{ info: snapshot(job) }, jobs]
```

出处：[core/background-job.ts:113](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/core/src/background-job.ts#L113)。SynchronizedRef<Map> 管 job 信息；done Deferred 给等待者最终状态；promoted Deferred 让前台等待者知道已转换；job scope 拥有实际 Fiber；token 防止旧执行误结算同 ID 后来的新 job；pending/tail/sequence 管理追加工作的顺序与最终输出。它们解决运行时竞态，不是可恢复任务的数据库 schema。

job 完成后更新 status、output/error、完成时间，并结算 done；cancel 关闭 scope，中断仍活动的工作。父会话 cancel 也会沿 metadata.parentSessionId/sessionId 递归取消后代 jobs，再取消父 Runner。前台 task 的资源释放若发现 interrupt，会取消 child Session 与对应 job，并移除 AbortSignal listener；删除 Session 也处理其后台后代。后台脱离的是父工具的同步等待，未脱离本进程、Instance 生命周期和明确的父取消语义。

进程退出后，Map、Fiber、done/promoted 信号及完成通知等待者都会消失。SQLite 中 `S-explore` 的历史可能还在，但不能据此说 job 仍 running 或将自动向父会话补通知。实现要有这样的保证，必须额外设计持久观察、运行所有权、幂等送达和恢复策略。保存一个 job ID 本身不能带来这些性质。

## 13. bash、直接 shell、PTY 与 Worker：四种资源不要串成一个概念

模型看到的工具 ID 仍是 `bash`，实现迁到 `tool/shell.ts`，为兼容既有权限与插件保留旧名称。它解析 shell/command/workdir/timeout，进行相应权限检查，用 ChildProcessSpawner 启动真实命令，合并读取输出，保留有界尾部和 metadata preview，过大的完整输出落盘。参数没有后台 job 模式；返回之前等待命令结束、取消或超时。

<!-- EXCERPT 055d95bb7e278c94baf06235a52cac79dd13ba67 packages/opencode/src/tool/shell.ts 533 559 -->
**真实源码 · B · V1 1.18.35** — [`packages/opencode/src/tool/shell.ts`，L533–L559](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/tool/shell.ts#L533-L559)

```typescript
          const abort = Effect.callback<void>((resume) => {
            if (ctx.abort.aborted) return resume(Effect.void)
            const handler = () => resume(Effect.void)
            ctx.abort.addEventListener("abort", handler, { once: true })
            return Effect.sync(() => ctx.abort.removeEventListener("abort", handler))
          })

          const timeout = Effect.sleep(`${input.timeout + 100} millis`)

          const exit = yield* Effect.raceAll([
            handle.exitCode.pipe(Effect.map((code) => ({ kind: "exit" as const, code }))),
            abort.pipe(Effect.map(() => ({ kind: "abort" as const, code: null }))),
            timeout.pipe(Effect.map(() => ({ kind: "timeout" as const, code: null }))),
          ])

          if (exit.kind === "abort") {
            aborted = true
            yield* handle.kill({ forceKillAfter: "3 seconds" }).pipe(Effect.orDie)
          }
          if (exit.kind === "timeout") {
            expired = true
            yield* handle.kill({ forceKillAfter: "3 seconds" }).pipe(Effect.orDie)
          }

          return exit.kind === "exit" ? exit.code : null
        }),
      ).pipe(Effect.orDie)
```

出处：[tool/shell.ts:533](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/tool/shell.ts#L533)。这场竞赛同时等 exitCode、AbortSignal 和计时器。正常退出带回 code；取消/超时会调用 kill，并配置三秒后的强制终止。`detached` 用于 POSIX 进程组管理，不等于工具立刻返回；把命令字符串写成 `... &` 也不等于获得系统维护的 job 观察、取消与结果回注协议。

用户直接执行 `!命令` 等 shell 路径走 SessionPrompt.shell 和前面的 Shell 状态机；模型调用 bash 仍属于 Running 里的工具。PTY 又是第三条路径：它保存交互式终端身份、进程、缓冲输出和连接，提供 write/resize/attach；适合持续交互，不等于把所有一次性测试工具自动改为后台 agent。PTY attach 断开与进程是否退出也应分别看其 API 语义。

TUI 的 Worker 是第四种角色。`cli/cmd/tui.ts:210` 创建 Worker，`cli/tui/worker.ts:30` 提供 fetch/server/reload/shutdown 等 RPC，并转发 GlobalBus 事件。其用途是划分 UI 与服务执行环境；不是每创建一个 Session 或 explore 都创建一个 Worker。Worker 可托管很多 Session/Fiber；shell 工具再在服务运行时下启动 OS 命令子进程。准确的图是“UI → Worker/RPC → 多个会话执行 → 某些工具的 OS 子进程”，不是“每个 agent 一个独立进程”。

## 14. retry 与 compaction：重新请求、继续推理、压缩历史各有不同原因

继续下一步通常说明本轮工具结果需要交给模型；retry 说明当前 provider 尝试发生可重试失败；compaction 说明上下文要缩短。Processor 的 `.retry(SessionRetry.policy(...))` 包住流消费尝试，失败分类在 policy 中处理，并向 SessionStatus 写 retry/attempt/next。取消 Cause 不会被当成普通网络错误无限重试。

<!-- EXCERPT 055d95bb7e278c94baf06235a52cac79dd13ba67 packages/opencode/src/session/retry.ts 188 205 -->
**真实源码 · B · V1 1.18.35** — [`packages/opencode/src/session/retry.ts`，L188–L205](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/session/retry.ts#L188-L205)

```typescript
  return Schedule.fromStepWithMetadata(
    Effect.succeed((meta: Schedule.InputMetadata<unknown>) => {
      const error = opts.parse(meta.input)
      const retry = retryable(error, opts.provider)
      if (!retry) return Cause.done(meta.attempt)
      if (meta.attempt > RETRY_MAX_RETRIES) return Cause.done(meta.attempt)
      return Effect.gen(function* () {
        const wait = delay(meta.attempt, SessionV1.APIError.isInstance(error) ? error : undefined)
        const now = yield* Clock.currentTimeMillis
        yield* opts.set({
          attempt: meta.attempt,
          message: retry.message,
          action: retry.action,
          next: now + wait,
        })
        return [meta.attempt, Duration.millis(wait)] as [number, Duration.Duration]
      })
    }),
```

出处：[session/retry.ts:188](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/session/retry.ts#L188)。常量上限为五次重试；每次先 parse 并判断 retryable，支持 transient 5xx、网络错误等，ContextOverflow 则不走普通重试。delay 优先尊重 Retry-After/Retry-After-ms，否则从约两秒指数退避并加抖动，无响应头时有延迟上限。SDK 调用默认 `maxRetries: input.retries ?? 0`，不能只看 SDK 默认值就误判整个会话没有重试。

重试不承诺副作用 exactly-once：如果 provider 在失败前已经发过工具调用，需要结合已保存 parts、清理与 adapter 语义分析，不能假定外部动作被回滚。Processor 最终失败会记录 assistant error 并清理孤儿状态；runLoop 不应把已经 interrupted 的 tool 误当待执行工作。

压缩仍在 V1 主链中显式编排。正常历史靠近 token 上限时创建 compaction part；遇到 provider ContextOverflow 可请求 overflow 压缩；下一次 loop 优先处理这个任务。专用 compaction agent 形成 summary assistant，选取要压缩的 head 并保留预算内最近 tail，调用 Processor 生成摘要时不给工具：

<!-- EXCERPT 055d95bb7e278c94baf06235a52cac79dd13ba67 packages/opencode/src/session/compaction.ts 419 448 -->
**真实源码 · B · V1 1.18.35** — [`packages/opencode/src/session/compaction.ts`，L419–L448](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/session/compaction.ts#L419-L448)

```typescript
      yield* session.updateMessage(msg)
      const processor = yield* processors.create({
        assistantMessage: msg,
        sessionID: input.sessionID,
        model,
      })
      const result = yield* processor.process({
        user: userMessage,
        agent,
        sessionID: input.sessionID,
        tools: {},
        system: [],
        messages: [
          {
            role: "user",
            content: [
              {
                type: "text",
                text: [
                  nextPrompt,
                  ...(compacting.prompt ? ["The following is the conversation history:", conversation] : []),
                ]
                  .filter(Boolean)
                  .join("\n\n"),
              },
            ],
          },
        ],
        model,
      })
```

出处：[session/compaction.ts:419](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/session/compaction.ts#L419)。`tools:{}` 和 summary 标记防止“生成摘要”又修改项目；summary 属于同一会话的历史管理，不是另开用户可见的 task child。成功后历史筛选使用摘要和保留尾部；自动压缩可注入 synthetic continue，overflow 路径还可能重放被截出的最近用户输入。prune 只标记更老工具输出的 compacted 时间，不等于再做一次摘要，也不等于清空数据库对话。

V1 每轮重组 environment/instructions/skills/MCP 指令，因此外部上下文变化会影响后续请求。它没有因此自动获得 V2 那种单独持久 epoch baseline 与变化边界。读到较新的工具注册、事件核心或 native client，仍须沿实际调用者判断它处在哪条执行链。

## 15. 把贯穿例子按时间重新拼起来

| 时刻 | 内存执行 | 持久可见记录与下一步 |
| --- | --- | --- |
| T0 用户提交 | `prompt(S-main)` | 保存 `U-01`，Runner 从 Idle 进入 Running |
| T1 第一次模型步骤 | Processor 处理 `A-01` 的流 | `call-read-01` 从 pending/running 到 completed，读出登录实现 |
| T2 委派 explore | task 创建/复用 `S-explore`，job 也用此 ID | child 保存自己的 user/assistant/tool 历史，父记录 `call-task-01` |
| T3 前台等待 | 父 task 等 job.done；child loop 独立推进 | child 返回入口结论，父 task 记 completed；若后台则这里已先返回 running |
| T4 运行测试 | 父下一模型步骤调用 `call-test-01` | shell 等命令退出，Processor 记录输出和退出信息，不因 detached 提前结束 |
| T5 汇总 | 父 loop 重新读取历史并发下一请求 | 模型看见探索结论和测试结果，产生最终 text，满足退出条件后 Runner 回 Idle |

如果 T2 选择后台，父可先做独立工作，但不能把尚未收到的探索结论写成已完成；T3 后的 synthetic 消息可能在父 busy 时加入现有 loop，也可能在父已 Idle 后启动新一轮。若 T4 用户取消，scope/fiber/AbortSignal 共同收尾并尽力终止命令；若取消父 run，还要处理后台后代。若宿主崩溃，保留下来的是已提交事实，不是表中的那些内存执行对象。

## 16. 十个深入问题与答案

**1. 为什么都用了 Effect，还称它为 V1？** 因为架构身份取决于调用链与数据契约。这里仍是 SessionPrompt/runLoop、V1 message/part、SessionProcessor；Effect 是依赖与执行方式，native LLM 和 EventV2 也是可共享设施。换语法和换 Session 核心不是同一个动作。

**2. 两个请求同时给 S-main 发 prompt，会产生两个模型循环吗？** 两条输入先各自保存，Runner 用同步状态转换只创建一个活动 run，其他调用等同一个 done。正在运行的 loop 下一轮重读历史，因此第二条输入可影响后续步骤；它不是独立 FIFO 用户任务。数据库写入与执行 join 两个层次都须检查。

**3. 为什么不直接用 busy=true/false 就够了？** busy 无法表达“shell 执行中但已经答应随后启动 run”，也难以把所有等待者结算到同一个结果。四态保留真正的 run/shell/pending handle，Deferred 传递结果，scope 管清理；busy 只是对外简化状态，不是完整执行所有权。

**4. Processor 是否负责执行每一个 tool-call？** 默认 SDK 路径中真实 execute 在 SessionTools 注册给 SDK 的回调里；Processor 消费标准事件、保存状态、结算记录。native adapter 有自己的工具 Fiber 调度，但仍向同一个 Processor 提供事件。照着 tool-call switch 再执行工具，会制造重复副作用。

**5. 为什么取消之后还要等工具结算信号？** 异步 execute 可能正在完成，流取消和结果回到宿主之间存在短暂竞态。done 让清理等待已开始的结算；等待窗过后仍未完成则写 interrupted 错误，避免 UI 永久 running。这个机制不能证明所有外部副作用都已停止或撤回。

**6. Task 是父会话里的一次函数调用，还是另一段对话？** 两者同时成立但层次不同：父看到一次 task tool call，宿主创建/复用 child Session，child 通过正常 prompt 开始自己的 loop。返回父会话的是整理后的结果，child 完整历史仍单独保存；child 不自动复制父全部上下文。

**7. 前台 task 转后台会重新做一遍吗？** promotion 结算专门的 promoted 信号，前台等待者返回运行信息，onPromote 建完成通知；原 job/child 继续工作。它转换等待和结果送达方式，不通过重启去“后台化”。追加任务 extend 则是另一种操作，会增加排队工作而不换 ID。

**8. 子 agent 会继承父 agent 的全部禁止项吗？** 不是这种简单全拷贝。父 Session 的 deny/external_directory 约束被传下，child 的能力由所选 agent 配置决定，再叠加 task/todo 默认限制、深度等检查。任务授权、工具授权、目录边界和 OS 沙箱是不同层，不可互相代替。

**9. shell 返回 0 是否代表用户任务成功？** 只代表该命令报告正常退出；测试可能选错目标、零测试、忽略失败，甚至输出未满足用户要求。agent loop 要读具体输出再推理。非零退出也要结合工具返回约定区分“命令执行完成但测试失败”与“宿主根本没能执行命令”。

**10. 会话/事件/job ID 都保存过，为什么不能宣称自动恢复？** 会话和事件是耐久事实，job registry 与运行连接不是。尤其副作用与结算之间有不确定窗口，重放读模型不等于重做操作。要恢复需另外定义运行所有权、已派发请求歧义、幂等、副作用与通知补偿；这些不能从一个 ID 或一条 running 记录推出。

本章只读核对固定提交的实现，没有调用真实模型、执行示例测试命令或修改应用源码。继续阅读新版 V2 时，可按“输入何时可见、谁拥有执行、工具何时结算、事实怎样保存、取消向哪传播”这五个问题逐项替换心智模型，而不必因文件移动重新从零理解一个 agent。
