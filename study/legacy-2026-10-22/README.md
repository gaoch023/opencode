# OpenCode 源码学习：v1.2.24 → V1 1.18.35 → V2 2.1.3

**2026-10-22 已按远端 dev 更新结论：当前 V2 的 `subagent` 与 `shell` 都支持后台执行，而且已默认注册。** 此前“V2 task 未移植”“后台 shell 尚未接入”只适用于 2026-10-08 的旧快照，不能描述当前代码。

本学习分支的应用源码仍保持 v1.2.24，只增加学习文档。你可以集中学习这份较易跟读的实现，再按专题理解新版。

## 下载与离线阅读

- [完整离线 ZIP](../downloads/opencode-source-study.zip)：包含九篇 Markdown、已渲染的 HTML 阅读版和四张 SVG 流程图。
- [单文件 HTML 阅读版](../downloads/opencode-source-study.html)：下载后直接用浏览器打开。

ZIP 解压后双击 `index.html`。正文、表格、目录和流程图可离线阅读，GitHub 源码外链需要联网。HTML 在 GitHub 文件页不会直接作为网页执行，应先下载；Markdown 文档可以直接在 GitHub 查看渲染结果。

## 固定版本，避免混用

| 对象 | 固定提交 | 含义 |
| --- | --- | --- |
| 学习基线 A | `c6262f9d4002d86a1f1795c306aa329d45361d12` | 2026-03-09，release v1.2.24 |
| V1 参考 B | `388406238bd5ca15564a762840a2362c3a45bd9c` | 2026-10-08，V1 包版本 1.18.35；旧专题 02～06 的取证快照 |
| 推广前 V1 主线末端 | `055d95bb7e278c94baf06235a52cac79dd13ba67` | V2 推广提交的第一父提交；V1 包版本仍为 1.18.35 |
| **当前 V2 N** | **`62c9002819d06dd48b673a1f84e94ce3e51e50e0`** | **2026-10-22，根包版本 2.1.3，Bun 1.4.4** |

