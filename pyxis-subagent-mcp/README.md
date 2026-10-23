# pyxis-subagent-mcp

Rust stdio MCP，统一管理 Claude Code、Codex CLI 和 DeepSeek Harness（DSH）的独立会话。保留只接受 `prompt` 的同步 specialist tools，增加模型查询和异步会话管理接口。MCP 进程拥有其全部会话；退出时清理进程树，不提供后台服务或重启恢复。

编译和耗时检查使用最低进程优先级。Windows shell 使用 PowerShell 7，不加载 profile：

```powershell
[System.Diagnostics.Process]::GetCurrentProcess().PriorityClass = 'Idle'
cargo build --release --locked --target-dir target/candidate
.\target\candidate\release\pyxis-subagent-mcp.exe .\config.v2.example.json
```

程序只有一个位置参数：JSON 配置路径。stdout 只承载 MCP JSONL，stderr 输出诊断。运行 MCP 不需要 Python 或额外的 agent SDK；DSH 使用它自身的 Node runtime 加载二进制内嵌的薄插件。

## 统一会话接口

| 工具 | 输入与行为 |
| --- | --- |
| `agent_models` | `harness`、`cwd`，可选 `provider`、`refresh`；查询原生模型、effort、默认值和能力 |
| `agent_start` | `harness`、`cwd`、`prompt`，可选 `model`、`provider`、`effort`、`mode`、`timeout_seconds`、`idempotency_key`；立即返回会话和任务 ID |
| `agent_send` | `session_id`、`prompt`；`action: queue` 排下一轮，`action: steer` 必须指定当前 `task_id` |
| `agent_list` | 查询当前会话、有效配置、活动任务、队列和失效状态 |
| `agent_status` | 指定 `task_id` 或 `session_id`；任务事件支持 `cursor`、`event_limit` 分页 |
| `agent_wait` | `task_ids`（1–8 个）、可选逐任务 `cursor`、`timeout_seconds`（0–60）；完成、失败、取消或待处理交互时返回 |
| `agent_respond` | `interaction_id`、`response`；回答原生审批或问题 |
| `agent_cancel` | `task_id`；取消排队任务，或请求原生停止当前任务 |
| `agent_close` | `session_id`；停止活动任务、取消队列、清理进程树 |
| `agent_compact` | `session_id`，可选超时和幂等键；排入原生压缩任务 |

所有 harness CLI 均按需启动：MCP 初始化、工具发现、状态查询不会启动 CLI，任务获得执行名额后才启动。显式 `agent_models` 查询会临时启动对应 CLI，并在查询结束后退出。每个会话独立拥有原生上下文，空闲默认 15 分钟后退出整个 CLI 进程树；下一次任务通过 Claude `--resume`、Codex `thread/resume` 或 DSH 原生持久化恢复相同会话，恢复失败明确报错，不创建空白上下文。空闲期间仍保留 Pyxis 会话及其容量名额，使用 `agent_close` 释放。运行中和等待交互的任务不会被空闲回收。创建后固定 harness、provider、model、effort、cwd 和 mode；换配置应创建新会话。`cwd` 必须是已有的绝对目录，可以是调用者准备的 worktree。Pyxis 不创建 checkout；同目录写入由调用者协调。

省略模型和 effort 使用本机 runtime 默认值；显式 `effort: null` 选择原生默认值。`provider` 只适用于 DSH。模型目录提供能力提示，不验证账户权限，也不限制未列出的精确模型名。明确不支持的参数报错，后端原生错误直接可见，不替换模型或降低 effort。

示例：

```json
{
  "harness": "codex",
  "cwd": "I:/code/project-worktree",
  "model": "gpt-6.1-sol",
  "effort": "medium",
  "mode": "auto",
  "prompt": "Implement the agreed change and verify it.",
  "idempotency_key": "implement-change-1"
}
```

`agent_start` 的接受结果是 `{session_id, task_id}`；初始化或执行错误随后进入任务状态。任务状态为 `queued`、`running`、`waiting_input`、`completed`、`failed`、`cancelled`。状态包含有效配置、原生进度、最终结果、错误、usage 与待处理交互列表。原生 session/turn ID 与 Pyxis 的公开任务 ID 分开。

## 调度、交互与结果

同会话显式任务按 FIFO 调度，不同会话并发。仅自动压缩让位于显式任务。旧 specialist 与统一接口共享活动、存活会话和队列资源预算。默认值：

| 配置 | 默认 |
| --- | --- |
| `active_tasks` | 8；等待交互仍占名额 |
| `sessions` | 16 |
| `queued_tasks` | 128 |
| `idle_seconds` | 900；空闲 CLI 退出时间，原生上下文保留以便按需恢复；失效会话在此期限后移除 |
| `retained_tasks` | 最近 256 个已结束任务 |
| `retained_events` | 每任务最多 1024 个事件，同时受字节预算限制 |

新任务默认总超时 3600 秒，允许 1–86400 秒，从接受开始包含排队、初始化、执行和等待交互。取消等待工具的 MCP 请求只结束等待；提交成功的任务仍由 Pyxis 管理。关闭 MCP 则停止全部会话。

