# 05 · 三个版本放在一起：变的究竟是哪一层

先读完三个版本的独立实现篇，再使用本章。A 指 v1.2.24，B 指推广前最终 V1 1.18.35，N 指 2026-10-29 的 V2 2.2.0；固定提交见总览。本章的问题不是“哪个版本多几个工具”，而是同一用户动作在三种执行模型里分别产生什么状态、由谁推进、什么时候能安全结束。

## 1. 用户按下发送：保存一句话和接受一项工作，开始成为两件事

假设会话 P 正在分析登录代码，用户补充：“先不要改文件，只整理调用链。”我们关心的不是 HTTP 是否返回，而是这句话何时成为下一次模型请求的输入。

**A 的路径** 是 `prompt → createUserMessage → loop`。用户消息先直接写入普通会话历史；loop 下一次重读历史时能看到它。若同一个 Session 已有运行者，这次调用不再启动第二个 loop，而是把自己的等待回调登记到现有运行者上。因此“多次输入”可以汇入同一条运行链，但没有单独的持久输入队列来表达“这句话已接收，还没有交给模型”。`noReply:true` 仍会写普通 user message，只是不主动开始回复。

**B 延续这个可见性模型** 。输入和可见历史的关系没有因为写法改成 Effect 就自动变成 V2 inbox；业务主线仍是保存 user message、调用 `loop`。变化主要是运行者和等待者由显式状态机、Fiber、Deferred 管理。

**N 的路径** 则先持久化 `session_inbox` 中的待交付项。正在生成的模型流不被这条新输入原地改写；在后续安全 step 边界，runner 才交付它并形成普通可见消息。`steer` 和 `queue` 让调用方表达两种不同意图：前者希望尽早影响后续步骤，后者希望等当前工作将进入空闲后再开始。`resume:false` 是先接受工作而不唤醒执行，不等价于 A/B 中“已经写入普通历史但不回复”的 `noReply`。

