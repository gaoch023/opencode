# 06 · 运行入口与架构迁移边界

> **历史快照说明（2026-10-22 更新）：** 本文比较 v1.2.24 与 2026-10-08 的 `388406238b`，其中“当前/最新/V2”都指当时快照。远端已在 2026-10-10 推广 V2；当前 2.1.3 已支持 subagent 与 shell 后台执行、持久通知及受限启动恢复。当前能力请读 [07](07-current-v2.md) 和 [08](08-current-session.md)，不要将本文的未实现项当成当前结论。

> [返回学习总览](README.md)。A 为 v1.2.24，B 为固定 dev；有 C 标记时指同一 dev 中的 V2。行号按各自提交计算。

本文的 A 指 v1.2.24 发布提交 `c6262f9d4002d86a1f1795c306aa329d45361d12`；B 指仓库当前 dev `388406238bd5ca15564a762840a2362c3a45bd9c`（packages/opencode/package.json 的版本为 1.18.35）。所有行号对应上述固定提交。以下是源码静态核查，没有运行模型请求或新 CLI 交互验收。

## 1. 根目录 bun dev 仍是 V1 主流程

B `package.json:9` 依然执行 `bun run --cwd packages/opencode src/index.ts`。正常 `run` 的调用链是：

```text
packages/opencode/src/cli/cmd/run.ts:864 client.session.prompt(...)
  -> packages/sdk/js/src/v2/gen/sdk.gen.ts:3742 prompt()
  -> 同文件:3786 POST /session/{sessionID}/message
  -> packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts:295 prompt()
  -> 同文件:301 promptSvc.prompt(...)
  -> packages/opencode/src/session/prompt.ts 的 SessionPrompt
```