`steer` 绑定当前公开任务 ID；竞争中未应用会明确返回 `applied: false`；确认丢失时返回 `applied: null`，表示应用结果未知，不进入下一轮任务。Codex 和 DSH 使用原生 steer；Claude 中断当前原生轮次后提交纠正指令，公开任务 ID 不变，事件注明原生轮次变化，已有文件修改不回滚。

运行中的执行器持续读取原生事件，并处理审批、问题、引导和取消。交互保留原生请求、选项与回答约束；桥接层不会自动批准。调用者依据用户授权填写 `response`。无效、重复或过期回答不会应用，等待超时不构成同意。常用回答格式：

| 原生请求 | `response` 示例 |
| --- | --- |
| Claude 工具审批 | `{"behavior":"allow"}` 或 `{"behavior":"deny","message":"Reason"}` |
| Claude AskUserQuestion | `{"answers":{"Exact question text":"Selected answer"}}` |
| Codex 命令/文件审批 | `{"decision":"accept"}` 或 `{"decision":"decline"}`；以原生可选项为准 |
| Codex 用户问题 | `{"answers":{"question-id":{"answers":["Selected answer"]}}}` |
| Codex 权限请求 | `{"permissions":{},"scope":"turn"}`；仅提交授权的权限子集 |
| MCP elicitation | `{"action":"accept","content":{...}}` 或 `{"action":"decline","content":null}` |
| DSH 审批 | `{"outcome":"allowed-once"}`、`rejected` 或 `cancelled` |
| DSH 用户问题 | `{"answers":[{"id":"question-id","selected":["Offered label"],"custom":"Optional text"}]}` |

取消先使用原生 interrupt/cancel，最多等待 10 秒。确认停止后可续接同一会话；停止超时或协议故障清理整个进程树并标记会话失效。失效会话的队列明确失败，调用者需要创建新会话；不会自动换成空白上下文。

`agent_start`、`agent_send`、`agent_compact` 支持幂等键。同一键和相同参数重放同一提交；更改参数会报错。幂等提交由管理器持有，取消等待其确认的 MCP 调用不会重复提交。幂等记录是有界的进程内缓存，不提供跨重启或无限期去重。

事件返回 `cursor`（本页最后一个游标）、`latest_cursor`、`has_more`、`expired_before`、`cursor_expired`。继续分页应使用 `cursor`，不要直接跳到 `latest_cursor`。已回收任务返回 `unknown_or_expired`。每任务事件缓存还有 1 MiB 字节预算，单个进度事件最多 64 KiB；大事件用显式省略记录代替，交互原文另行保留。超长最终文字显式标记 `result_truncated`。

最终结果的 `usage` 保留后端原生数据，并用 `usage_scope` 标明口径。Claude 的多原生轮次分别保留 usage；Codex 保留原生 last 与会话 total；DSH 保留原始样本和可用聚合值。可用费用带 `cost_scope`；Claude `total_cost_usd` 是会话累计估算，不能当作单轮费用或订阅账单。没有费用时不自行估算。

## 配置与兼容

`config.v2.example.json` 将三家 `runtimes`、`specialists` 预设和 `limits` 分开。runtime 的 key 为 `claude`、`codex`、`dsh`，包含本机 executable、固定 CLI args、默认模型/effort、缓存策略与可选权限配置。specialist 可继承相同 harness 的 runtime 并覆盖模型、模式、描述和前缀；也可保留完整旧定义。

旧的根对象 `{toolName: AgentConfig}` 格式继续接受，只暴露原有 specialist。它们输入仍只有 `prompt`、同步返回最终答复，保留原超时和无人值守交互策略。specialist 名称不能使用保留的 `agent_` 前缀。

公共配置字段：

| 字段 | 含义 |
| --- | --- |
| `executable`、`args` | 本机可信启动配置，调用参数不能覆盖 |
| `model`、`reasoning_effort`、`provider` | runtime 默认值；provider 为 DSH 必填 |
| `mode` | `plan` / `auto`；runtime 省略时默认 auto |
| `prefix_prompt` | 原生固定 system/developer 前缀，默认空 |
| `cwd` | specialist 固定 cwd；省略时继承 MCP cwd |
| `request_timeout_seconds` | specialist 与自动压缩超时，默认 3600 |
| `cache` | enabled、ttl_seconds、compact_margin_seconds、use_usage_ttl |
| `dsh_module_root` | DSH 必填，包含其已安装 node_modules 的目录 |
| `approval_policy`、`sandbox` | 可选可信 Codex runtime 配置；省略时新接口继承原生配置 |

配置中的相对目录、带目录的 executable 相对于配置文件解析；裸 executable 使用 PATH。拒绝未知字段，不热重载运行中的配置。Windows 在向原生 CLI 传递 canonical cwd 时移除 verbatim device prefix，兼容原生目录遍历。

DSH Desktop 路径示例：