历史分界是 [5caf1f9f6f · chore: promote v2 to dev](https://github.com/gaoch023/opencode/commit/5caf1f9f6f5f30e13fc50a788ad953d97ae3748f)，日期 2026-10-10。旧快照早于推广，当前快照晚于它。部分 V2 功能早已在侧支开发，不能只看源码提交日期，就说这些功能都是推广之后才开发出来的。

本次确认仓库不是 shallow checkout，未启用 sparse checkout 或 partial clone；远端暴露 dev。问题根因是此前没有刷新推广前快照，不是当前仓库缺几个源码文件。拉取后本地 dev 与远端的 N 提交一致。

B 到推广前 V1 末端的相关源码变化仅涉及两个 Copilot 适配文件；旧文中的 loop/task/shell 专题仍可作为 V1 1.18.35 学习材料。“推广前末端”不代表另有已验证的 1.x 维护分支或发行标签。

## 阅读顺序

| 文档 | 范围与用途 |
| --- | --- |
| [01 · v1.2.24 阅读地图](01-v1.2.24-reading-map.md) | 只讲基线；先走通输入、loop、LLM、工具和存储 |
| **[07 · 当前 V2 的子 agent 与后台工具](07-current-v2.md)** | **以 N 为准：subagent、shell 前后台、Job、完成回注、管理 API 与重启恢复** |
| **[08 · 当前 V2 的 Session 与 loop](08-current-session.md)** | **以 N 为准：新 inbox、step、重试、系统上下文、execution claim 和启动恢复** |
| [02 · 历史 loop 对照](02-agent-loop.md) | A 与 B；其中 V2 是推广前的重构切片 |
| [03 · 历史 Session 对照](03-session-state.md) | A 与 B；旧 session_input、精确 payload 重试不能直接套到 N |
| [04 · 历史子 agent 与工具](04-subagents-tools.md) | A 与 B 的 V1 task 演进；旧 V2 缺失结论只作历史 |
| [05 · 历史进程与后台对照](05-process-background.md) | Fiber/Worker/OS 进程/PTY 区别；当前 Job 恢复见 07 |
| [06 · 历史入口与迁移边界](06-entry-migration.md) | B 当时的 V1/V2 并存入口；当前默认入口已改变 |

02～06 仍保留固定旧提交的源码证据，用于理解重构过程；每篇顶部都标明历史范围。**判断当前功能是否存在，应以 07、08 为准。**

## 当前能力矩阵

| 能力 | v1.2.24 | V1 1.18.35 参考实现 | 当前 V2 2.1.3 |
| --- | --- | --- | --- |
| 普通 shell 工具 | `bash`，等待结果 | `bash`，Effect scoped shell | **`shell`，支持前台与后台** |
| 子 agent 委派 | `task`，子 Session | `task`，子 Session | **`subagent`，子 Session，已默认注册** |
| 子 agent 后台启动 | 无专用后台参数 | `background`，需实验开关 | **`background:true`，立即返回并注册完成通知** |
| shell 后台启动 | 无受管理后台工具协议 | 无受管理后台工具协议 | **`background:true`，返回 shellID、输出文件与 running** |
| 前台转后台 | 无统一转换 | task 有实验入口 | **Session background 操作，可转移当前可后台化工具** |
| 后台结果回到会话 | 无通用机制 | task synthetic prompt 回注 | **shell/subagent 均有 synthetic 完成通知** |
| 后台状态 | 无统一 registry | 进程内 BackgroundJob | **Job 活跃 registry + 可恢复工作的持久通知标记** |
| 重启时后台 shell | 无相同协议 | 不恢复 registry | **running shell 报 cancelled 并补通知，不重跑命令** |
| 重启时后台子 agent | 无相同协议 | 不自动恢复 | **身份及恢复预算检查后可继续子会话并补通知** |
| 同一 Session 并发 | AbortController + callbacks | Effect Runner / Deferred | **process-global coordinator，Location-scoped runner** |
| 输入接收 | 先写可见 user message | 同上 | **session_inbox 先入队，在安全 step 边界交付** |
| 模型步重试 | processor 重试 | V1 retry policy | **step 重试与 RetryScheduled，见 08** |
| 公开 compact/shell/skill/wait | V1 对应能力 | V1 对应能力 | **已有实现；旧 unavailable 结论失效** |
| 默认开发入口 | packages/opencode | packages/opencode | **packages/cli；旧 packages/opencode 源码包已移除** |

这里的“已实现”来自执行链、默认注册与现有测试的静态核对，不代表本轮运行过模型请求或完整测试套件。权限、agent mode、深度等条件仍会限制实际调用。

## 你最关心的两条新链

```text
父模型 → subagent(background=true)
  → 校验 agent / 权限 / 深度，创建或续用 child Session
  → child.prompt → SubagentJob.start → Session.resume
  → 父工具立即返回 running + sessionID
  → 子会话完成 → synthetic 完成/失败结果送入父会话
```

```text
父模型 → shell(background=true)
  → Shell.create 启动真实 OS shell 进程，输出持续写文件
  → Job.start + background，持久化可恢复通知元数据
  → 父工具立即返回 running + shellID + 输出文件
  → 命令完成 → synthetic 输出/退出状态送入所属 Session
```

这已经超出 shell 命令里手写 `&`：它有 OpenCode 自己的执行身份、观察、前后台转换和通知协议。shell 还提供 HTTP list/create/get/output/remove 接口；应用 API 与模型直接可用的工具目录仍要分开理解。

## 仍要分清的边界

- Agent 是策略身份；Session 是对话身份；子 agent 通常仍是宿主进程内的异步执行链，不是每个 child 都启动 OS 进程。
- shell 命令会创建真实 OS 子进程；PTY 是带伪终端的交互进程。shellID、SessionID、Job ID 与 PID 不要混用。
- 当前已有恢复机制，但不保证所有 OS 进程跨服务器重启存活，也不保证任意外部副作用 exactly-once。
- 当前父会话取消与后台子任务的关系应看 07，不要直接沿用旧 V1 的递归后代取消规则。
- 学习基线使用 Bun 1.3.10，N 使用 Bun 1.4.4。此前 Bun 1.3.14 的依赖安装结果不能视为新版本运行验证。

新源码尽量引用完整 SHA。阅读时按链接或 `git show <SHA>:<path>` 查看，避免在 v1.2.24 分支中按新路径查找不存在的文件。
