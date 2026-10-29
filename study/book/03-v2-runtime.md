# 03 · 当前 V2 的运行核心：把一个输入推进成可靠的模型与工具历史

本章固定阅读提交 `726cf096396d5014c3b1845e231cc01efe0421ee`，根包版本 2.2.0。它独立介绍这份 V2 实现，不要求读者记住之前重构快照的类名。源码片段只取自这个提交；图中的会话名、消息名和顺序数字是教学示意，不是本次实际模型运行的日志。本章没有调用外部模型、修改应用源码或宣称跑过完整测试。

**存储前提：** 下文关于跨进程重启后读回历史、claim 和通知的保证，针对使用持久存储的宿主。嵌入 SDK 默认 `database.path=":memory:"`（`packages/sdk/src/internal/host.ts:41`），要跨重启恢复必须配置持久 `database.path`；内存 SQLite 也执行事务和投影，但进程关闭后数据不会保留。CLI 受管理服务器显式选择磁盘数据库（`packages/cli/src/server-process.ts:114–115`），workerd 则注入 Durable Object 存储。不要把“核心提供恢复机制”读成“所有入口默认都能跨重启恢复”。

从 v1.2.24 过来，最值得保留的认识是：模型不会凭空持续工作。应用必须反复准备历史、发起模型请求、执行工具、记录结果，再决定是否继续。V2 没有取消这条主线，而是把它拆成可以分别说明所有权、持久化、重试和取消的几个边界。先理解这些边界，再追服务装配，目录数量就不再像一堵墙。

## 1. 先给运行单位各取一个名字

**Session 是会话身份和历史，不是活动线程。** `SessionStore` 能读到一个 Session，并不代表服务器正在为它调用模型。 **Inbox 是尚未交付给会话历史的工作。** 用户输入到达后可以先在这里等待，而不是立即挤进正在发送给模型的消息。 **Step 是一个逻辑模型调用单位** ；失败时可以有多个请求 attempt。 **Drain 是当前执行者推进这个 Session 的一段工作过程** ，它可包含多个 step，甚至在 Location 切换后交接到另一份 runner。

**Agent 是执行策略身份** ：提示、模型选择、权限和步数等，不自动等于一个进程。 **Tool call 是模型请求一项能力** ，工具可能读文件、起 OS 进程，也可能启动另一个会话。 **Fiber 是 Effect 的协作执行单位** ，用于并发、等待和中断；它不自带系统 PID。把这几个名词分开后，“Session 有后台子 agent”和“Session 自己的前台执行已空闲”就可以同时成立。

本章会反复使用 `S1` 表示同一个真实 Session ID，`U1/U2/U3` 表示三个符合实际 ID schema 的输入 ID，`A1/A2` 表示 assistant 消息 ID。这些简写只为方便手工画状态图。设 S1 位于目录 `/workspace/demo`，用户第一次输入是“阅读 README，说明启动方式”；接着在模型工作期间补充“只看 Linux”，再排队“然后检查测试入口”。这三个输入分别对应最初工作、steer 和 queue，足以把大部分核心结构串起来。

给这次纸面追踪指定具体示意 ID：`S1=ses_study_001`，`U1=msg_study_001`、`U2=msg_study_002`、`U3=msg_study_003`。这些字符串满足本提交对应 ID schema 的前缀检查，但不是生成器在本次运行中创建的真实会话。已在正确 Location 上创建 S1 后，第一条 HTTP 请求体可写成下面这样；这里是接口使用示意，后文带 SOURCE 标记的内容才是仓库原文摘录。

```json
{
  "id": "msg_study_001",
  "text": "阅读 README，说明启动方式",
  "delivery": "steer",
  "resume": false
}
```

请求地址是 `/api/session/ses_study_001/prompt`。后续在同一个地址提交 U2 时改 text 并省略 resume，提交 U3 时改 delivery 为 queue；这样每个 ID 与叙述中的输入都能一一对应，不必把请求序号、会话 ID、工具 call ID 混为一谈。

为避免阅读途中又把它们合成一个“状态”，可以先准备下面这张草稿。字段仅列理解本章必要的部分，真实类型以源码为准。

| 对象 | 需要追踪的内容 | 保存在哪里 |
| --- | --- | --- |
| Session | 身份、Location、parentID、所选 agent/model、权限 | 数据库会话投影 |
| Inbox item | 输入身份、所属会话、类型、payload、delivery、入队顺序 | 数据库 pending 表 |
| Assistant message | 回答身份、内容块、完成时间、错误、模型信息 | 消息及 part 投影 |
| Execution | owner、done、pendingWake、stopping | 当前进程 coordinator Map |
| Logical step | 当前 agent 步数计数、本次请求准备、attempt 结果 | runner 控制流及相应消息/事件 |
| Claim | 未释放执行意图、恢复次数 | 数据库会话上的运行恢复字段 |

用户问“是否还在执行”时，不能随意挑一行作为答案。Inbox 有内容只说明尚有待交付输入；assistant 未完成可能是活动模型，也可能是崩溃遗留；claim 仍在也可能等待下一次启动恢复。反过来，前台 Execution 已空闲不妨碍独立后台 Job 继续工作。界面状态要综合这些来源，核心源码也因此让它们保留不同身份与生命周期。

本章的调试办法是：每经过一个调用边界，就在这张表上标一次变化。若函数只是 wake，应该改的是门铃或活动 owner，不是偷偷新建 user message；若是 InboxDelivered projector，则应消费 pending 并增加可见历史，不负责直接发网络请求。能准确预测这种局部变化，比记住十几个 Service 名更有用。

## 2. Service、Layer 与 Scope 各管一件事

`Context.Service` 提供“我要用哪一种能力”的类型化身份和接口。执行 `yield* SessionStore.Service`，取到的是当前运行环境已经提供的实例。它不表示此刻重新构造整个数据库。`Layer` 描述实例如何依赖别的能力并被构建；实际是否共享取决于服务图、memoization 和构建作用域。`Scope` 管生命周期，离开时运行 finalizer，关闭资源并中断挂在它下面的结构化工作。

仓库在这些基础上引入 global 与 Location node。Global 通常表示同一个宿主运行时图中共享：Bus、Session facade、SessionStore、SessionExecution、Job 等。它不是跨机器共享内存。Location 表示某个目录和可选 workspace 对应的环境：Agent、权限、模型配置、工具快照、文件系统和 instruction sources 等从这里取得。两个 Session 可以共享一份 Location 服务，但各自读写不同的历史。

核心默认不会为每个 Session ID 创建一套专属服务 Layer。按 Session 绑定的 handle 只是对会话操作参数的封装；真正运行时，SessionExecution 先查 S1 的最新位置，再进入该 Instance 提供的 runner。这样移动 Session 后不必让旧目录的服务永远拥有它。下面的 `instances.provide(session)` 是关键：路由位置属于执行进入时读取的会话事实。