```json
{
  "executable": "C:/Users/USER/AppData/Local/Programs/DeepSeek Harness/resources/runtime/cli/bin/dsh.cmd",
  "dsh_module_root": "C:/Users/USER/AppData/Local/Programs/DeepSeek Harness/resources/app.asar/dsh",
  "args": ["--patch", "C:/Users/USER/.dsh/profiles/desktop/cordis.patch.yml"]
}
```

通过 Desktop patch 复用 provider、额外 MCP 和权限设置；凭证完全由原生服务读取。provider 名来自实际本机配置，不在代码中固定或复制凭证。2026-10-15 本机使用 `command-1`，保留 `deepseek/deepseek-v4.1-flash / max`。此前文档对 high/max 的拒绝结论是旧状态；新的原生 SDK 与桥接纯文本 max 请求已经成功，完整工具任务的验收见验证记录。

DSH 会将本会话原生 normal retry policy 的 `retryableCodes` 加入 `INVALID_REQUEST`，用于目前出现的 provider 400；保留原有次数、退避、jitter、禁用设置和其他错误码。当前原生默认最多重试 5 次，从约 0.5 秒开始指数退避。执行仍由已安装的 `dsh-llm-retry` 插件负责，只重试当前失败的模型请求，不重新提交 prompt 或重跑已完成的工具。原生 `llm/retry` / `llm/retry-started` 进入进度事件，取消和任务总超时仍有效。永久无效请求可能连续失败；耗尽后原样报告错误，不更换模型或 effort。

## 原生后端与压缩

| Harness | 控制协议 | 交互 | plan 与压缩 |
| --- | --- | --- | --- |
| Claude Code | 双向 stream-json / SDK control | host + stdio 权限回调 | 原生 plan；同流 /compact；按 session idle 与子任务生命周期确认完整完成 |
| Codex CLI | app-server model/thread/turn/event | 审批、权限、用户输入、MCP elicitation | plan collaboration + read-only；auto 继承 sandbox/审批；`thread/compact/start` |
| DSH | SDK profile + 薄插件 | 公开 approval / user-questions 服务 | 原生 planMode 是软指导；`compaction.compactNow` |

三家保留自己的工具、插件和项目配置。子进程携带 `PYXIS_SUBAGENT_NESTED`；继承配置启动的新版嵌套 Pyxis 不暴露执行工具，其余 MCP 仍可使用。旧版本嵌套进程不识别该标记，所以部署应更新被继承的 Pyxis 注册。

自动压缩沿用缓存窗口逻辑：从任务开始计时，触发点为 `start + TTL - margin`；Claude 写入桶选取最短可见 TTL，只有读取量时保留上一估计。忙时不压缩，显式任务优先，冷窗口不补发。每段用户活动最多自动压缩一次，不保温、不重置压缩计时。压缩失败使上下文失效时，任务和会话状态明确报告。压缩可能丢失信息，TTL 也不保证 provider 缓存命中。

Windows 使用 kill-on-close Job Object，Unix 使用独立进程组。Windows harness 默认隐藏窗口、Idle 优先级。DSH 插件只调用原生服务，不实现模型循环、压缩算法或凭证管理；其两个临时文件在进程结束时单独删除。

## 验证与部署

```powershell
[System.Diagnostics.Process]::GetCurrentProcess().PriorityClass = 'Idle'
cargo fmt --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --locked
cargo build --locked
python tests/integration.py
python tests/dynamic.py
node tests/bridge.mjs
```

Python 仅用于验证。模拟 CLI 覆盖旧接口回归、并发、FIFO、容量、上下文隔离、幂等、交互、竞态、取消、原生停止超时、压缩、分页与回收。

`tests/live_acceptance.py` 是显式启用的真实验收：指定 candidate binary、v2 config 和独立合成目录，验证文件、命令、会话隔离、问答、引导、取消与原生压缩；会使用原生登录和 provider 额度。`--effort` / `--native-effort` 只改变该次诊断任务，不修改 runtime 默认值。`tests/native_dsh_probe.py` 可对照不加载 Pyxis 的已安装官方 SDK。旧版基线记录仍保留在 `docs/verification.md`；当前验收记录单独标注日期与二进制哈希。

部署使用独立、带版本/哈希的候选二进制，验证后更新 MCP command、配置路径和工具白名单，保存原配置、原二进制哈希和回退信息。不要覆盖正在运行的 MCP executable。新连接必须重新检查 tools/list 与实际调用。

`scripts/deploy.py --binary CANDIDATE --runtime-config CONFIG --codex-config CODEX_CONFIG` 预检并显示将安装的工具；追加 `--apply` 才修改本机注册和 companion skill。脚本保存原 MCP table 与二进制哈希，保留无关配置字节和旧 executable。使用 `--rollback MANIFEST --codex-config CODEX_CONFIG --apply` 恢复；检测到注册或 skill 后续修改时停止，避免覆盖。`tests/deploy.py` 在合成配置上验证安装和回退。

协议依据：[Claude SDK 控制实现](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/query.py)、[Claude 用户输入](https://code.claude.com/docs/en/agent-sdk/user-input)、[Codex App Server](https://learn.chatgpt.com/docs/app-server)、DSH 已安装 SDK 与公开服务。
