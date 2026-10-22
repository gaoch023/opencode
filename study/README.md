# OpenCode 源码学习：从 v1.2.24 读懂最新 V1 与 V2

这组笔记为“先扎实读 v1.2.24，再按专题学习新架构”而写。阅读时把旧版源码留在眼前，用对照文档理解后来解决了什么问题，不需要先通读新版的整个仓库。

## 先固定比较对象

| 简称 | 比较对象 | 源码锚点 | 这里的含义 |
| --- | --- | --- | --- |
| 基线 | `release: v1.2.24` | `c6262f9d4002d86a1f1795c306aa329d45361d12`，2026-03-09 | 本学习分支的源码基线；包版本 1.2.24 |
| 最新 V1 | 当前仓库 `origin/dev` 中的传统执行链 | `388406238bd5ca15564a762840a2362c3a45bd9c`，2026-10-08 | 包版本 1.18.35；仍有 `SessionPrompt → SessionProcessor → LLM`，实现已大量 Effect 化 |
| V2 | 同一个 `dev` 提交中的新 core/protocol/server 执行链 | 同上 | `SessionV2 → SessionExecution → SessionRunner`；是架构代际，不代表已发布一个 2.x 产品版本 |

“最新”仅指本次核查的这个仓库快照，不声称它是阅读当天上游的最新发行版。远端检查仅暴露 `dev`，没有另一个现成的学习分支或 V2 分支；因此从 v1.2.24 发布提交建立本地 `v1.2.24-study` 分支。全部比较都引用完整 SHA，之后 `dev` 更新不会改变本文的证据。

本分支的应用源码保持 v1.2.24，只新增 `study/` 文档。源码链接指向同仓库的固定提交；新版文件在旧版学习分支中不存在时，点击链接即可阅读，或用 `git show <SHA>:<path>` 离线查看。

## 阅读顺序

| 文档 | 你要回答的问题 |
| --- | --- |
| [01 · v1.2.24 阅读地图](01-v1.2.24-reading-map.md) | 一次用户输入如何经过 loop、LLM、工具，变成可显示且可恢复的对话记录？ |
| [02 · Agent loop 与模型调用](02-agent-loop.md) | 谁控制下一轮？工具何时启动？重试、压缩、停止与系统上下文怎么变？ |
| [03 · Session、事件与并发状态](03-session-state.md) | 会话记录与执行状态如何分离？steer/queue、取消、重连和进程重启分别意味着什么？ |
| [04 · 子 agent 与工具调度](04-subagents-tools.md) | task 是新会话还是新进程？同步子任务与后台子任务怎么回传结果？V2 接上了多少？ |
| [05 · 前后台执行与进程模型](05-process-background.md) | Worker、Fiber、shell 子进程、PTY、BackgroundJob 到底分别管理什么？ |
| [06 · 入口、技术栈与迁移边界](06-entry-migration.md) | 为什么仓库里有三种“v2”？CLI/API 实际走哪条链？兼容桥接是否会双跑模型？ |

建议先按 01 走通一次完整调用链，再按自己卡住的问题读 02～06。每篇均比较三层；不要直接从 V2 的服务装配开始学基础 agent 行为。

## 先掌握这张对照表

| 主题 | v1.2.24 | 最新 V1 | V2 当前实现 |
| --- | --- | --- | --- |
| loop 所属 | `SessionPrompt.loop` 的 `while (true)` | 仍由 `SessionPrompt.runLoop` 编排，但进入 Effect 服务与运行状态机 | 独立 `SessionRunner`；每个 provider turn 显式调用一次 `llm.stream` |
| 同一会话并发 | Instance 内 `AbortController + callbacks` | `SessionRunState` / `Runner` 的 Fiber、Deferred 与状态机 | process-global coordinator 按 Session ID 串行、不同 Session 并发 |
| 输入何时进入上下文 | 先写 user message，再运行或加入正在运行的 loop | 仍以 V1 message/part 路径为中心 | 先 durable admission inbox；在安全边界 promote，才成为模型可见输入 |
| 会话数据 | SQLite session/message/part + Bus 更新通知 | 同步事件与投影基础设施已增加，不能再按“只有内存 Bus”理解 | EventV2 持久事件 + 投影；durable seq 支持 replay/tail |
| 子 agent | TaskTool 创建或复用 child Session，等待其 loop | 保留同步模式；增加受实验开关控制的后台 task | 已有新 Agent 定义与通用后台服务，但原生 task 内置工具尚未接入 |
| bash 前后台 | 普通 bash 工具等待 OS 子进程完成；`detached` 不等于工具后台返回 | shell 仍等待命令结束；后台能力增加在 task/subagent，不能类推给 bash | bash 前台执行已接入；把 BackgroundJob 暴露给 V2 bash/agent 的完整后台协议仍是后续工作 |
| 工具并行 | SDK execute 回调与异步工具执行；不能把 await 误读成整个系统单线程阻塞 | Effect 管理工具作用域，兼容 SDK/native 两个 LLM 后端 | 先记录完整调用再启动 structured child Fiber；流结束后等所有已启动工具结算，再续轮 |
| 取消 | AbortSignal 协作取消，bash 额外杀进程树 | Fiber interrupt + scoped cleanup；后台任务有独立生命周期 | coordinator 中断本进程活动 drain；保留 inbox；不等于分布式取消 |
| 进程重启 | 会话记录保留，活跃 Promise/abort 状态消失 | 会话与事件保留；BackgroundJob registry 和活动 Fiber 是内存状态，不会自动恢复 | durable 记录可回放，但自动恢复未知 provider/工具副作用并未因此完成 |
| 压缩 | V1 compaction part、专用摘要调用与历史裁剪 | 原链路继续演进 | runner 自动压缩已有；公共 `sessions.compact()` 尚不可用，不能混为一谈 |
| 默认入口 | 根目录 `bun dev` → `packages/opencode` | 根目录仍走 `packages/opencode` 的 V1 prompt API | `/api/session/...` 和新 CLI/core 是另一条入口；并非切到 dev 就全部走 V2 |

