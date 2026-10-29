# 06 · 为什么要有 V2：从一个能工作的 agent，变成能被不同宿主管理的运行系统

你在 v1.2.24 中学到的核心模型依然成立：用户输入进入会话，模型产生文本或工具调用，工具结果写回历史，程序决定是否继续。V2 没有发明另一种“智能”。它重新安排的是这条链周围的责任：谁接收输入、谁拥有执行、哪个宿主提供能力、哪些事实需要保存、客户端断开或进程死亡后谁来收尾。

这一章固定比较三个提交：A 为 `c6262f9d4002d86a1f1795c306aa329d45361d12`（v1.2.24）；B 为 `055d95bb7e278c94baf06235a52cac79dd13ba67`（V2 推广前的 V1 主线末端，包版本 1.18.35）；N 为 `726cf096396d5014c3b1845e231cc01efe0421ee`（本次最新快照，根包版本 2.2.0）。不能用旧 2.1.3 的能力边界代替 N。

先说明证据等级。 **文档明确** ，指仓库说明直接写出的职责或设计选择； **实现推导** ，指我们沿调用链能解释的收益，不冒充作者宣言； **尚无测量** ，指代码提供了优化条件，但没有本章可据以报告的速度、成本或稳定性基准。仓库没有一份包办所有动机的“V2 宣言”；[规格索引](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/specs/v2/README.md#L5)反而要求按问题找权威：Protocol 管 HTTP，Schema 管公共形状，Core 管执行和持久化。

也要公平对待 V1。B 已经有 Effect 服务、Fiber 运行状态机、事件与投影设施、实验性后台 task、可选 native 模型适配器。把 V2 解释为“把 Promise 换成 Effect”“终于支持子 agent”“终于能持久化”，都会错过真正变化。

## 1. 问题：别的应用想嵌入 OpenCode，为什么要理解一整个 CLI 的启动过程？

在 A 中，`packages/opencode` 同时承载命令行入口、服务端、SessionPrompt、模型和工具。阅读很顺，但复用某段业务时也容易碰到它所假定的环境。在 B 中已经拆出若干 core 能力，传统 loop 却仍在应用包；插件初始化还会动态导入应用的 Server，并为插件构造客户端。见 [B 的插件启动](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/plugin/index.ts#L143)。这里不是“V1 完全不能嵌入”：它已有进程内 fetch 路径；问题是这条复用路径仍与应用 Server、Instance 和启动上下文纠缠。

设想你写一个 Slack 机器人，只想创建会话、注册团队工具、提交输入、接收结果。你不希望顺便开终端界面，也不希望自己拼齐某个私有运行时。V2 的回答是把 Core、Protocol、Server、Client、SDK 分开：领域执行不属于 TUI；API 契约不由每个调用方重新手写；宿主负责装配资源和关闭它们。 **文档明确** ，[SDK README](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/sdk/README.md#L3)说 SDK 在内存中执行 Server 已装配的路由，不开监听端口，也不增加网络跳转。

下面不是文档中的伪代码，而是 N 的 SDK 实现：生成客户端的 fetch 被接到当前 host 的进程内传输。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/sdk/src/effect/opencode.ts 42 59 -->
**真实源码 · N · V2 2.2.0** — [`packages/sdk/src/effect/opencode.ts`，L42–L59](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/sdk/src/effect/opencode.ts#L42-L59)

```typescript
  const host = yield* Effect.acquireRelease(EmbeddedHost.create(options, embed), (host) => Effect.promise(host.close))
  const httpClient = yield* HttpClient.HttpClient.pipe(Effect.provide(FetchHttpClient.layer))
  const client = yield* OpenCode.make({ baseUrl: "http://opencode.local" }).pipe(
    Effect.provideService(
      HttpClient.HttpClient,
      // FetchHttpClient reads Fetch at request time; callers must not replace this host's in-process transport.
      HttpClient.transformResponse(httpClient, Effect.provideService(FetchHttpClient.Fetch, host.fetch)),
    ),
  )

  return {
    ...client,
    sessions: {
      ...client.session,
      create: host.sessions.create,
      get: (input: { readonly sessionID: Session.ID }) => host.sessions.get(input.sessionID),
      move: host.sessions.move,
    },
```

这段代码值得停下来读。`acquireRelease` 把 host 的创建和关闭放到同一生命周期；`http://opencode.local` 是客户端构造请求使用的逻辑地址，实际请求由 `host.fetch` 接走。应用与 CLI 可以共享 Session 语义，而无需复制 agent loop。Promise 用户也不必先把应用全部改写成 Effect；[Client 的公开边界](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/client/README.md#L7)明确分出零 Effect 的 Promise 客户端和 Effect 客户端。

还有一个容易被“拆包”二字遮住的收益：HTTP 的字段、错误和返回形状有单一权威。比如 Session 接口增加 delivery 或改变错误类型时，服务端、Promise 客户端和 Effect 客户端不应该分别猜一遍。Client 文档明确说明生成器读取 Server 的具体 HttpApi，运行时只依赖客户端侧 Protocol 投影，并用生成等价性检查限制漂移。可观察的改变是 API 修改有可以检查的传播路径，而不是等某个 TUI 请求收到不认识的响应才发现两端不同步。代价是生成产物也成了维护对象，修改公共契约后必须更新并校验；类型系统不会替你部署客户端与服务端，也不会让所有旧客户端自动兼容新协议。

**可观察改善** 是调用方可以用一个有关闭语义的 host 运行会话，而不是依赖 CLI 已经启动成功。 **代价** 是包边界、生成客户端和服务装配更多；“一个函数在哪个包”比 A 难找。它解决复用和生命周期契约，不保证每次本地方法调用都更省 CPU。内存路由仍做请求构造、校验和响应转换，不能把“不经过 TCP”误写成“没有任何传输抽象开销”。

## 2. 问题：同一套会话逻辑能否换宿主，而不是到处判断有没有 Bun？

B 的正常开发入口仍通过 Bun 运行应用包，但 Node 适配和 Effect 化已经存在。V2 的进一步变化是把“这台宿主拥有什么能力”变成装配决定：数据库、进程启动器、文件系统、PTY 等可以替换；会话执行不应在每个分支都硬编码一种机器。 **文档明确** ，[Workerd profile 的实现注释](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/server/src/workerd.ts#L19)要求在 Cloudflare Durable Object 内启动 core/server，并替换或禁用本地专属能力。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/server/src/workerd.ts 75 88 -->
**真实源码 · N · V2 2.2.0** — [`packages/server/src/workerd.ts`，L75–L88](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/server/src/workerd.ts#L75-L88)

```typescript
/** The workerd replacement graph, applied after the standard server replacements. */
export function replacements(options: Options): LayerNode.Replacements {
  return [
    Database.node.replace(Database.configuredClient(sqliteLayer({ storage: options.storage }))),
    CrossSpawnSpawner.node.replace(EnvironmentUnavailable.layer),
    Snapshot.node.replace(Snapshot.noopLayer),
    Vcs.node.replace(vcsLayer),
    FileSystem.node.replace(fileSystemLayer),
    FileSystemSearch.node.replace(fileSystemSearchLayer),
    Pty.node.replace(ptyLayer),
    // Precompiled (internal and SDK) plugins only: no plugin-directory scan, npm
    // install, or import of plugin code from disk.
    ConfigPluginSource.node.replace(ConfigPluginSource.empty),
  ]
```

`Database.node.replace` 接入注入的 Durable Object SQLite；进程启动器被替换为明确的环境不可用实现；磁盘插件发现关闭，只保留打包进宿主的插件。此处真正有价值的不是 API 名叫 `replace`，而是 **缺能力的地方能在一个边界集中表达** 。同一套 Session 请求进入不同 profile 时，领域身份仍相同，外部资源的提供方式不同。

例如某应用只需要在 Durable Object 中管理消息、输入和模型调用，它不应为了初始化 Session 而加载本机的 PTY 原生模块。相反，若它要求执行 `git status`，不能因为 V2“支持 workerd”就假装能在隔离环境里启动系统 shell。裸 workerd profile 没有本地执行平面，文件系统和 PTY 等操作会明确失败；接上远程 sandbox 是另外一层工作。

**实现推导的收益** 是减少宿主假设渗透，并使能力缺失更可诊断。 **代价** 是必须维护 profile、条件导出、打包和平台测试；某个 Node 入口能用，不代表所有 Bun 插件都能原封不动搬过去。“跨运行时”也不是“所有功能在所有运行时等价”：这是依赖替换契约，不是一次性抹平操作系统差异。

## 3. 问题：插件增加、禁用或重载后，为什么旧贡献还留在系统里？

A、B 都有插件 hooks；B 的 `trigger` 甚至已经根据 `Hooks` 推导参数类型，见 [B 的类型接口](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/plugin/index.ts#L41)。所以“V2 才有类型安全插件”不是准确说法。更深的问题是：插件贡献的不只是一次回调，还包括 provider、model、agent、tool 等有状态目录；插件卸载时，谁撤回这些贡献？如果只对现存对象做原地修改，撤销后应该恢复成哪个值？

N 把运行 hooks 和目录 transforms 分开。运行 hooks 拦截某次 request、permission、tool 等操作；transform 从基础值按注册顺序重建目录。 **文档明确** ，[Effect 插件说明](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/plugin/src/effect/README.md#L26)把注册归属到插件 Scope，关闭 Scope 自动移除，也支持提前 dispose；同文明确已读出的旧值不会被新重建原地修改。

下面截取运行 hook 的注册：

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/plugin/hooks.ts 69 84 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/plugin/hooks.ts`，L69–L84](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/plugin/hooks.ts#L69-L84)

```typescript
    const register: Interface["register"] = Effect.fn("PluginHooks.register")(
      function* (domain, name, callback, options) {
        const scope = yield* Scope.Scope
        const id = key(domain, name)
        let active = true
        const entry = { callback, options }
        callbacks.set(id, [...(callbacks.get(id) ?? []), entry])
        const dispose = Effect.sync(() => {
          if (!active) return
          active = false
          const next = (callbacks.get(id) ?? []).filter((item) => item !== entry)
          if (next.length === 0) callbacks.delete(id)
          else callbacks.set(id, next)
        })
        yield* Scope.addFinalizer(scope, dispose)
        return { dispose }
```

`entry` 既加入 callbacks，也把自己的删除动作挂到 Scope finalizer。作用域不是装饰：它回答“谁拥有这次注册，何时失效”。对应的 [State 重建](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/state.ts#L191)每次从 `initial()` 开始依次应用活跃 transforms，而不是拿已经被上一个插件改过的对象继续修修补补。

举例说，公司策略插件暂时移除昂贵模型。关掉它以后，下一次目录重建可以从基础模型列表恢复，而不是要求插件记住删过的每个对象、再反向补回。插件配置和模型目录变化能各自更新，不必把“配置改动”全都解释为“销毁整个项目运行时”。这正是 [历史决策记录](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/specs/v2/catalog-config-plugin-lifecycle.md#L3)中选定的 Location-scoped 可重放 transforms 的方向；记录中的旧接口草图不能当成 N API。

**代价与限制** 同样具体：注册顺序具有语义，transform 必须同步且可重放，异步数据应先取得再注册或 reload；finalizer 能移除内存贡献，不能撤回插件已经发出的外部请求。typed hook 也不自动构成权限沙箱，可信插件仍有宿主授权的能力。

## 4. 问题：用户已经点击发送，为什么“已收到”“已排队”“模型已看到”像同一件事？

B 的 [SessionPrompt.prompt](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/session/prompt.ts#L1052)先写 user message，再根据 `noReply` 决定返回还是进入 loop。这对于一问一答很直观；但模型正在工作时，第二个客户端发来“先别改数据库”，UI 还需要管理排队输入的取消和交付模式，后台任务又送来完成通知——这些都不是简单再加一个 `await` 就能把时序讲清的。

V2 把接收输入建模为 inbox 命令，执行只是后续消费者。N 同时处理 user、synthetic、compaction、move 等输入类型；`resume:false` 表示记录但不唤醒执行，不等于输入不保存。`steer` 默认在安全 Step 边界交付；`queue` 等当前执行将空闲时再按规则交付。模型不会在一半 tool 参数里突然读到新输入。

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

看这里三道门：先 reconcile 身份，成功重试直接复用；首次输入才执行可取消的 prepare；admit 完成以后才决定 wake。这样网络丢了响应、用户重试同一个请求时，不必重做附件解析和插件准备。N 的语义是 **同 Session、同类型、同 ID 首次接收胜出** ，后续 payload 不覆盖首次输入；不是旧切片里“必须逐字相同才幂等”的契约。N 的 inbox PATCH 只接受 `delivery`，用来切换 steer/queue，不提供原地修改正文的接口；若要替换排队文本，应先取消原 pending 项，再以新 ID 提交新文本，不能重复使用请求 ID 偷换内容。取消与交付可能并发发生，调用方仍需核对返回与投影状态，不能把这两步当成原子的“替换”。

**可观察改善** 是 UI 能区分待发送、服务已接收但未交付、模型历史中已交付的输入。断开的是 HTTP 连接，输入所属的是 Session；重连后可查投影中的消息和 pending inbox，而不是用某个失联 Promise 猜状态。模型 loop、TUI 和 SDK 的发送行为因而能遵循同一组规则。

这里必须区分持久状态和完整事件日志。N 的 [Bus 默认 `persist:false`](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/bus.ts#L208)；投影、序号和 commit hook 在事务中更新，但 [原始 EventTable 只在 persist 为真时写入](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/bus.ts#L402)。因此不能推断“所有默认宿主都保留完整事件，可无限重放每个历史 token”。live delta 也不是持久回放内容。UI 重连读当前状态，与开启保留后按游标回放原始事件，是两个承诺。

再看一个具体时序：用户排队“重构登录”，随后希望改成“只解释登录”，此时当前模型仍在执行上一条任务。N 当前能做的是取消旧 pending 项，再用新 ID 提交替代输入；[inbox update 的 payload](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/protocol/src/groups/session.ts#L571)只有 delivery，不能直接 PATCH 正文。取消接口对已经不可用的项是 no-op，因此若旧输入恰好已被交付，客户端必须据投影反馈说明它已不能撤回；新输入只会影响后续边界，不能让已发出的模型请求倒流。因此输入身份、pending 取消或模式更新、交付确认、可见历史必须分开建模。共享协议提供这些基本操作，却没有替客户端完成正文替换的并发交互设计。

**代价** 是 inbox、投影和交付规则多了一层。一次 prompt 返回成功只表示接收契约成立，不能替代执行完成或模型成功；调用方要明确自己是在等待 admission、某段执行结束，还是最终业务结果。

## 5. 问题：停止、关闭服务和崩溃，为什么都不能只叫 cancel？

A 的活跃状态主要是 AbortController 和 callbacks；B 已用 `SessionRunState`、Runner、Fiber 管理，也会取消后台工作。见 [B 的 runner Map](https://github.com/gaoch023/opencode/blob/055d95bb7e278c94baf06235a52cac79dd13ba67/packages/opencode/src/session/run-state.ts#L35)。这些改进解决正常运行中的协作取消，却不能让进程死后仍存在的“执行意图”自动可识别：内存 Map 没了，数据库里的最后一条 assistant part 不能说明宿主是主动停止、正常关闭，还是在写结果前被 SIGKILL。

N 为执行忙碌期写入 claim。开始事件和 claim 同事务提交；成功、失败、用户中断释放；服务关闭导致的中断保留。 **文档明确** ，这不是要求旧进程在临终前完成清理，而是让数据库留下“这段工作尚未得到释放”的证据。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/session/execution.ts 109 134 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/session/execution.ts`，L109–L134](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/execution.ts#L109-L134)

```typescript
    const coordinator = yield* SessionRunCoordinator.make<SessionSchema.ID, SessionRunner.RunError, InterruptReason>({
      started: (sessionID) =>
        reportLifecycle(
          sessionID,
          bus.publish(SessionEvent.Execution.Started, { sessionID }, claimOnCommit(sessionID)),
        ),
      drain: (sessionID, force, promotable) => drain(sessionID, force, undefined, promotable),
      // One terminal observation per busy period, covering every coalesced drain.
      settled: (sessionID, exit, reason) =>
        reportLifecycle(
          sessionID,
          Effect.gen(function* () {
            const outcome = terminal(exit, reason)
            if (outcome.type === "succeeded") {
              yield* bus.publish(SessionEvent.Execution.Succeeded, { sessionID }, releaseOnCommit(sessionID))
              return
            }
            if (outcome.type === "interrupted") {
              // Deliberate stops release the claim; shutdown keeps it for restart continuity.
              if (outcome.reason !== "shutdown") yield* jobs.cancel(sessionID)
              yield* bus.publish(
                SessionEvent.Execution.Interrupted,
                { sessionID, reason: outcome.reason },
                outcome.reason === "shutdown" ? undefined : releaseOnCommit(sessionID),
              )
              return
```

`claimOnCommit` 和 `releaseOnCommit` 的定义就在同文件 78～83 行，分别调用 store.claim/release。`outcome.reason === "shutdown"` 不释放 claim，是重启连续性的设计选择；用户点击停止通常不该被下次启动当成“未完成任务请继续”。当前 interrupt 的 API 接受中断后即可返回，finalizer 和终态结算在执行 Fiber 中继续；需要等清理结束的调用方应使用 `awaitIdle`，不能把“停止按钮已收到响应”当成“所有子进程已确认退出”。

重启扫描还带持久尝试次数，超过预算会终止，避免某个会话反复把服务拖入恢复循环。见 [SessionRestart](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/execution/restart.ts#L78)。它会先处理遗留工具状态，再从投影历史继续，而不是直接重放上次 shell。后台 Job 也区分活跃 registry 与持久通知元数据：重启时仍显示 running 的 shell 会被按取消处理并补通知；子 agent 在身份和预算检查后才可能恢复。当前独立 PTY daemon 及受控服务交接又有自己的存活语义，不能把所有终端和 Job 都归结为“重启全死”或“全部续跑”。

**改善** 是正常停止、服务重启和无清理死亡有不同收尾策略，后台任务的完成通知不再只依靠一个仍活着的回调。 **边界** 是 at-least-once 恢复：外部工具可能已改文件、外部模型可能已接收请求，只是本地来不及记录。claim 不提供 exactly-once；恢复指令“不要重复已完成工作”也不能神奇证明副作用是否发生。托管服务必须保证旧 owner 已死亡再恢复，未注册且共享数据库的服务不能随意扫描别人的 claim；这还不是通用分布式租约与 fencing 协议。

## 6. 问题：两个项目或两组应用工具，为什么会互相污染配置？

V1 已用 Instance 隔离项目状态，不能说 V2 才知道“项目隔离”。V2 进一步把隔离对象显式列进服务图：Location 上有文件系统、权限、插件、模型目录和 runner；宿主上共享数据库、执行协调等。执行从 Session ID 开始，先读当前 placement，再取得该 Session 的能力，不应把某个创建时的目录永久偷偷捕获进 Session 对象。

这个边界还能被 SDK 应用细化。比如同一代码目录里有两个 Slack thread，一个允许调用工单系统，一个注册发布工具；仅按 directory 共享所有插件就太粗。N 允许应用从 Session metadata 选择稳定的 instance key，并与 canonical Location 一起作为缓存身份。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/sdk/src/internal/instances.ts 54 75 -->
**真实源码 · N · V2 2.2.0** — [`packages/sdk/src/internal/instances.ts`，L54–L75](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/sdk/src/internal/instances.ts#L54-L75)

```typescript
      const key = (session: Session.Info) => ({
        key: options.key(session),
        ...LocationServiceMap.canonical(session.location),
      })
      const provide = (session: Session.Info) => Effect.provide(instances.get(key(session)))
      const instances: LayerMap.LayerMap<
        ReturnType<typeof key>,
        Instance.Services,
        Instance.Error
      > = yield* LayerMap.make(
        (input: ReturnType<typeof key>) =>
          Layer.unwrap(
            Effect.gen(function* () {
              const configuration = yield* options.configure(input.key).pipe(Effect.orDie)
              return Instance.layer(input, {
                plugins: configuration.plugins,
                replacements: [
                  ...replacements(),
                  // Instances borrow this selector and the host's Location map instead of retaining
                  // their Layer scopes; retaining the selector would block its shutdown on its own entries.
                  Instance.node.replace(Layer.succeed(Instance.Service, { provide })),
                  LocationServiceMap.node.replace(Layer.succeed(LocationServiceMap.Service, locations)),
```

同 key、同 Location 复用能力；换目录或 workspace，即使 key 相同也不共用。`configure` 在缓存未命中时构建一次，不是每次 prompt 都重装插件；构造失败不能悄悄退回另一个实例。宿主关闭时对应 Scope 回收。这个设计把“会话数据是什么”和“本次在什么能力集合下执行”拆开，移动后的会话也能重新选择能力。

**可观察改善** 是多项目 host 与嵌入应用能控制共享粒度，避免靠全局可变对象切换当前目录。 **代价** 是实例缓存会保留资源，配置更新与实例出生参数不是同一个机制；函数必须由应用在重启后重建，不是序列化进数据库。尤其要牢记 [SDK 文档的限制](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/sdk/README.md#L63)：instance selection 不是授权边界或存储隔离边界，不能把不同 key 当成已经完成了多租户安全。

## 7. 问题：模型调用、工具 loop 和网络重试，谁才有权决定“再来一次”？

A、B 的主线容易记：SessionPrompt 决定续轮，processor 消费流，LLM 调 AI SDK，工具作为 execute 回调交给模型流路径。B 又加入可选 native runtime，所以新旧传输实现可能同时服务同一条传统 loop。V2 要解决的是业务执行策略不应被某个 provider SDK 的内存工具 loop 隐式接管：何时新建 assistant、何时交付输入、工具何时持久结算、失败是否算一个新 Step，都应由 Session 层明确决定。

N 的 [SessionStep](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/runner/step.ts#L103)直接调用一次 `llm.stream`；完整 local tool-call 发布成功后才 fork 工具，provider stream 关闭后等待已启动的工具结算。一个逻辑 Step 可以因为重试产生多个物理 attempt，所以不要把“每个 Step 恰好一个 HTTP 请求”写成不变量。`@opencode/ai` 可以给普通应用提供自己的内存 tool loop，但 Session runner 不用它代替持久化编排。

新 AI 包把 provider-neutral request/event、协议 lowering、endpoint/auth、HTTP 或 WebSocket transport 拆开，也保留 AI SDK 适配路径。收益是相同 Session 规则可以覆盖更多模型路由，provider 差异留在明确的转换层；不表示支持的每种模型都有相同能力，更不表示消除了所有兼容代码。

N 当前的默认 HTTP executor 很克制：发送一次并分类错误，重试交给上层 Step 策略。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/ai/src/route/executor.ts 229 250 -->
**真实源码 · N · V2 2.2.0** — [`packages/ai/src/route/executor.ts`，L229–L250](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/ai/src/route/executor.ts#L229-L250)

```typescript
export const layer: Layer.Layer<Service, never, HttpClient.HttpClient> = Layer.effect(
  Service,
  Effect.gen(function* () {
    const http = yield* HttpClient.HttpClient
    const executeOnce = (request: HttpClientRequest.HttpClientRequest, middleware?: HttpMiddleware) =>
      Effect.gen(function* () {
        if (!middleware)
          return yield* http.execute(request).pipe(
            Effect.mapError((error) => httpError({ error, request, operation: "request" })),
            Effect.flatMap(statusError),
          )

        const response = yield* middleware(request, (input) =>
          http
            .execute(input)
            .pipe(Effect.mapError((cause) => (cause instanceof Error ? cause : new Error(String(cause))))),
        ).pipe(Effect.mapError((error) => httpError({ error, request, operation: "request" })))
        return yield* statusError(response)
      })
    return Service.of({
      execute: executeOnce,
    })
```

为什么这比“底层也重试两次、上层再重试四次”更容易推理？上层能看到是否已经有持久输出、工具是否执行过、provider 是否可能收到请求，从而决定继续同一步、补一条继续指令，还是终止。若底层偷偷重复发送，UI 只显示一次 retry，实际尝试次数和副作用风险却被放大。这里改善的是 **策略归属与可观察性** ，不是宣称“一次请求一定只执行一次”。网络超时仍可能发生在对方已接受之后，ambiguous delivery 仍需显式处理。

Session 的 WebSocket 通道还可以复用连接、维护 checkpoint，并在连续流失败后回退 HTTP。参见 [model-transport](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/model-transport.ts#L186)。这是可减少重复握手、支持增量续接的条件；token 推理速度由 provider 决定，不能从出现 WebSocket 就推出首 token 延迟或整体吞吐一定改善。

## 8. 问题：每轮都重拼系统提示，怎样知道缓存稳定、上下文变化又没有丢？

V1 已经做系统提示、历史裁剪、压缩和 provider 缓存标记；V2 的价值不是第一次想到这些优化。新的问题是，当 agent、技能、AGENTS.md、项目环境随运行改变时，怎样既保留稳定请求前缀，又让模型准确知道变化发生在哪一轮？直接改掉最前面的 system prompt 可能破坏缓存前缀；每轮重复追加全部说明则会浪费上下文，还模糊旧指令是否已经被替换。

N 把指令源读成结构化值，用内容哈希存储 blob 与当前状态；首次完整观察形成 epoch 基线，后续只在发生变化时发布 delta，并把需要的更新文字冻结为按时间排列的 System 消息。这里说的是 N 的 instruction-state，不是旧切片那套可直接套用的 Context Epoch 表或 registry API。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/core/src/session/instruction-state.ts 38 61 -->
**真实源码 · N · V2 2.2.0** — [`packages/core/src/session/instruction-state.ts`，L38–L61](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/core/src/session/instruction-state.ts#L38-L61)

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
})
```

代码里没有 delta 就返回；initial 和后续变化采用不同表示；blob 与状态变化通过发布事务一起提交。这解决两个问题：重放或重组历史时，不用回到已经改变的工作目录重新“猜”旧指令；模型请求可以使用同一 epoch 内稳定的起始值，将真正的变更放到时间线上。不可用与确认删除也必须区分，否则一次短暂读取失败就会错误撤销安全指令。

更底层，AI 包提供可配置 cache policy，而不是让每条调用链自行拼 provider 私有字段：

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/ai/src/cache-policy.ts 17 35 -->
**真实源码 · N · V2 2.2.0** — [`packages/ai/src/cache-policy.ts`，L17–L35](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/ai/src/cache-policy.ts#L17-L35)

```typescript
const AUTO: CachePolicyObject = {
  tools: true,
  system: true,
  messages: { tail: 1 },
}

const NONE: CachePolicyObject = {}
const BREAKPOINT_CAP = 4

// Resolution rules:
//   - undefined   → "auto" — caching is on by default.
//   - "auto"      → tools + first/last system + final message boundary.
//   - "none"      → no auto placement; manual `CacheHint`s still flow.
//   - `{ ttl }`   → "auto" with that lifetime.
//   - object form → exactly what the caller asked for.
const resolve = (policy: CachePolicy | undefined): CachePolicyObject => {
  if (policy === "none") return NONE
  if (typeof policy === "object" && !isAuto(policy)) return policy
  return AUTO
```

`auto` 把稳定工具、system 和近期对话边界暴露给支持的协议。 **实现推导** ：当内容稳定且满足 provider 的缓存门槛时，重复前缀更可能被复用。 **不能推出** ：总 token 数必然下降、所有 provider 都省钱、摘要无损。OpenAI/Gemini 的隐式缓存并不依靠相同的 inline markers；缓存命中也可能只是以较低计价复用同样长的输入。压缩会再调用模型并丢弃部分活动上下文，可靠性收益与信息损失必须一起评估。

因此若要回答“V2 究竟省了多少”，需要同模型、同工具、同任务的对照，记录 cache read/write、input/output tokens、重试次数、工具耗时、摘要质量和实际账单。仅凭文件名含 cache 或看到更短 prompt，不能发布一个百分比结论。

## 9. 一个能明确解释的性能改进：多窗口不该把同一事件编码 N 次

不是所有提升都只能泛谈“模块更清楚”。[事件流决策记录](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/specs/v2/event-stream-architecture.md#L31)明确比较过：此前每个 `/api/event` 连接各自过滤、schema encode、JSON stringify、SSE framing；连接越多，同一事件重复编码越多。N 的 Server-scoped feed 共享一次编码结果，再给每个连接独立队列。

<!-- EXCERPT 726cf096396d5014c3b1845e231cc01efe0421ee packages/server/src/event-feed.ts 48 70 -->
**真实源码 · N · V2 2.2.0** — [`packages/server/src/event-feed.ts`，L48–L70](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/packages/server/src/event-feed.ts#L48-L70)

```typescript
  const publish = Effect.fnUntraced(function* (event: Event.Payload) {
    if (!isOpenCodeEvent(event)) return
    let encoded: string | undefined
    for (const [subscriber, types] of subscribers) {
      if (types && !types.has(event.type)) continue
      encoded ??= yield* Effect.try({
        try: () => render(event),
        catch: (cause) => new EncodingError({ eventID: event.id, eventType: event.type, cause }),
      }).pipe(
        Effect.catch((error) =>
          Effect.logError("Failed to encode public event", {
            eventID: error.eventID,
            eventType: error.eventType,
            cause: error.cause,
          }).pipe(Effect.andThen(fail(error)), Effect.as(undefined)),
        ),
      )
      if (encoded === undefined) return
      if (Queue.offerUnsafe(subscriber, encoded)) continue
      subscribers.delete(subscriber)
      Queue.failCauseUnsafe(subscriber, Cause.fail(new SubscriberOverflowError({ capacity })))
    }
  })
```

注意 `encoded ??=` 位于遍历连接内部，同一事件只有真正需要发送时才编码，随后复用字符串；某个队列放不进去，只删除并失败该订阅者，其他连接继续。这同时回答效率和故障范围：慢窗口不该反过来阻塞 Core 发布，也不该拖住其他窗口。它是 V2 内部继续演进的一项具体优化，不能全部记成“V1 到 V2 当天才有”。

这里能证明编码工作由“每个接受该事件的连接一次”变成“该事件最多一次”；不能仅从源码断言服务器整体快了几倍。队列仍需逐个 offer，HTTP、TLS、代理、客户端缓冲也不由这段代码消失。事件数容量更不等于完整字节内存上限；超限断开后客户端还需要刷新投影，live stream 本身没有变成可靠消息总线。

## 10. 默认 CLI 迁移之后，为什么这已经不只是实验性库？

历史上的 V2 与 V1 一度并存在同一 dev 树，根 `bun dev` 仍跑旧应用包；那时“看到 core/session 就认为正常 TUI 已用新 loop”会误判。`5caf1f9f6f5f30e13fc50a788ad953d97ae3748f` 的推广提交明确把 V2 树提升为 dev。N 的 [根 package.json](https://github.com/gaoch023/opencode/blob/726cf096396d5014c3b1845e231cc01efe0421ee/package.json#L5)现在是 2.2.0，`dev` 到 `packages/cli`；旧 `packages/opencode` 已不在这棵树中。

这意味着开发者日常调试、命令行运行、宿主 SDK 与 UI 开始共同依赖新边界。CLI 是接入和服务管理的一层，Core 才负责 Session 行为；当前主线也不能再套用旧 TUI Worker 后端的进程图。服务进程、终端 daemon、UI 连接各有生命周期，退出一个界面与取消某个 Session 是不同操作。

**改善** 是同一套能力真正进入默认工作流，避免新架构永远停在旁路 demo。 **代价** 是迁移成本会落到用户：配置、插件 API、模型路由、历史导入都可能存在兼容点。推广不是“以后没有回归”的证明。判断生产稳定性需要看运行记录、回归测试和工作负载，而不是只看版本号、Effect 类型或包拆分数量。

## 把五个最容易问偏的问题答具体

**为什么不继续修 V1？** 可以继续修，而且 B 正是持续修补后的结果。单个 bug 未必值得重写。但如果同时要求进程内 SDK、Durable Object、可撤销插件贡献、独立输入协议、跨重启恢复，继续在原应用 loop 周围追加特殊分支，会让一个位置同时承担越来越多宿主与生命周期责任。V2 选择改边界以承接这组需求；这是从实现能解释的工程取舍，不是已证明“重构一定比增量维护便宜”。它也引入了自己的复杂度和回归面。

**是不是更快、token 更少？** 目前只能逐项说：共享事件编码减少了重复编码；连接复用减少了重复建连的机会；稳定前缀与 cache policy 提供缓存复用条件；压缩限制活动历史。工具等待、模型推理和网络仍可能占大头。没有统一基准，就不能把这些机制加总成“V2 更快且更便宜”；可靠恢复和更多元数据也会增加数据库写入、检查与调度开销。

**是不是分布式 agent 系统？** 当前执行协调仍是 process-local；Location、workspace、HTTP 与远程能力提供放置和接入边界，不等于已有跨节点排他所有权。持久 claim 服务于受控重启连续性，不能当通用租约。两个活进程直接共享数据库、各自恢复同一 Session，不属于凭一张 claim 表就能正确处理的场景。

**是不是生产一定更稳？** 对若干故障有了更明确协议：输入重试先查 admission；工具先记录调用再执行；取消和关闭区别对待；claim 提供恢复依据；后台工作有补通知；慢订阅者独立失败。这些机制缩小了某些不确定性，但外部副作用、宿主错误、插件故障和 provider 行为仍可能出错。尤其持久数据库、事件保留、恢复启用方式和旧 owner 死亡保证必须按实际宿主核对；默认内存 SDK 关闭以后不会留下可恢复的磁盘历史。

**学习 v1.2.24 还值不值？** 值。A 用更短的路径展示 agent 的因果链：消息、模型、工具、结果、续轮。先亲手追一遍 task 如何开 child Session、bash 如何启动 OS 进程、processor 如何更新 part，你才能判断 V2 的新增层究竟在处理哪种失败。随后补上本章的边界：input 不等于 transcript，Session 不等于执行，Scope 不等于数据库持久化，claim 不等于 exactly-once，Location 不等于授权。这样学 V2 是给已理解的核心补上运行系统，而不是背一串新服务名。