<!-- EXCERPT c6262f9d4002d86a1f1795c306aa329d45361d12 packages/opencode/src/session/prompt.ts 158 184 -->
**真实源码 · A · v1.2.24** — [`packages/opencode/src/session/prompt.ts`，L158–L184](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/session/prompt.ts#L158-L184)

```typescript
  export const prompt = fn(PromptInput, async (input) => {
    const session = await Session.get(input.sessionID)
    await SessionRevert.cleanup(session)

    const message = await createUserMessage(input)
    await Session.touch(input.sessionID)

    // this is backwards compatibility for allowing `tools` to be specified when
    // prompting
    const permissions: PermissionNext.Ruleset = []
    for (const [tool, enabled] of Object.entries(input.tools ?? {})) {
      permissions.push({
        permission: tool,
        action: enabled ? "allow" : "deny",
        pattern: "*",
      })
    }
    if (permissions.length > 0) {
      session.permission = permissions
      await Session.setPermission({ sessionID: session.id, permission: permissions })
    }

    if (input.noReply === true) {
      return message
    }

    return loop({ sessionID: input.sessionID })
```

上面 A 的代码把 `createUserMessage` 放在 `noReply` 判断之前，这个顺序就是语义证据。对照 N 中 admission 完成后才可选 wake 的代码：

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/session/session.ts 155 178 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/session/session.ts`，L155–L178](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/session.ts#L155-L178)

```typescript
  const prompt = Effect.fn("Session.prompt")((sessionID: SessionSchema.ID, input: PromptRequest) =>
    Effect.uninterruptibleMask((restore) =>
      Effect.gen(function* () {
        const session = yield* get(sessionID)
        const messageID = input.id ?? SessionMessage.ID.create()
        const admitted = yield* Effect.gen(function* () {
          const existing = yield* admission.reconcile({
            id: messageID,
            sessionID: session.id,
            type: "user",
            delivery: input.delivery ?? "steer",
          })
          if (existing) return existing
          // Preparation, the staged revert commit and admission share one acquisition of the session's location
          // services, so projections see its config whoever called prompt.
          return yield* Effect.gen(function* () {
            const item = yield* restore(SessionPrompt.prepare({ session, messageID, input }))
            // Commit a staged revert only after preparation succeeds, before admitting new work.
            if (session.revert) yield* SessionRevert.commit(bus, session)
            return yield* admission.admit({
              id: messageID,
              sessionID: session.id,
              item,
            })
```

这里 `reconcile` 先处理重复输入；只有没有既有身份时才 prepare/admit。对新 N 的 user/synthetic 项，幂等契约是同 Session、同类型的第一次 admission 获胜，重试携带的新文本、metadata 或 delivery 不覆盖原项。不能拿旧重构快照的“逐字段 payload 必须相等”套到这里。一个容易踩的坑是用户真正编辑了内容，却复用上一次输入 ID：这会被当成重试，而不是一次新编辑。

**学习迁移点：** A/B 把“历史中出现 user message”视为主要输入事实；N 需要分别问“已接收吗”“仍在 inbox 吗”“已交付给历史吗”。这三个问题的答案可以不同。

## 2. 两个请求同时推进一个 Session：互斥对象没有变，所有权表达变了

A 在 Instance 中保存 `SessionID → AbortController + callbacks`。第一位调用者创建运行者，后来的调用者登记回调。这里的 callbacks 是“等结果的人”，不是一批模型输入；模型输入早已写入历史。不要把这两个队列概念合在一起。

<!-- EXCERPT c6262f9d4002d86a1f1795c306aa329d45361d12 packages/opencode/src/session/prompt.ts 65 83 -->
**真实源码 · A · v1.2.24** — [`packages/opencode/src/session/prompt.ts`，L65–L83](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/session/prompt.ts#L65-L83)

```typescript
  const state = Instance.state(
    () => {
      const data: Record<
        string,
        {
          abort: AbortController
          callbacks: {
            resolve(input: MessageV2.WithParts): void
            reject(reason?: any): void
          }[]
        }
      > = {}
      return data
    },
    async (current) => {
      for (const item of Object.values(current)) {
        item.abort.abort()
      }
    },
```

B 把手工控制关系收敛为四种状态。`Running` 是模型执行链，`Shell` 是用户直接发起的 shell 执行，`ShellThenRun` 表示 shell 结束后还欠一个模型工作。Deferred 给等待者一个共享的完成信号，Fiber 是真实活动执行，scope 决定何时回收它。

<!-- EXCERPT 055d95bb7e278c94baf06235a52cac79dd13ba67 packages/opencode/src/effect/runner.ts 14 37 -->
**真实源码 · B · V1 1.18.35** — [`packages/opencode/src/effect/runner.ts`，L14–L37](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/effect/runner.ts#L14-L37)

```typescript
interface RunHandle<A, E> {
  id: number
  done: Deferred.Deferred<A, E | Cancelled>
  fiber: Fiber.Fiber<A, E>
}

interface ShellHandle<A, E> {
  id: number
  cancelled: Deferred.Deferred<void>
  ready?: Latch.Latch
  fiber: Fiber.Fiber<A, E>
}

interface PendingHandle<A, E> {
  id: number
  done: Deferred.Deferred<A, E | Cancelled>
  work: Effect.Effect<A, E>
}

export type State<A, E> =
  | { readonly _tag: "Idle" }
  | { readonly _tag: "Running"; readonly run: RunHandle<A, E> }
  | { readonly _tag: "Shell"; readonly shell: ShellHandle<A, E> }
  | { readonly _tag: "ShellThenRun"; readonly shell: ShellHandle<A, E>; readonly run: PendingHandle<A, E> }
```

读这段时注意 `PendingHandle.work` 与 `RunHandle.fiber` 的区别：前者还没开始，只记录未来要运行的 Effect；后者已经拥有一个运行中的 Fiber。这样可以明确表达“正在 shell，之后再跑模型”，而不靠多个布尔值组合出隐含状态。

N 将执行协调服务做成 process-global，以 Session ID 为 key，再通过当前 instances 服务为 Session 获取对应 runner；默认按 Location 装配，SDK 还可按稳定 instance key 与 Location 进一步细分。以下是它对显式 resume 与 advisory wake 的不同处理：

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/session/run-coordinator.ts 119 140 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/session/run-coordinator.ts`，L119–L140](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/run-coordinator.ts#L119-L140)

```typescript
    const run = (key: Key): Effect.Effect<void, E> =>
      Effect.suspend(() => {
        const execution = executions.get(key)
        if (execution !== undefined) {
          // A stopping execution refuses joiners: wait out its cleanup, then run fresh.
          if (execution.stopping)
            return Deferred.await(execution.done).pipe(Effect.ignoreCause, Effect.andThen(run(key)))
          return Deferred.await(execution.done)
        }
        return Deferred.await(start(key, true, "input").done)
      })

    const wake = (key: Key, scope: Promotable = "input") =>
      Effect.sync(() => {
        const execution = executions.get(key)
        if (execution !== undefined) {
          // Coalesced wakes keep the widest scope: "input" subsumes "steer".
          execution.pendingWake = execution.pendingWake === "input" ? "input" : scope
          return
        }
        start(key, false, scope)
      })
```

`run` 有活动执行时等待同一个 `done`；空闲时可以 forced 启动。`wake` 则像门铃：有执行者就合并记录 pendingWake，没有执行者才启动一次非强制 drain。它不是每敲一次就启动一个模型。门铃还要覆盖“runner 刚判断无事可做、但尚未完全转为空闲”的缝隙，否则输入可能已经落库，却没有执行者继续处理。

三个版本都不应让同一会话出现两个无协调的活动主循环。N 的变化不是“终于支持并发”，而是使全局会话执行、Location 能力环境和持久输入接收各自有明确边界。不同 Session 可以同时推进；它仍不是多个机器共享一个分布式执行锁。

## 3. 模型发出工具调用：事件处理和实际执行不能混读

假定模型输出一个 read 调用。A 的 `resolveTools` 已把工具转成 AI SDK 的 `execute` 回调；SDK 处理本次模型流时触发回调。processor 的 `tool-call` 分支负责把对应 part 标成 running，并不因此独自承担了所有工具执行。实际 read、task、bash 仍在各自 execute 里做工作。完成结果被整理回模型消息，外层 loop 再决定下一次模型请求。

B 保留这一基本分工，但工具 execute 是 Effect 逻辑，借助 EffectBridge 接到需要 Promise 的 SDK 边界；模型事件也归一成统一的流。可选 native LLM 适配器改变 provider/工具事件的接法，但外层仍是 V1 SessionPrompt，不会凭这个开关就切成 V2。

N 的 Session step 自己掌握本次模型调用与本地工具调度：完整调用事实先进入投影，再启动受作用域管理的工具执行；模型流结束后，要等待所有已启动工具结算，才推进正常后续 step。工具可以在模型仍继续输出其他调用时立即开始，减少不必要的等待。

这里有两个不同的“并行”：

1. 同一 step 中多个独立工具 Fiber 同时工作，但下一 step 仍等它们结算。
2. shell/subagent 工具选择后台模式后，工具先返回 running 身份，实际工作继续；父模型可以在其他 step 里工作，稍后由 synthetic 输入收到结果。

第一种不是第二种。只看到 `forkScoped` 并不能证明工具支持用户可见的后台协议；只看到一个 Promise 被 await，也不能证明整个服务事件循环被阻塞。

**学习迁移点：** 先标出“谁接收模型 call”“谁真正调用工具 handler”“谁写结果”“谁决定下一 step”，再看它使用 Promise 还是 Effect。控制权比函数名更重要。

## 4. 子 agent：三个版本都有 child Session，但后台生命周期逐步变完整

A 中父模型调用 `task`，TaskTool 校验 agent/权限、创建或复用一个 child Session，然后 `await SessionPrompt.prompt(child)`。新子会话没有自动复制父历史；父模型必须把交接内容写进 task.prompt。父工具等到 child 最终文本后，包装成 task_result 作为工具结果返回。child 的 Session ID 是可续接身份，不是 PID。

B 的前台路径已经围上 BackgroundJob：即使默认等结果，也先成为受管理的 job，再等待完成或 promotion。启用 `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS` 后，task 可直接后台返回；也能把正在前台等待的 task 转后台。后台完成会 synthetic prompt 回注父会话。因此“后台子 agent 是 V2 才首次实现”不成立。

N 的默认工具名是 `subagent`，通过工具插件贡献注册；不是简单去旧目录找 `task.ts`。它创建带 parentID 的 child，先 admit 子输入，再由 SubagentJob/Job 组织 resume、前后台等待和完成观察。还显式检查续用的 Session 确实属于当前父会话，避免把任意 Session 当作自己的 child。

N 的通知与后台作业不再依赖一个可被 Location 释放带走的插件局部 scope；恢复元数据、通知 ID 与已完成结果可以持久保存。真正提升的是“后台工作拥有独立且可解释的生命周期”，不是“用了新词就有了另一个进程”。

取消语义也要分版本：A 用 AbortSignal 转发 child cancel；B 的 run-state 有明确的后代后台任务遍历取消；N 明确传播前台等待的 interruption，但不能不查实现就推断普通父会话 interrupt 必然递归杀死所有已经后台化的 descendants。先区分 parent 的一次执行、child Session、后台 Job 三种所有权。

## 5. shell：V2 增加的是受管理后台协议，不是操作系统第一次能后台运行

A/B 的 bash 工具执行命令并等最终结果。命令内部当然可以使用 shell 自己的 `&` 等机制，但 OpenCode 不会因此自动创建一个可跟踪、可通知、可转前后台的工具级 job。后台进程能否存活还取决于输出管道、进程组、终端和宿主清理。

N 的 `shell` 输入包含 background。它先建立真实命令资源和输出文件，再注册 Job：

```text
Shell.create / OS spawn / 输出捕获
       ↓
Job.start（管理等待、后台化、结果与恢复元数据）
       ↓
前台 block → 命令终结 → 返回最终 tool result
后台或转后台 → 立即返回 shellID → 命令终结后 synthetic 通知
```

顺序很重要：不是 Job 这个抽象本身会神奇地启动所有系统进程，实际 OS 边界在 Shell/AppProcess/Spawner。默认前台超时与后台超时策略也不同。当前 HTTP shell 接口支持观察状态、读取输出、终止与删除资源；模型直接可用的工具目录和这些管理 HTTP API 又是两层。

如果你问“后台测试已经结束，为什么父模型还不知道”，应分别检查进程退出、Job 终态、通知是否 admission、父 Session 是否推进，以及通知是否已交付给模型历史，而不是只看 OS PID。

## 6. 持久化：必须明确保存的是哪一种事实

A 已经用 SQLite 保存 Session/message/part。它不是“纯内存原型”。内存中丢失的是活跃 Promise、AbortController、等待者等运行状态。

B 已有共享 EventV2、事务投影与兼容表。它也不是“只有 Bus，没有事件持久化”。Effect 和事件基础设施均在最终 V1 演进中存在，不能包装成 V2 的独创。

N 延续事务投影与顺序管理，但本次最新代码中还有一个必须点出的配置分界： **原始事件日志是否长期保留，并不等于读模型是否持久化。**

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/bus.ts 200 210 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/bus.ts`，L200–L210](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/bus.ts#L200-L210)

```typescript
          durable: new Map<string, Set<PubSub.PubSub<void>>>(),
          typed: new Map<string, Set<PubSub.PubSub<Event.Payload>>>(),
        }
        const projectors = new Map<string, Subscriber[]>()
        const listeners = new Array<Subscriber>()
        const durableLocks = KeyedMutex.makeUnsafe<string>()
        const { db } = yield* Database.Service
        const logReadPageSize = options?.logReadPageSize ?? 512
        const persist = options?.persist ?? false
        const sessions = new Map<SessionID, Location.Ref>()
        // Keep routing separate from the public event, and retain its snapshot
```

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/bus.ts 402 432 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/bus.ts`，L402–L432](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/bus.ts#L402-L432)

```typescript
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
                              seq: sql`max(${EventSequenceTable.seq}, ${seq})`,
                              ...(input?.ownerID && row?.ownerID == null ? { owner_id: input.ownerID } : {}),
                            },
                          })
                          .run()
                          .pipe(Effect.orDie)
                        if (persist)
                          yield* db
                            .insert(EventTable)
                            .values([
                              {
                                id: event.id,
                                aggregate_id: aggregateID,
                                seq,
                                created: event.created ?? 0,
                                type: versionedType(definition.type, durable.version),
                                data: encode(),
                              },
                            ])
                            .run()
                            .pipe(Effect.orDie)
```

第一段中 `persist` 默认 false；第二段先执行 projector、commit hook 和 sequence 更新，再按 persist 决定是否额外插入 EventTable。事务投影仍可以把消息、inbox、claim 等状态可靠保存，但没有启用原始日志保留时，不能承诺以后能从完整原始事件重新播放一切。

同理，实时 token delta、消息完成边界、Job 的 durable notification marker、OS 进程输出文件是不同对象。它们的保留和重连语义必须各自看。UI 重连后能读回消息，不代表每个历史 token 的传输片段都可逐字重演。

## 7. 取消、超时、失败、关闭、崩溃：不要压成一个“停止”

| 情况 | 它真正意味着什么 | 不应推出什么 |
| --- | --- | --- |
| 模型正常结束 | loop/runner 判定当前不再需要继续 | Session 被删除 |
| 工具失败 | 一次能力执行出现错误，通常要形成模型可见结果 | 整个会话一定失败 |
| 用户 interrupt | 请求活动执行停止 | 所有外部副作用已撤回 |
| shell timeout | 命令管理层尝试终止并报告超时 | 同一 Job 的 wait timeout 也会杀命令 |
| 服务正常替换 | 宿主按设计释放、交接或保存恢复标记 | 等同于突然断电 |
| 进程崩溃 | 内存执行所有权消失，可能留有模糊结果 | 可以无条件重跑副作用 |

A/B 的手工 abort 或 Effect interrupt，本质上都要求外部资源层配合。JS 中断只改变内部控制流，shell 必须落实到杀进程/进程组，网络调用也必须有其取消路径。

N 把 interrupt 的接受确认与清理结束拆成两种等待：调用者可以迅速得到“已接受”结果，需要等待本 Session 前台 execution 的清理和结算时，再等核心 execution.awaitIdle；公开 Session.wait 内部使用这个等待点。它不承诺后台 Job、子会话或公共资源已经全部停止。等待慢 finalizer 的 UI 不必把一次点击卡成整段清理，但开发者也不能拿 interrupt 返回当作“进程已死、文件句柄全关”。

## 8. 重试与恢复：从重新请求到知道自己为什么继续

A processor 会判断可重试错误、退避再调模型；B 有更明确的 Session retry policy 和 retry 状态。这里已有“失败后再尝试”的业务能力。

N 把 step 与 attempt 分开：一个逻辑模型步骤可以有多次失败尝试。是否可以重试还取决于已经产生了哪些可持久输出，而不是所有错误都重新开始。已经有部分输出时，有些恢复会生成后续 assistant 继续，而不是把之前输出抹掉装成从未发生。

还要区分 model retry、工具自身内部重试、用户重复提交同一输入、server restart resume。它们有不同的身份与预算。当前 N 的文本模型 RequestExecutor 也不能沿用旧快照的“底层固定两次 HTTP 重试”；真正策略位置以当前 AI/core 分层为准。

N 的 execution claim 让启动恢复能发现“上一个进程曾经负责推进而未终结的 Session”。后台 Job 另有通知恢复信息。 **claim 是恢复标记，不是分布式租约** ：一个旧 owner 如果其实没死，多个进程擅自一起恢复仍可能出问题。

shell 与 subagent 也不能用同一种恢复口号概括：shell 通常补交“服务器重启导致取消”的结果，而非重跑命令；子会话可按预算从持久上下文继续。文件写入与远端 API 副作用仍可能已经发生，恢复不提供 exactly-once 保证。

## 9. 进程结构：最新 PTY 和旧 Worker 需要分开读

A 的本地 TUI 已经有 Worker/RPC；这不是 V2 首次引入线程。A/B 的普通 PTY 则管理有 PID 的交互终端，detach 订阅与 kill 进程是不同操作。

N 当前不能再沿用旧 TUI Worker 的整张架构图解释默认入口。CLI 与服务进程分离；出现 Worker 的地方还可能只是图像处理，而非 Session 执行宿主。更进一步，PersistentPty 引入独立 `opencode-pty` daemon，并在受控服务替换时支持 best-effort handoff。普通 scope PTY 与这个可交接资源必须分开；突然崩溃、机器重启和受控 server 替换也不是一种场景。

因此“V2 所有 PTY 随 server 一重启就丢”也过时了。请沿第 04 章的实际 daemon/IPC/prepare_handoff/accept 调用链读，确定当前采用的是哪一种 PTY 模式。

## 10. 上下文与压缩：改变的是历史和缓存的组织方式

A/B 的 instructions/system prompt 主要由当前环境、选定 agent/provider 和已有消息重新组装；compaction 是另一条摘要生成和历史筛选路径。A/B 本来就支持压缩，并非只有 V2 才考虑长对话。

N 将来源观察、hash blobs、初始/当前 instruction state 与后续变化留痕分开。稳定前缀有利于 provider cache 使用，变化又能按顺序进入后续历史；压缩或会话移动时有相应重建/更新边界。这里的目标是让“模型此次看到哪些特权指令、为什么变化”更可解释。

但缓存友好的输入组织不等于测得 token 减少或延迟降低。provider 是否命中缓存、计费如何、历史增长多少，都需要用同一任务、模型、环境和计量方式验证。读源码可以证明机制存在，不能替代基准测试。

## 11. 学完三版后，应该如何迁移自己的理解

先保留 A 的最小闭环：输入→历史→模型→工具→结果→继续/结束。再把 B 理解为这个闭环的生命周期与能力工程化：Effect、Runner、事件投影、后台 task。最后把 N 理解为重新定义几个核心边界：输入 admission/交付、执行所有权/位置、step/attempt、工具贡献/调用、工作/等待/通知、恢复标记/实际副作用。

你以后看到新功能，可以用同一组问题定位：它改变输入契约，还是执行调度？产生新持久状态，还是只有实时 UI 状态？是工具叶子能力，还是共享的运行框架？由谁拥有 scope，谁负责通知，谁有资格恢复？这样才能把版本变化读成一套机制，而不是记住一张“有/没有”的表。