TUI 也可从 [B `packages/tui/src/component/prompt/index.tsx:1094`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/tui/src/component/prompt/index.tsx#L1094) 看见 `sdk.client.session.prompt`。因此“已经用了 sdk/v2，所以整个 agent loop 已切换 V2”不成立。此处 SDK 的目录版本和业务架构的代际是不同维度。

最新 V1 的 HTTP 框架确实已经换掉：[A `packages/opencode/src/server/server.ts:5`](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/server/server.ts#L5) 使用 Hono，B 同路径 `:3-10` 使用 NodeHttpServer + Effect HttpRouter/HttpApiApp。HTTP 框架迁移不能证明 SessionPrompt 已被 V2 SessionRunner 替代。

## 2. 真 V2 有另一套显式 API 与入口

[B `packages/protocol/src/groups/session.ts:205`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/protocol/src/groups/session.ts#L205) 定义 `POST /api/session/:sessionID/prompt`；`:209-213` 是 `prompt / delivery / resume` 输入；[B `packages/server/src/handlers/session.ts:140-150`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/server/src/handlers/session.ts#L140) 将它交给 `SessionV2.prompt`。

两个 API 可以挂在同一 HTTP 服务下：[B `packages/opencode/src/server/routes/instance/httpapi/server.ts:154-171`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/server/routes/instance/httpapi/server.ts#L154) 装配旧 instance routes，`:177-180` 装配新的 `Api` 和 `@opencode-ai/server/handlers`；`:276-283` 将两者合并，`:299-302` 给 V2 注入 `SessionExecutionLocal`。

```text
同一个 opencode HTTP listener
  ├─ /session/:id/message -> V1 SessionPrompt
  └─ /api/session/:id/prompt -> V2 SessionV2 -> SessionExecutionLocal -> SessionRunner
```

不能因一个 Server 同时装配两套服务，就推导“一个 prompt 会触发两个 agent loop”。真实调用由客户端选择的 API path 决定。

[B `packages/sdk-next/src/opencode.ts:10-38`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/sdk-next/src/opencode.ts#L10) 还实现内嵌 V2：创建 `createEmbeddedRoutes()`，用自定义 fetch 直连进程内 HTTP handler，不必真的绑定 TCP 端口。`:40-41` 暴露应用级工具注册。它与旧 `@opencode-ai/sdk/v2` 是不同模块。

## 3. 新 packages/cli 也不能和根 bun dev 混为一谈

[B `packages/cli/package.json:8`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/cli/package.json#L8) 的 bin 叫 `lildax`，`:15` dev 运行自己的 `src/index.ts`。[B `packages/cli/src/index.ts:10-24`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/cli/src/index.ts#L10) 注册 default/api/debug/migrate/service/serve 命令。

其 serve 实现 [B `packages/cli/src/commands/handlers/serve.ts:11,44`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/cli/src/commands/handlers/serve.ts#L11) 使用 `@opencode-ai/server/routes.createRoutes`；后者 [B `packages/server/src/routes.ts:26-37,51-61`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/server/src/routes.ts#L26) 装配 V2 SessionV2 与 SessionExecutionLocal。

daemon 是真实独立操作系统进程：[B `packages/cli/src/services/daemon.ts:110-134`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/cli/src/services/daemon.ts#L110) 用 `spawn(process.execPath, [..., "serve", "--register"], {detached:true,stdio:"ignore"}).unref()` 启动，`:66-77` 通过健康检查与版本校验连接。它的进程独立性与某一 Session 内 Fiber 并发是不同问题。

新 CLI default handler [B `packages/cli/src/commands/handlers/default.ts:7-11`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/cli/src/commands/handlers/default.ts#L7) 取得 daemon transport，再调用复用的 TUI。[B `packages/cli/src/tui.ts:21-34`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/cli/src/tui.ts#L21) 对一小部分旧接口 404 提供默认返回值，说明此处仍有 UI 兼容接缝；这些默认值不能证明所有旧 Session API 都完成兼容。本次没有运行新 CLI TUI，不能据此断言其所有交互路径完整可用。

## 4. native LLM runtime 是最新 V1 内部的 provider 适配选择

[B `packages/opencode/src/effect/runtime-flags.ts:54`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/effect/runtime-flags.ts#L54) 的 `OPENCODE_EXPERIMENTAL_NATIVE_LLM` 控制它。

[B `packages/opencode/src/session/llm.ts:224-252`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/llm.ts#L224) 在启用时尝试 LLMNativeRuntime；不支持会记录原因并回退（`:254-268`）；默认仍使用 AI SDK `streamText`（`:271-280`）。它们最终都被归一成 processor 使用的 LLMEvent 流。

[B `packages/opencode/src/session/llm/native-runtime.ts:103-137`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/llm/native-runtime.ts#L103) 调用 `llmClient.stream` 并用 FiberSet 调度工具，`:169-187` 将工具回调适配回旧 `Tool.execute`。这不会自动替换外层 SessionPrompt、V1 transcript、V1 TaskTool、V1 并发状态机。可称“V1 用了新 LLM 传输实现”，不能称“V1 变成 V2 Session 核心”。

## 5. message-v2.ts 是历史命名，不等于新的 V2 Session

A 本来就有 `packages/opencode/src/session/message-v2.ts:19` 的 `MessageV2` namespace。

B 同路径 `:2-17` 明确从 `@opencode-ai/core/v1/session` 导入类型，`:56-62` 的 Event 映射也是 SessionV1.Event。真正新 Session 消息模型在 `packages/core/src/session/message.ts` 与 `packages/schema/src/session-message.ts`，不能按文件名中有无 v2 判代。

同理，[B `packages/opencode/src/session/session.ts:13-14`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/session.ts#L13) 虽然残留 SessionV2/SessionExecutionLocal import，实际服务依赖 `:1010-1013` 仍是 BackgroundJob、RuntimeFlags、Database、EventV2Bridge。unused import 不是运行时转发证据。

## 6. bridge 同步的是事件，不能等价为第二次执行

[B `packages/opencode/src/event-v2-bridge.ts:19-32`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/event-v2-bridge.ts#L19) 为发布的事件附加 location；`:35-60` 把事件及持久化 sync 包转发到 GlobalBus；没有调用 SessionExecution.resume 或 SessionPrompt.loop。[B `packages/opencode/src/session/session.ts:631`](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/session.ts#L631) 发布 V1 MessageUpdated。

B `specs/v2/session.md` 中关于 shadow bridge 发 Prompted 的描述，不能直接当成当前调用链事实：应优先以当前实现和事件发布点为准。即使观察到 V1 数据投影或 EventV2 使用，也不代表独立执行了 V2 LLM turn。

## 7. 给 v1.2.24 学习者的目录迁移地图

| 内容 | A v1.2.24 | B 最新实现 |
|---|---|---|
| 默认入口 | packages/opencode/src/index.ts | 根 bun dev 仍到相同包；另有 packages/cli |
| agent loop | packages/opencode/src/session/prompt.ts | 最新 V1 保留；V2 在 packages/core/src/session/runner/ |
| HTTP | Hono routes/session.ts | 最新 V1 Effect HttpApi handlers/session.ts；V2 protocol + server 分包 |
| 消息类型 | opencode/session/message-v2.ts | 最新 V1 core/v1 + schema/session-v1；新 V2 schema/session-message |
| 公共协议 | 旧服务生成 SDK | V2 packages/protocol + packages/client；旧 sdk/v2 仍存在 |
| 内嵌客户端 | 旧 SDK fetch/server 路径 | 新 V2 packages/sdk-next 组合 Client/Core/Server |
| Bun pin | A package.json:7 为 1.3.10 | B package.json:7 为 1.3.14 |
| AI SDK catalog | A package.json:46 为 5.0.124 | B package.json catalog ai 为 6.0.168 |

先在 A 中学透“prompt -> loop -> processor -> llm -> tool.execute -> 保存 message/part -> 下一轮”；再分别看 B 的 V1 保留了哪些语义、V2 在哪些边界重新建模。不要从目录数量或 V2 字样猜调用链。


## 固定提交源码索引

正文中的短文件名和 `:行号` 延续所在段落的版本与文件。下表按完整路径提供两个提交的入口；“—”表示该路径在该提交中不存在，不能用学习分支中的同名文件代替新版源码。

| 文件 | A · v1.2.24 | B · dev（含最新 V1/V2） |
| --- | --- | --- |
| `packages/cli/package.json` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/cli/package.json#L1) |
| `packages/cli/src/commands/handlers/default.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/cli/src/commands/handlers/default.ts#L1) |
| `packages/cli/src/commands/handlers/serve.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/cli/src/commands/handlers/serve.ts#L1) |
| `packages/cli/src/index.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/cli/src/index.ts#L1) |
| `packages/cli/src/services/daemon.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/cli/src/services/daemon.ts#L1) |
| `packages/cli/src/tui.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/cli/src/tui.ts#L1) |
| `packages/core/src/session/message.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/core/src/session/message.ts#L1) |
| `packages/opencode/package.json` | [源码](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/package.json#L1) | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/package.json#L1) |
| `packages/opencode/src/cli/cmd/run.ts` | [源码](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/cli/cmd/run.ts#L1) | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/cli/cmd/run.ts#L1) |
| `packages/opencode/src/effect/runtime-flags.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/effect/runtime-flags.ts#L1) |
| `packages/opencode/src/event-v2-bridge.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/event-v2-bridge.ts#L1) |
| `packages/opencode/src/index.ts` | [源码](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/index.ts#L1) | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/index.ts#L1) |
| `packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts#L1) |
| `packages/opencode/src/server/routes/instance/httpapi/server.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/server/routes/instance/httpapi/server.ts#L1) |
| `packages/opencode/src/server/server.ts` | [源码](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/server/server.ts#L1) | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/server/server.ts#L1) |
| `packages/opencode/src/session/llm.ts` | [源码](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/session/llm.ts#L1) | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/llm.ts#L1) |
| `packages/opencode/src/session/llm/native-runtime.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/llm/native-runtime.ts#L1) |
| `packages/opencode/src/session/message-v2.ts` | [源码](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/session/message-v2.ts#L1) | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/message-v2.ts#L1) |
| `packages/opencode/src/session/prompt.ts` | [源码](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/opencode/src/session/prompt.ts#L1) | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/prompt.ts#L1) |
| `packages/opencode/src/session/session.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/opencode/src/session/session.ts#L1) |
| `packages/protocol/src/groups/session.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/protocol/src/groups/session.ts#L1) |
| `packages/schema/src/session-message.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/schema/src/session-message.ts#L1) |
| `packages/sdk-next/src/opencode.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/sdk-next/src/opencode.ts#L1) |
| `packages/sdk/js/src/v2/gen/sdk.gen.ts` | [源码](https://github.com/gaoch023/opencode/blob/c6262f9d4002d86a1f1795c306aa329d45361d12/packages/sdk/js/src/v2/gen/sdk.gen.ts#L1) | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/sdk/js/src/v2/gen/sdk.gen.ts#L1) |
| `packages/server/src/handlers/session.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/server/src/handlers/session.ts#L1) |
| `packages/server/src/routes.ts` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/server/src/routes.ts#L1) |
| `packages/tui/src/component/prompt/index.tsx` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/packages/tui/src/component/prompt/index.tsx#L1) |
| `specs/v2/session.md` | — | [源码](https://github.com/gaoch023/opencode/blob/388406238bd5ca15564a762840a2362c3a45bd9c/specs/v2/session.md#L1) |