这张表是导航。细节、前提、实验开关和精确源码证据见各专题，尤其不要把“共享服务已存在”推断成“所有 V2 功能已接入”。

## 用同一个例子比较三条链

假设用户说：“读项目，再让 explore 找出登录入口，执行测试。”

### v1.2.24

```text
用户输入
  → prompt 写 user message/parts
  → loop 读取会话历史，选 agent/model，构建工具
  → LLM.stream / SDK streamText
  → 模型发 read/task/bash tool call
  → task 创建 child Session，再调用一次 SessionPrompt.prompt
      → 子会话有自己的 loop，父 task 等它返回
  → bash 创建 OS shell 子进程，等待结束并收集输出
  → processor 写 tool result 和消息状态
  → loop 重读历史，决定续轮或结束
```

### 最新 V1

外部行为仍能对上这条链，但 await/AbortController 的管理大量迁入 Effect 服务与 Fiber 生命周期。task 的后台模式可以先返回任务信息，让父 loop 继续；后台完成后用 synthetic prompt 回注父会话。shell 工具本身仍等待命令退出，不能把 task 的后台参数类推给 bash。启用条件和取消规则见 04、05。

### V2

```text
prompt admission
  → 持久 inbox（收到请求 ≠ 已写入模型历史）
  → advisory wake / 按 Session 排他执行
  → 获取当前 Location 的服务
  → 首次先初始化完整 Context Epoch，再 promote 输入
  → 后续安全边界 promote 输入、reconcile 上下文并准备历史
  → 一次 llm.stream
  → 完整 local tool-call 持久化后立即启动其 Fiber
  → 等 provider stream 结束，再等已启动工具结果持久化
  → 重新读取投影历史并判断续轮
```

这个 V2 例子里的 `read`、`bash` 能沿原生内置工具链理解，`task` 则不能直接照搬：当前原生 registry 没有 task 的实现。学习 V2 子 agent 时，应先理解它需要接到 Session/BackgroundJob/权限/完成通知的哪些边界，再把它与“当前已经可用”分开。

## 把这些执行单位分清楚

| 名称 | 它是什么 | 它本身不保证什么 |
| --- | --- | --- |
| Agent | 提示词、模型、工具权限、步数等配置身份 | 不自动创建进程或独立文件系统 |
| Session | 会话身份、历史、上下文选择及相关持久记录 | 不等于正在运行的 loop |
| Provider turn | 一次模型流请求及其本轮工具处理 | 不等于一整段用户任务或整个 Session |
| Drain / run | 本进程这次负责推进会话的活动执行链 | 不自动获得跨机器执行所有权或崩溃恢复能力 |
| Tool call | 模型请求一次工具能力 | 不一定创建 OS 进程；read 和 bash 的资源类型不同 |
| Subagent | 一个 agent 身份下的 child Session 执行 | 不等于新线程、新容器或新 OS 进程 |
| Fiber | Effect 的协作执行与取消单位 | 不等于 OS 线程，取消也不是强行回滚外部副作用 |
| Worker | Bun/JS Worker 执行环境 | 不等于子 agent，不按 Session 自动一一创建 |
| OS child process | bash、命令工具、daemon 等启动的系统进程 | `detached` 本身不意味着 tool call 已结束 |
| PTY | 为交互式进程提供的伪终端与输入输出会话 | 不等于后台 agent 调度器 |
| BackgroundJob | 管理后台工作、状态、观察、取消等生命周期的进程内服务 | 当前 registry 非持久化；OS 进程或 Fiber 也不因 job ID 而跨重启存活 |

## 边学边验证，不需要同时啃完整个新仓库

1. 在基线上给一次 `read` 调用画出 message/part 的状态变化，再看 02 中 V2 的事件化结算。
2. 给 `task` 的父 Session、child Session、tool call ID 各写一个值，追踪谁等待谁；再看后台模式为什么需要额外 job ID 与完成通知。
3. 想象用户在 loop 正运行时再发一句话，分别解释基线 callbacks、最新 V1 状态机、V2 steer/queue 的作用。
4. 分别取消一次 provider 请求、前台 bash、后台任务，画出取消信号如何传播，哪些副作用不能撤回。
5. 假设进程在“工具已改文件、结果还未落库”之间崩溃，说明为什么“事件已持久化”不能证明可以安全重跑工具。
6. 用 06 的入口表确认当前读到的是 V1、V1 native adapter 还是原生 V2，再继续往下追。

## 阅读与验证范围

分析以固定提交的源码为主，已有测试用于核对预期行为；设计文档和 TODO 仅作线索。没有为本次文档调用外部模型、执行有副作用的演示任务，或声称已经运行所有版本的完整测试。文档校验会检查源码引用的提交、路径、行号与内部链接。

本分支使用 v1.2.24 的 `packageManager`（Bun 1.3.10）；此前为 dev 安装的 Bun 1.3.14 和 node_modules 不应被当作基线运行验证结果。仅阅读源码不需要重装依赖。若以后要运行基线，请按它自己的 package.json 与 frozen lockfile 准备依赖，不要沿用 dev 的构建产物判断行为。