嵌入 SDK 还允许 `instances.key(session)` 选择应用级实例身份，并把它与 canonical Location 一起作为缓存键（`packages/sdk/src/internal/instances.ts:54–75`）。所以同目录的两个 Session 可以按稳定应用 key 取得不同插件/能力配置；相同 key 但不同目录或 workspace 也不会合成一个实例。省略 selector 时仍按默认 Location 共享。 **这只是能力实例选择，不是授权或存储隔离边界** ，不能据此假设插件能读写的 Session 或持久存储已经按 key 安全分区，见 `packages/sdk/README.md:63–74`。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/session/execution.ts 84 107 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/session/execution.ts`，L84–L107](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/execution.ts#L84-L107)

```typescript
    const drain = Effect.fnUntraced(function* (
      sessionID: SessionSchema.ID,
      force: boolean,
      continuation?: SessionRunner.Continuation,
      promotable: SessionInbox.Promotable = "input",
      stepped = false,
    ): Effect.fn.Return<void, SessionRunner.RunError> {
      const session = yield* store.get(sessionID)
      if (!session) return yield* Effect.die(new Error(`Session not found: ${sessionID}`))
      const result = yield* SessionRunner.Service.use((runner) =>
        runner.drain({ sessionID, force, continuation, promotable, stepped }),
      ).pipe(
        instances.provide(session),
        Effect.tapCause((cause) =>
          Cause.hasInterruptsOnly(cause)
            ? Effect.void
            : Effect.logError("Failed to drain Session", cause).pipe(Effect.annotateLogs({ sessionID })),
        ),
      )
      return yield* SessionRunner.DrainResult.$match(result, {
        Complete: () => Effect.void,
        Moved: (result) => drain(sessionID, false, result.continuation, promotable, result.stepped),
        Reloaded: (result) => drain(sessionID, result.force, result.continuation, promotable, result.stepped),
      })
```

Global 与 Location 也不是权限的替代词。一个进程级服务可以通过 Location 获取受约束的文件能力；一个 Location 服务也可能启动拥有宿主用户权限的 shell。作用域决定实例和资源活多久，权限决定允许做什么，OS 隔离则是另一层。学习时不要用其中一个词代替另外两个。

## 3. 创建 Session：先有一个可查询的身份

HTTP 的 `POST /api/session` 先经过协议解码、Location 与授权等中间件，handler 再调用核心 facade。内部 `create` 可以接收明确 Location，也可以接收 parentID 来继承父会话位置。新会话包含项目、标题、agent/model 选择、权限、metadata 等，后续 runner 才据此选择执行环境。本章关注顶层 S1；子会话的模型选择与父子通知在子 agent 章展开。

`create` 的第一步是读取调用者给的 ID。若 S1 已存在，直接采用原有身份，不重新覆盖标题、位置或模型。若两个请求并发创建同一个 ID，投影唯一性冲突会回查已有 Session。这是身份幂等，不是执行 exactly-once：创建会话不直接运行模型，更不证明某个 shell 命令只执行过一次。

下面片段同时展示“发布 Created 事实”和“由投影产生可查询记录”的入口。子 Session 在没有显式 metadata/permissions 时可以继承父会话值，但这与模型是否获得父对话全文是不同问题；历史是否复制要查具体创建/委派入口，不能从 parentID 推断。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/session.ts 288 318 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/session.ts`，L288–L318](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session.ts#L288-L318)

```typescript
      create: Effect.fn("Session.create")(function* (input) {
        const sessionID = input.id ?? SessionSchema.ID.create()
        const recorded = yield* store.get(sessionID)
        if (recorded) return recorded
        const parent = input.parentID ? yield* store.get(input.parentID) : undefined
        if (input.parentID && parent === undefined) return yield* new NotFoundError({ sessionID: input.parentID })
        const location = parent?.location ?? (input.location && LocationServiceMap.canonical(input.location))
        if (location === undefined)
          return yield* Effect.die(new Error("Session.create requires either location or an existing parentID"))
        const project = yield* projects.resolve(location.directory)
        const projected = yield* bus
          .publish(
            SessionEvent.Created,
            {
              sessionID,
              slug: Slug.create(),
              version: app.version,
              projectID: project.id,
              parentID: input.parentID,
              location,
              subpath: RelativePath.make(path.relative(project.directory, location.directory).replaceAll("\\", "/")),
              title: input.title,
              agent: input.agent,
              // Children inherit metadata and permissions the way they inherit
              // location, so host policies that read them treat the family uniformly.
              metadata: input.metadata ?? parent?.metadata,
              permissions: input.permissions ?? parent?.permissions,
              model: input.model,
            },
            { location },
          )
```

创建成功后，S1 即使从未调用过模型，也已经是合法会话。此时数据库里有身份，coordinator 的 active 集合却可以完全没有 S1。后面所有“这个会话是否忙”“能否取消”“重启后是否该继续”的问题，都不能仅靠会话存在来判断。

## 4. 收到输入：HTTP 成功不等于模型已经看见

当前普通输入端点是 `POST /api/session/:sessionID/prompt`。payload 使用 `text`，还可以带 files、agents、skills、metadata、delivery 和 resume。协议与 handler 分别见 `packages/protocol/src/groups/session.ts:337`、`packages/server/src/handlers/session.ts:289`。handler 返回的 data 是接收凭据，不是最终 assistant 回答。界面随后通过查询与事件流观察会话变化。

假设客户端给 U1 提交文本并指定 `resume:false`。核心先确认 S1 存在，查 U1 是否已经接收；若没有，再运行输入准备：触发 prompt hook，物化文件附件，准备显式技能内容。准备成功后才提交 inbox。这个次序避免文件根本打不开时，先留下一个看起来已接收、实际却无法执行的半成品。

注意 `uninterruptibleMask` 里的 `restore`：它没有把整个准备过程变成不可取消的大黑盒。附件/插件等准备可以响应取消；幂等检查、投影提交和唤醒的关键交接则避免被打断在不一致的中间点。N 还把 preparation、staged revert commit 与 admission 放在同一次 Location acquisition 里，保证投影能看到同一个实例配置。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/session/session.ts 155 188 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/session/session.ts`，L155–L188](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/session.ts#L155-L188)

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
          }).pipe(
            instances.provide(session),
            Effect.provideService(Database.Service, database),
            Effect.provideService(FSUtil.Service, fs),
          )
        }).pipe(
          Effect.catchTag("SessionInbox.LifecycleConflict", () => new PromptConflictError({ sessionID, messageID })),
        )
        if (input.resume !== false) yield* execution.wake(sessionID)
        return admitted
```

当 `resume` 省略或为 true，核心在接收后调用 `execution.wake(S1)`；为 false 就只接收。即使触发了 wake，HTTP 调用也不等整段模型工作结束。回到示例：U1 收据到达客户端时，它可能还躺在 `session_inbox`，模型历史里尚无这一条 user message。接收、交付、运行和完成是四个不同的可观察阶段。

## 5. 重复输入与 pending-only inbox

当前 user/synthetic 的幂等规则是 **first admission wins** 。相同输入 ID、相同 Session、相同类型的重试采用第一次内容；新的 payload、metadata 和 delivery 不覆盖第一次接收的版本。这不是“新 payload 必须完全相同才算重试”的旧契约。当前 inbox PATCH 只修改 `delivery`，不提供 pending 正文原地编辑。若要改一句话，应先取消尚未交付的旧 pending 项，再以新 ID 提交新文本；取消与交付可能竞争，必须处理接口结果。如果旧项已经进入可见历史，就不能假定取消成功，而应提交后续输入明确修正。

幂等检查会先查 inbox，再查已经投影的消息。输入已交付以后，inbox 行已被消费，U1 仍能从 user/synthetic 消息恢复已存在身份，因此不会因为“队列表里没有它”就再次入队。跨 Session 或跨类型复用同一个 ID 会冲突。compaction/move 等控制项还有各自规则，不应把 user/synthetic 的 first-wins 生搬到所有操作。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/session/inbox.ts 176 203 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/session/inbox.ts`，L176–L203](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/inbox.ts#L176-L203)

```typescript
  /** First admission wins for matching Session and type, without preparing a new payload. */
  const reconcile = Effect.fn("SessionInbox.reconcile")(function* <Type extends Item["type"]>(request: {
    readonly id: SessionMessage.ID
    readonly sessionID: SessionSchema.ID
    readonly type: Type
    readonly delivery: Delivery
  }) {
    const existing =
      (yield* find(db, request.id)) ?? (yield* promotedFromMessage(db, request.sessionID, request.id, request.delivery))
    if (existing === undefined) return undefined
    if (existing.type === "compaction" || !matches(existing, request))
      return yield* new LifecycleConflict({ id: request.id })
    return existing
  })

  const admit = Effect.fn("SessionInbox.admit")(function* <Type extends Item["type"]>(request: {
    readonly id: SessionMessage.ID
    readonly sessionID: SessionSchema.ID
    readonly item: Item & { readonly type: Type }
  }) {
    const existing = yield* reconcile({ ...request, type: request.item.type, delivery: request.item.delivery })
    if (existing !== undefined) return existing
    const admitted = yield* bus
      .publish(SessionEvent.InboxEnqueued, {
        inboxID: request.id,
        sessionID: request.sessionID,
        item: request.item,
      })
```

`session_inbox` 只存未消费工作：交付事件的 projector 删除该行，并在同一事务里插入对应的可见消息。它不是永久保留 admitted/promoted 两种状态的任务档案。客户端要显示已交付内容，应看消息投影；要显示待处理输入，应看 inbox。把两边相加而不按身份处理，容易把同一句话显示两次。

这还解释了为什么“取消队列项”和“中断正在运行的模型”不是一回事。U3 未交付时可以删除 pending 项，或改变它是 queue 还是 steer；但 U1 一旦交付就进入历史，取消执行不会自动删掉那段历史。Inbox cancellation 处理尚未成为对话事实的工作；interrupt 处理当前宿主正在推进的活动链。

## 6. 发布事件如何变成数据库记录

先定义两个术语。 **事件事实** 记录新的不可约信息，例如输入已入队、某工具已被调用。 **投影** 把这些事实变成便于查询的表，例如 session、session_message、session_part、session_inbox。模型和 UI 通常不每次从原始事件全量折叠，而是读取这些查询模型。

Bus 对同一 aggregate 的发布有本地串行约束，并在 SQLite immediate transaction 内执行 projector、本地 commit hook 和 sequence 更新。输入交付、可见消息插入、instruction state 的更新与 execution claim 等，都利用这类事务边界。事务成功之后才发出本地唤醒，避免消费者因一个尚未提交的通知读到旧状态。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/bus.ts 397 429 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/bus.ts`，L397–L429](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/bus.ts#L397-L429)

```typescript
                        const committed = {
                          ...event,
                          durable: { aggregateID, seq, version: durable.version },
                        } as Event.Payload
                        const route = yield* prepareRoutes([committed])
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
```

这里有个必须看清的 N 版本细节： **durable 事件定义与原始事件日志保留不是同一个开关** 。`Bus.configured` 的 `persist` 默认 false（同文件 208），上面 `if (persist)` 才把 payload 写进 EventTable。即使不保留原始事件，投影、sequence 和事务 hook 仍会提交，Session 与 inbox 并不会因此退回纯内存。

宿主可用 `events.persist` 覆盖这项配置；服务器 routes 会透传它，workerd 入口显式开启。因而不能说“所有默认本地 Session 都保留每条原始 durable event，随时可完整重放”。模型历史恢复首先依赖投影数据；原始 log/replay 能读到多少，则取决于这个宿主有没有保留那些事件。后面讨论 log API 时还会用到这个区别。

## 7. Coordinator：一个进程内，同 Session 谁拥有推进权

S1 的 wake 不直接等于“启动一个新 loop”。`SessionRunCoordinator` 按 key 维护一个进程内 Map，记录 owner Fiber、done Deferred、stopping 和 pendingWake。owner 是本次活动推进链；Deferred 是等待它完成的单次结果；pendingWake 像门铃，表示运行期间又有工作到达，需要结束前再查一次。

`resume` 与 `wake` 的语义不同。resume 是显式选择从历史继续：空闲时启动 force=true 的执行，已有活动执行时加入其 done 等待。wake 是“有新工作”的通知：空闲时以 force=false 启动；忙时只合并门铃，不会每敲一次就发一个并行模型请求。不同 Session 的 key 则可并发，没有全局“一次只能一个 agent”的锁。

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

为什么还要保留门铃？假设 owner 刚检查 inbox 为空、正准备退出，另一个请求接收了 U2。若只看 owner 存在就丢掉通知，U2 可能一直等到别的动作。coordinator 把这次 wake 记下来，循环或结算时再启动后继执行。门铃可以合并，因为完整待处理内容在数据库；它没有必要为每个输入保存一份内存队列。

这只是本进程的排他推进。另一个未协调的宿主若也打开同一数据库，不会因这张 Map 自动受到约束。Event replay 的 owner_id、恢复 claim 和未来分布式 execution lease 是不同事情。当前实现不能被描述为已经拥有多机器抢占、租约续期和 stale-owner fencing 的通用工作流引擎。

## 8. 取消先确认，清理后完成

interrupt 做的第一件事是尝试取得当前活动 owner 的中断意图：标记 stopping、清掉已属于旧意图的 pendingWake、对 owner 发 Fiber interruption，然后立刻返回是否接受。它不会让 HTTP 连接等待一个慢 shell finalizer 清理几秒钟。原 owner 退出后，资源 scope 与 settled hook 仍需完成；等待这个阶段使用 `awaitIdle`，公开 `Session.wait` 也走它。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/session/run-coordinator.ts 142 171 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/session/run-coordinator.ts`，L142–L171](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/run-coordinator.ts#L142-L171)

```typescript
    const interrupt = (key: Key, reason?: Reason): Effect.Effect<boolean> =>
      Effect.sync(() => {
        const execution = executions.get(key)
        if (execution === undefined || execution.stopping) return false
        if (execution.owner === undefined) {
          // Settlement window: the owner exited but the settled hook has not finished. The
          // terminal outcome is already decided, so no reason attaches — but the interrupt
          // still claims the recorded wakes so settle does not start a dead-intent successor.
          execution.pendingWake = undefined
          return false
        }
        execution.stopping = true
        // Wakes recorded so far belong to the interrupted intent; the interrupt claims them.
        // Wakes arriving during cleanup are new admissions and restart normally at settle.
        execution.pendingWake = undefined
        execution.interruptionReason = reason
        // Fire and forget: nobody benefits from waiting out cleanup here, and callers like
        // the interrupt endpoint must acknowledge immediately even when finalizers are slow.
        fork(Fiber.interrupt(execution.owner))
        return true
      })

    // One execution's `done` already spans coalesced continuations; re-check after it
    // settles to cover a successor execution started by a late doorbell.
    const awaitIdle = (key: Key): Effect.Effect<void> =>
      Effect.suspend(() => {
        const execution = executions.get(key)
        if (execution === undefined) return Effect.void
        return Deferred.await(execution.done).pipe(Effect.ignoreCause, Effect.andThen(awaitIdle(key)))
      })
```

`awaitIdle` 不是 join 某一个旧 Fiber 就算完。它在 done 后重新检查，覆盖清理期间新到的 wake 启动后继执行的情况。反过来，resume 遇到 stopping owner 也不会加入一个已经注定要取消的结果，而是等清理后再尝试启动新的显式执行。因此“取消请求返回了”和“会话现在绝对空闲”不能当同一个 UI 状态。

已登记的旧门铃会随中断清除，清理期间收到的新输入却仍可形成后继。带 `resume:true` 的中断还会按 steer/control 语义通知继续，queued 下一段工作不会因此被提前拉进来。这个选项可以用于“停止当前尝试，尽快响应新指令”；它不是永久封禁该 Session，也不是撤销此前所有外部副作用。

## 9. Runner 的安全边界：先准备，再交付，再读历史

拿到位置后，Location-scoped runner 负责真正的业务循环。`advanceToStep` 是进入下一 step 前的边界处理；`runStep` 负责本次逻辑模型调用的策略；`SessionStep.attempt` 负责一次具体流尝试和工具结算。遇到 move 或服务 reload，runner 可以返回 Moved/Reloaded，让 global execution 根据最新位置重新取得 runner，而不是继续在旧文件系统环境里跑。

在边界上，先等待插件 activation 稳定，选择当前 agent、权限、工具和 instruction sources，再建立或更新 InstructionState，之后才交付 eligible 输入。初始系统基线不可用时，U1 仍留在 inbox。否则可能出现“用户消息已经成为模型历史，但系统上下文尚不存在”的不可解释半状态。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/session/runner/llm.ts 184 200 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/session/runner/llm.ts`，L184–L200](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/runner/llm.ts#L184-L200)

```typescript
              const ready = yield* restore(
                Effect.gen(function* () {
                  const selected = yield* prepareContext(sessionID)
                  const promoted = yield* SessionInbox.promote(
                    db,
                    bus,
                    sessionID,
                    entering && !continuing ? promotable : "steer",
                  )
                  // A control admitted during context preparation owns this boundary.
                  if (promoted === undefined) return undefined
                  if (promoted > 0 && !selected.session.parentID && SessionTitle.isUntitled(selected.session))
                    yield* FiberMap.run(titles, sessionID, title.generate(sessionID), {
                      onlyIfMissing: true,
                    })
                  if (promoted > 0) step = 1
                  return { _tag: "Ready" as const, context: yield* context.load(selected) }
```

交付后重新 load 投影历史，模型才会看到 U1。`step=1` 表示重新开始计算当前 agent 的步数额度，不是只允许再走一步。一个边界交付多条 steer 只重置一次。若没有 eligible 输入、没有 continuation、也不是显式 force，runner 不会为了“也许还有事”无意义地调用模型。

N 还有一个可见的 idle hook：会话准备进入空闲时，可以把当前 outstanding background 情况交给 `session.experimental.idle`，插件可能接收新输入使执行继续。只做控制操作、从未跑过模型的 drain 会跳过它。这个 hook 没有把后台任务自动变成同一个前台 step；只是给会话从忙到闲的边界一个可扩展的观察点。

## 10. 用三个输入画一次完整时序

先提交 U1 并令 resume=false：S1 已存在，U1 在 inbox，messages 中没有 U1，active 中也可以没有 S1。接着显式 resume(S1)：coordinator 建 owner，runner 准备 instruction baseline，交付 U1，再读取历史并开始 step 1。假设模型决定读两个文件，同时发出两个 read 调用。

此时提交 U2“只看 Linux”，delivery=steer。接收事务成功，wake 只敲当前 owner 的门铃。U2 不会在半个模型 token 流中直接拼进已发送的 request；当前流和已启动工具先结算。下一安全边界才交付 U2、重读历史、重置 step 计数，并让下一请求包含这句约束。

再提交 U3“然后检查测试入口”，delivery=queue。只要当前工作仍需要模型根据 read 结果继续，U3 便保持 pending。直到 S1 将进入 idle，steer 优先处理，否则只交付一个 queue，再重新判断 continuation。它不是把所有排队用户输入一次性灌给模型，因而更接近“先完成当前段，再开始下一段”。

控制项又有一点不同：手动 compact 或 move 也走 inbox。边界会将控制项交付并发布对应的开始/移动事实；steered compaction 可以在同一控制边界内优先，但不会越过第一个 steered move。不要把它简化为所有类型一条绝对 FIFO，也不要把控制项当普通聊天文字展示后期待模型自己执行系统操作。

若调试时给事件顺序暂记 100、101、102，它们只表示 aggregate sequence 的单调关系，不表示每条用户消息固定产生几个事件。插件和控制操作可能增加事件；消息 ID 和 timestamp 也不能代替这个顺序。手工追踪应记录“哪个输入何时交付、属于哪个 assistant、工具何时结算”，而不是依赖 ID 字典序猜执行先后。

## 11. 模型并不直接读取数据库行：历史还要降低成请求

Session 历史是应用自己的类型化模型，含 user、assistant、synthetic、compaction、instruction 更新等。provider 接受的却是统一 AI Message 经过协议适配后的结构。`SessionContext.load` 解析当前模型，调用 `SessionHistory.entriesForRunner` 得到适合它的历史和 instruction initial，再由 `baseTranscript` 组装 system 与 messages。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/session/model-request.ts 99 118 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/session/model-request.ts`，L99–L118](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/model-request.ts#L99-L118)

```typescript
export const baseTranscript = (input: {
  readonly agent: Agent.Info
  readonly model: SessionRunnerModel.Resolved
  readonly tools: Tool.Snapshot
  readonly initial: string
  readonly messages: ReadonlyArray<SessionMessage.Info>
}) => {
  const providerMetadataKey = input.model.model.route.providerMetadataKey ?? input.model.model.provider
  return {
    providerMetadataKey,
    system: [
      input.agent.system
        ? input.agent.system
        : SessionSystemPrompt.make(input.tools.definitions.map((tool) => tool.name)),
      input.initial,
    ]
      .filter((part) => part.length > 0)
      .map(SystemPart.make),
    messages: toLLMMessages(input.messages, input.model.ref, providerMetadataKey),
  }
```

这里有两种稳定性要求。第一，工具定义与执行者必须一致：本次 request 持有的工具 snapshot 同时保存 advertised definitions 与 retained handler，后来插件修改 registry 不能让旧调用悄悄跑另一份实现。第二，provider 私有延续数据不是任意模型都能理解；换模型时不能把旧 reasoning/native metadata 生搬过去。降低层需要根据当前模型决定哪些内容可重放、哪些降为普通可见文本。

N 的 assistant 内容还可分存 `session_part`。`SessionHistory.withParts` 按 ordinal 把分表内容接回消息；旧内联内容有兼容读取路径。媒体可能先以 blob 引用存储，再在适当边界 hydration。所有这些都说明，“把一行 JSON 原样交给 LLM”不足以解释真实执行；查询模型、模型中立消息、provider wire request 是三层。

压缩也参与历史选择。普通本地 summary 可以形成历史边界；native compaction 保存模型特定的 replacement window，只有模型匹配且 window 能解码时才能用在该模型上下文里。对模型中立 UI、原始本地历史、具体目标模型的读取策略不同。切换模型时重新选择可重放边界，比无条件隐藏 summary 前所有记录更稳妥。

## 12. 工具“立即并行启动”与“后台工具返回”不是一回事

一次 attempt 先捕获用于 step diff 的起始快照，但把这项工作与 provider request 重叠，减少开始前延迟。模型输出完整 local tool-call 后，publisher 先完成 Tool.Called 发布，然后才 `forkScoped` 真正执行。先记录调用，再启动副作用，至少让后续历史知道“应用已经允许这次执行开始”。它仍不能原子地把 SQLite 与任意外部命令绑定成一笔事务。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/session/runner/step.ts 103 134 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/session/runner/step.ts`，L103–L134](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/runner/step.ts#L103-L134)

```typescript
    // Provider and tool fibers retain per-source order without a shared writer queue.
    // A local execution starts only after its Tool.Called publication completes.
    let overflowFailure: ProviderErrorEvent | undefined
    // Read to the end, not just the finish event, so the next request can reuse this response.
    const providerStream = llm.stream(input.prepared.request, input.prepared.options).pipe(
      Stream.runForEach((event) =>
        Effect.gen(function* () {
          if (overflowFailure || publisher.hasProviderError()) return
          // Wait here, where cancellation still works, rather than inside the uninterruptible publish.
          if (!publisher.hasStarted()) yield* Fiber.join(pendingStartSnapshot)
          if (
            LLMEvent.is.providerError(event) &&
            isContextOverflowFailure(event) &&
            !publisher.record().outputStarted
          ) {
            overflowFailure = event
            return
          }
          yield* publisher.publish(event)
          if (event.type !== "tool-call" || event.providerExecuted) return
          toolRuns.push({
            call: event,
            fiber: yield* Effect.uninterruptibleMask((restore) =>
              restore(executeTool(event)).pipe(
                Effect.flatMap(toolOutput.truncate),
                Effect.flatMap((outcome) => publisher.toolExecution(event.id, event.name, outcome)),
                Effect.catchTag("Tool.Error", (error) =>
                  publisher.failTool(event.id, toSessionError(error), error.metadata).pipe(Effect.asVoid),
                ),
              ),
            ).pipe(Effect.forkScoped),
          })
```

在 S1 的例子里，read A 的参数完整后就可以开始读 A；provider 流继续输出 read B，B 也随即开始。A 的结果甚至可以早于 provider 流结束持久化。这里的 eager 指“不等整个 provider 流结束再批量起工具”，并不是“父模型已经拿着 job ID 跑下一步”。真正的后台 subagent/shell 是工具内部先把长期工作交给 Job，再立即结算这一轮 tool result。

attempt 会继续读 provider stream 到结束，之后等待所有已启动的本地工具。中断时发出工具 Fiber 中断；结算事实需要在受保护的退出路径里完成，以免永远留下 running。只有这些结果已形成可读历史，runner 才能正常进入下一 step。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/session/runner/step.ts 140 161 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/session/runner/step.ts`，L140–L161](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/runner/step.ts#L140-L161)

```typescript
    // Keep the final tool and Step events uninterruptible, even when the work itself is cancelled.
    return yield* Effect.uninterruptibleMask((restore) =>
      Effect.gen(function* () {
        const stream = yield* restore(providerStream).pipe(Effect.exit)
        const streamFailure = Option.getOrUndefined(Exit.findErrorOption(stream))
        const streamInterrupted = Exit.hasInterrupts(stream)
        // Cancelled before the start snapshot existed: record nothing, as when the capture preceded the request.
        if (streamInterrupted && !publisher.hasStarted() && !pendingStartSnapshot.pollUnsafe())
          return yield* Effect.failCause(stream.cause)
        if (!overflowFailure && publisher.hasStarted()) yield* publisher.streamed()
        if (streamInterrupted) yield* interruptTools
        const joined = yield* restore(Fiber.awaitAll(toolRuns.map((run) => run.fiber))).pipe(Effect.exit)
        if (Exit.isFailure(joined)) yield* interruptTools
        const tools = classifyToolExits(joined, toolRuns)

        const overflow = overflowFailure ?? streamFailure
        if (
          !publisher.record().outputStarted &&
          isContextOverflowFailure(overflow) &&
          (yield* restore(input.recoverOverflow))
        ) {
          yield* Effect.logWarning("provider rejected the request as too long; compacting", {
```

providerExecuted 工具是另一条分支：执行在 provider 侧，OpenCode 不把同一个调用再派到本地 shell/插件。普通工具错误通常变成模型可见的工具结果，允许模型换一种做法；用户拒绝权限、问题被取消、宿主关闭等则可能停止这段执行。不能从“工具报错”一概推导为“整个 Session 永久失败”。

## 13. 重试：谁重发请求，谁承担已经显示的内容

N 的文本请求底层 `RequestExecutor` 默认是 executeOnce：发送 HTTP、读取响应、提取 Retry-After、把 HTTP/transport/provider 错误归一成 AIError。它的职责是报告事实与分类，不是在背后固定再发两次完整模型请求。`packages/ai/src/route/executor.ts:229–250` 直接把 execute 绑定到 executeOnce。某些 transport 会给“可以完整重发/需要轮换连接”的恢复提示；媒体轮询也有自己的读重试，都不能直接等同于会话 step 重试。

真正的 Session 策略在 `SessionRunnerRetry` 与 `ModelRetry`：是否可重试、还剩几次、等多久、是否允许插件修改决定，以及如何在历史里显示 retry 状态。它发布 RetryScheduled 后等待，因此 UI 可以解释“现在正在退避”，而不是只有一个迟迟不返回的网络请求。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/model-retry.ts 22 40 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/model-retry.ts`，L22–L40](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/model-retry.ts#L22-L40)

```typescript
// Exponential from 2s capped at 10s per gap, for 10 retries: 2, 4, 8, then 10 × 7, about 84s of
// waiting when every attempt fails (67–101s with jitter). `min` takes the faster schedule, so the
// cap applies per gap; `max` with `recurs` bounds the count.
const schedule = Schedule.max([
  Schedule.min([Schedule.exponential("2 seconds"), Schedule.spaced("10 seconds")]),
  Schedule.recurs(10),
]).pipe(
  Schedule.jittered,
  Schedule.setInputType<Input>(),
  Schedule.modifyDelay(({ input, duration: delay }) => {
    const minimum = retryAfter(input)
    const duration = minimum === undefined ? delay : Duration.max(delay, Duration.millis(minimum))
    return Effect.succeed(Duration.millis(Math.ceil(Duration.toMillis(duration))))
  }),
)

// A timed-out attempt already waited minutes before failing, so the general allowance would let a
// dead provider hold a call for most of an hour. Cap those attempts well below it.
const MAX_TIMEOUT_RETRIES = 3
```

默认退避从 2 秒指数增长，基础单间隔封顶 10 秒并加入 jitter，最多 10 次重试；timeout 单独最多 3 次。provider 的 Retry-After 可以提高等待，读取上限 15 分钟；retry hook 仍可调整/否决决定，所以 10 秒不是所有路径的硬等待上限，15 分钟也不是插件可设延迟的总上限。这组数字属于 N，不应背成所有历史版本永恒相同。

没有输出就失败时，可以按策略再尝试同一个逻辑 step。已经生成部分内容后失败，处理要更谨慎：先将已出现的 assistant/tool 状态结算，允许的情况下在等待后写 synthetic 继续提示，用新的 assistant ID 延续，避免把用户已经看见的部分当作从未发生。这里也检查工具拒绝与中断，不能无条件在工具可能产生副作用后整轮重播。

上限到达时，runner 保留工具 definitions 维持 provider cache prefix，设置 toolChoice none，并加 MAX_STEPS 提示。provider 若仍发本地调用，执行边界会拒绝它。连续 length 截断、context overflow、无输出连接恢复又各有独立分支。读重试代码时，应把“HTTP 一次发送”“attempt”“逻辑 step 的续接”和“进程重启恢复”写在四行，不要揉成同一个 retry 次数。

## 14. InstructionState：稳定前缀与后续变化同时保留

每一步都重新扫描指令不意味着每一步都改写最前面的 system prompt。V2 将 instruction sources 的观察值按 hash 保存，`initial_values` 表示当前 epoch 的稳定基线，`current_values` 表示已接收的最新状态；blob 保存对应内容。第一次完整观察建立初始基线，后来的 delta 形成按时间排列的 InstructionsUpdated 事实及冻结的呈现文本。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/session/instruction-state.ts 38 60 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/session/instruction-state.ts`，L38–L60](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/instruction-state.ts#L38-L60)

```typescript
export const commit = Effect.fn("InstructionState.commit")(function* (
  db: DatabaseService,
  bus: Bus.Interface,
  instructions: Instructions.List,
  observation: Observation,
) {
  if (!observation.initial && Object.keys(observation.delta).length === 0) return
  // The rendered text is frozen into the durable event because re-rendering it
  // later would require the original Location-scoped instruction sources.
  const text = observation.initial ? "" : yield* renderUpdateText(db, instructions, observation)
  yield* bus.publish(
    SessionEvent.InstructionsUpdated,
    {
      sessionID: observation.sessionID,
      delta: observation.delta,
      ...(text.length > 0 ? { text } : {}),
    },
    {
      // Initial sync establishes the baseline; unlike later deltas it is not chronological history.
      ...(observation.initial ? { metadata: { instructions: { initial: true } } } : {}),
      commit: () => insertBlobs(db, observation.blobs),
    },
  )
```

为什么同时保存 hash 和呈现文本？hash 让系统知道来源是否变了，避免把没变的内容反复塞进历史；冻结文本让以后不依赖“原机器此刻的 AGENTS.md 内容”也能理解当时的更新。首次事件只建立 baseline，不作为一条普通 chronological 更新；后来的消息才说明发生了什么变化。

来源按共享程度排序，工具目录、MCP/reference/skill guidance、项目指令、日期环境等形成较可复用的前缀。模型切换、插件重载和 Location 移动都会影响选择，但当前 step 已经发出去的 request 不会被中途改写。下一安全边界重新等待 activation 并读取来源，才决定后续请求该看到什么。

初始来源不可用时先停，不交付 pending 输入；已有来源暂不可观察时也不能随意当作“已删除”。压缩后推进 instruction epoch，可以把当前已接收值作为新基线，降低后续历史负担。这个 epoch 记录的是应用准备提供给模型的上下文，不证明远端 provider 已经成功收到；若进程在发送边界崩溃，dispatch ambiguity 仍然存在。

## 15. 压缩既有本地摘要，也有 native replacement window

手动 compact 不是 UI 给模型发一句“请压缩”。公开操作接收一个 inbox 控制项，runner 在安全边界交付它并发布 Compaction.Started，再执行专门压缩。自动压缩在每次 step 准备时检查估算与模型窗口；overflow 是 provider 已拒绝上下文后的独立恢复路径。配置关闭自动压缩时，自动与 overflow 路径都会跳过，手动仍可请求。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/session/compaction.ts 290 321 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/session/compaction.ts`，L290–L321](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/compaction.ts#L290-L321)

```typescript
    const compact = Effect.fn("SessionCompaction.compact")(function* (trigger: Trigger): Effect.fn.Return<Outcome> {
      const settings = state.get()
      const context = trigger.context

      // Only the user compacts when automatic compaction is off, overflow included.
      if (trigger.reason !== "manual" && !settings.auto) return { status: "skipped" }
      if (
        trigger.reason === "auto" &&
        !due(context, calculateCeiling(context.model.limit, settings.buffer), trigger.estimate)
      )
        return { status: "skipped" }
      const held = precomputed.get(context.session.id)
      precomputed.delete(context.session.id)
      const native = context.model.compaction?.type === "native"
      const model = yield* selectModel(context, settings.buffer, native)
      // The provider just rejected this context, so the estimate ran low; the first attempt already aims below it.
      const budget =
        trigger.reason === "overflow"
          ? Math.min(model.cap, Math.floor(total(trigger.estimate ?? estimatePrompt(context)) * SHRINK_STEPS[0]))
          : model.cap

      const selected = model.context === context ? trigger : { ...trigger, context: model.context }
      // The runner opened the manual compaction's message when it delivered the `/compact` item. An automatic one
      // opens here, before any wait, so a background summary still being written shows as a running compaction.
      if (trigger.reason !== "manual")
        yield* bus.publish(SessionEvent.Compaction.Started, { sessionID: context.session.id, reason: "auto" })
      const swapped = held ? yield* usePrecomputed(selected, held, budget, settings) : undefined
      const compaction = swapped
        ? Effect.succeed(swapped)
        : native
          ? compactNatively(selected, budget, settings.keep)
          : summarize(selected, budget, settings.keep)
```

本地 summarize 会选择摘要模型、保留合适的 recent 部分，把结果投影为 compaction 消息；native 路径让支持的模型提供 replacement window，并按模型身份约束以后重放。N 还支持可选 precompute：临近阈值时在后台预先摘要，但暂不替换当前前缀，等真正 due 时再尝试交换，以免每一步都破坏缓存。默认设置中 precompute 为 false，不能把存在该实现写成所有用户默认都在后台摘要。

一次普通 step 所谓“一次模型调用”也不代表压缩不另发请求：摘要是单独的任务类型，可以有独立请求、失败与 retry。压缩成功后重新准备历史和 instruction baseline；失败则留下明确 compaction 状态，而不是默默删除旧对话。阅读时把“减少将发送的上下文”和“删除数据库中的原始事实”分开，前者不自动意味着后者。

## 16. 实时事件、查询模型与 log API

界面想尽早显示文本，需要 text/reasoning/tool-input delta；重新连接想恢复可靠事实，需要已提交的消息/part 投影或保留下来的 durable log。这两种消费目标不同。实时 delta 不一定逐 token 留在原始日志，不能拿最后收到的 token 数当 durable sequence，也不能指望断线后重放每个曾看见的碎片。

当前 `Session.log` 包装 Bus.log，并只保留公开 Session durable event 与 log.synced marker。对应 HTTP 是实验端点 `/api/experimental/session/:sessionID/log`，不是旧的 history 分页接口。`follow:false` 读到捕获的水位后结束；`follow:true` 先回放，再接后续提交；两者都用 Synced 明确这次历史水位已追上。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/bus.ts 875 892 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/bus.ts`，L875–L892](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/bus.ts#L875-L892)

```typescript
              // Subscribing before the historical read means events committed during
              // replay either appear in the read or arrive through a post-marker wake.
              const wakes = input.follow ? yield* subscribeDurable(input.aggregateID) : undefined
              const target = yield* latestSequence(db, input.aggregateID)
              const marker: EventLog.Synced = {
                type: "log.synced",
                aggregateID: input.aggregateID,
                ...(target >= 0 ? { seq: Event.Seq.make(target) } : {}),
              }
              const replay: Stream.Stream<LogItem> = readThrough(target).pipe(Stream.concat(Stream.make(marker)))
              if (!wakes) return replay
              const live: Stream.Stream<LogItem> = Stream.fromSubscription(wakes).pipe(
                Stream.mapEffect(() => latestSequence(db, input.aggregateID)),
                Stream.filter((target) => target > sequence),
                Stream.flatMap((target) => readThrough(target)),
              )
              return Stream.concat(replay, live)
            }),
```

先订阅再读历史，是避免 handoff 丢事件的关键。历史读期间到来的提交，要么被这轮 query 包含，要么敲响 durable wake；wake 是容量为 1 的可合并脏信号，醒来后重新查 SQLite。可以合并的是通知，不是已保留的事件记录。fork aggregate 还可能继承一个 prefix，未知 event type 会被过滤，所以 sequence 有缺口不自动等于丢包。

务必再带上第 6 节的 retention 前提：Bus 默认不保留全部 raw payload，log 不能凭空重建未写入 EventTable 的内容。要开发事件同步或审计消费者，先确认宿主的 events.persist；只做 UI 重连则应同时理解 messages/context/inbox 等投影接口。原始日志可回放、投影已持久化、实时渲染已追上，是三个独立事实。

## 17. Execution claim：重启后为什么知道该继续谁

如果活动状态只有内存 Map，进程死亡后它便消失。当前实现为每个活动 busy period 写一个数据库 execution claim：Execution.Started 提交时同事务记录 claim，正常成功/失败/用户中断的 terminal 提交时释放；shutdown interruption 保留。在配置持久存储的宿主中，无论优雅退出有没有机会执行 hook，新进程都能从数据库发现尚未释放的执行意图；默认内存数据库的嵌入 SDK 不保留上一个进程的 claim。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/session/execution.ts 73 83 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/session/execution.ts`，L73–L83](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/execution.ts#L73-L83)

```typescript
    // Write-ahead claim: starting records the durable intent that a turn is in flight, in the same
    // transaction as the started event. Terminals release it — except shutdown interruption, which
    // preserves the claim so the next server start resumes the turn. A claim that survives with no
    // terminal is the signature of a process that died without teardown (crash, SIGKILL, eviction);
    // recovery is a property of the database, never of a shutdown hook that may not run.
    const claimOnCommit = (sessionID: SessionSchema.ID) => ({
      commit: () => store.claim(sessionID),
    })
    const releaseOnCommit = (sessionID: SessionSchema.ID) => ({
      commit: () => store.release(sessionID),
    })
```

claim 不是一条能替代全部运行事实的万能 run 记录，也不是分布式 ownership lease。它主要回答“这个前台执行在没有 terminal 的情况下消失了吗”。coordinator 仍在进程内管同 Session 的排他；恢复者仍要确认旧 owner 已死亡。受管理服务器通过注册锁和进程退出确认满足这个前提，任意两个自建服务共享数据库并不自动获得安全恢复资格。

恢复前先增加 durable resume_attempts，超过预算就发布失败并释放 claim。N 默认预算为 10；预算针对同一未完成执行，不是把某个 Session 过去成功使用十次以后永久禁用。正常 terminal 释放 claim 并重置计数。先计数再开始恢复，避免新进程在恢复代码内部再次崩溃时反复逃过预算。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/session/execution/restart.ts 78 99 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/session/execution/restart.ts`，L78–L99](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/execution/restart.ts#L78-L99)

```typescript
      const prepareResume = Effect.fnUntraced(function* (sessionID: SessionSchema.ID) {
        // Durable before the resume runs, so a crash inside the resumed turn is
        // counted by the next sweep and the budget cannot be dodged.
        const attempts = yield* store.countResume(sessionID)
        if (attempts === undefined) return false
        if (attempts > maxAttempts) {
          // Terminalize instead: the release hook clears the claim and resets the
          // counter atomically with the terminal event.
          yield* bus.publish(
            SessionEvent.Execution.Failed,
            { sessionID, error: RESUME_EXHAUSTED },
            { commit: () => store.release(sessionID) },
          )
          return false
        }
        yield* bus.publish(SessionEvent.Synthetic, {
          sessionID,
          text: CONTINUE_AFTER_SERVER_RESTART,
          description: "Continuing after restart",
          metadata: { notice: "restart" },
        })
        return true
```

启动恢复先读取持久后台通知项与 suspended Sessions，整理可恢复子会话，补齐旧 shell 等任务的结局，再继续相关会话。shell 进程不能被当作一个普通函数从中间恢复；重启时仍 running 的命令会报告 cancelled，不自动重新执行。后台子 agent 则可在父子身份和预算检查后恢复它的 Session。N 还对后台图像任务有结果通知恢复分支，细节放在对应工具章。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/session/execution/restart.ts 230 264 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/session/execution/restart.ts`，L230–L264](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/execution/restart.ts#L230-L264)

```typescript
      return Service.of({
        resumeSuspendedSessions: Effect.gen(function* () {
          const active = yield* execution.active
          const pending = yield* jobs.pendingBackground
          const children = pending.flatMap((background) =>
            background.status === "running" && background.recovery.kind === "subagent"
              ? [background.recovery.childSessionID]
              : [],
          )
          // Early notices wait for recovery's accounting, including Sessions that exhaust their budget.
          const suspended = new Set(
            [...(yield* store.listSuspended()), ...children].filter((sessionID) => !active.has(sessionID)),
          )
          yield* store.releaseChildClaims(children)
          yield* Effect.forEach(
            // Admit shell outcomes before a recovered child can start its first model request.
            pending.toSorted((a, b) => Number(a.recovery.kind === "subagent") - Number(b.recovery.kind === "subagent")),
            Effect.fnUntraced(function* (background) {
              if ((yield* jobs.get(background.id))?.status === "running") return
              const recovery = background.recovery
              if (recovery.kind === "shell") return yield* recoverShell(background, recovery)
              if (recovery.kind === "image") return yield* recoverImage(background, recovery)
              yield* recoverSubagent(background, recovery, suspended)
            }),
            { discard: true },
          )

          // Background completion can wake a parent, so inspect local ownership only after recovery.
          const resumed = yield* execution.active
          yield* Effect.forEach(
            (yield* store.listSuspended()).filter((sessionID) => !resumed.has(sessionID)),
            (sessionID) =>
              execution
                .resume(sessionID)
                .pipe(Effect.ignore, Effect.forkIn(scope), Effect.when(prepareResume(sessionID))),
```

这些能力是 at-least-once，不是“所有外部副作用只发生一次”。假设 shell 已改文件，但工具结果还没提交就崩溃，数据库无法单凭缺结果证明它没有执行。runner 会把遗留 streaming/running 工具结算为中断失败，不默默重跑同一条工具；恢复后的模型仍可能依据历史决定新的动作。实际业务若需要扣款、部署等 exactly-once，必须在外部系统使用自己的幂等键或事务协议，不能靠会话 claim 推断。

把 S1 再走一遍：服务器在 read 结果落库后、下一步模型发送前退出，旧进程内 owner 消失，数据库里结果与 claim 都在。新宿主扫描 claim，先增加恢复计数，接收“服务器已重启，请接续”的提示，再从投影历史准备模型请求；它不需要复活旧 Fiber。若退出发生在 shell 已改变外部文件而结果尚未提交之间，恢复逻辑仍能接续会话，却没有足够证据证明外部操作的准确终态。这两个窗口看起来都叫“服务器重启”，实际上能恢复的事实不同，设计上不能用同一个“自动重试所有工作”掩盖差异。

## 18. 十个常见难题，直接用边界作答

**1．HTTP prompt 已成功，为什么 messages 里暂时没有新问题？** 收到的是 inbox admission receipt。只有 runner 在安全边界交付后，它才成为可见 user message。先查 inbox、active 和错误状态，不要再用另一个 ID 重发相同工作来“催一下”。

**2．同一个 U1 换成另一句 text 重试，为什么没有更新？** user/synthetic 是同 Session+类型 first admission wins。重试不是编辑。该规则同时覆盖 pending 与已交付消息；想表达新输入应使用新 ID。

**3．两次 resume 会不会同时让 S1 产生两份回答？** 同一进程的 coordinator 会 join 同一个活动执行；不同 Session 则可并行。这个回答不自动扩展到任意两个共享 SQLite 的未协调进程，跨进程所有权是另一层。

**4．wake 连续收到十次，是不是调用十次模型？** 不是。它可以合并为门铃；runner 依据数据库里的 eligible 输入决定工作。没有新输入且不需 continuation 的非 force wake 不会凭空产生请求。

**5．queue 与 steer 都是输入，为什么不一起处理？** steer 表达对当前工作的及时约束，queue 表达下一段工作。steer 在安全 step 边界交付，queue 等当前 continuation 将结束才取一项。控制项另外有 compaction/move 的边界顺序。

**6．工具 Fiber 并行，为什么下一步模型还没开始？** 该 step 必须等所有已启动工具结算，下一次模型才有完整结果。真正后台工具先把长期工作登记到 Job，并把 running 身份作为当前 tool result 返回；两者不是同一种并行。

**7．interrupt 返回 true，为什么还能看见一小段输出或资源还在清理？** true 证明中断意图被活动 owner 接受，不证明 finalizer 已完成。需要无活动执行时另等 awaitIdle；同时别把清理期间新收到的输入误当成旧执行拒绝取消。

**8．既然有 durable event，为什么 log 中没有所有 token 或全部过去事件？** token delta 可以是 ephemeral，且 raw event retention 由 persist 控制，默认 false。投影持久化不等于保留每条原始 payload。先确认宿主配置与 log.synced 水位，再判断缺的是实时片段还是根本未保留的历史。

**9．已有自动压缩，为什么还要手动 compact 和 precompute？** 自动路径在窗口将满时执行；手动请求通过控制项在边界确定执行；precompute 可提前准备摘要，但默认不开启，也不立即改写前缀。三者的触发时机不同，最后都要留下明确压缩状态与可读历史。

**10．有 restart recovery，为什么仍不能保证命令只执行一次？** claim 记录执行意图，不能与任意文件、进程、网络副作用原子提交。恢复预算防止无穷重启，投影让模型知道历史，二者都不能抹去“副作用已发生、结果尚未落库”的不确定窗口。

读完本章，建议亲手在纸上追 U1/U2/U3：为每一步写出 inbox、可见历史、owner、当前 step、待结算工具和 claim 的值。能明确指出哪次事务改变哪一列，便已经抓住 V2 核心；之后学习子 agent 与 shell，只需在这条主线上增加 Job、child Session 和 OS 进程的生命周期。
