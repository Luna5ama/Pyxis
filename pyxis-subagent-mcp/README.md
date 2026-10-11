# pyxis-subagent-mcp

Rust 编写的 stdio MCP，把 Claude Code、Codex CLI、DeepSeek Harness 包装成可供任意 MCP caller 调用的 specialist tools。配置根对象的每个 key 就是 tool 名字；默认示例配置只暴露 `opus-plan`，使用 Opus 5.5 / high / plan。

```powershell
cargo build --release --locked
.\target\release\pyxis-subagent-mcp.exe .\config.example.json
```

程序只有一个位置参数：JSON 配置文件路径。stdout 仅承载 MCP JSONL；诊断和任务/压缩日志输出到 stderr。使用单线程 Tokio runtime，直接管理 CLI stdio，不需要 Python、Node SDK 或 Rust agent SDK。DSH 自带的 Node runtime 会加载一个随二进制嵌入的微型插件。

MCP client 配置示例（将路径换成实际绝对路径）：

```json
{
  "mcpServers": {
    "pyxis-subagent": {
      "command": "I:/code/Pyxis/pyxis-subagent-mcp/target/release/pyxis-subagent-mcp.exe",
      "args": ["I:/code/Pyxis/pyxis-subagent-mcp/config.example.json"]
    }
  }
}
```

调用 `opus-plan`：

```json
{ "prompt": "研究当前渲染模块，比较两种资源所有权方案，并给出实现计划。" }
```

输入只有 `prompt`。输出 `content` 为最终答复；`structuredContent` 包含 `result`、`session_id`、原始 `usage`、可用时的 `total_cost_usd`。session ID 仅用于诊断，caller 不负责管理。Claude 的 `total_cost_usd` 是 CLI 报告的会话累计估算值，不是单次费用或订阅账单。

每个 MCP 进程、每个 tool 恰好一个会话。首次调用才启动 harness；同一 tool 的请求排队，不同 tool 可以并发。后续调用自动复用常驻进程和历史。MCP 退出后丢弃会话注册表，不恢复旧会话。各 harness 仍可能按自身设置写 transcript，本项目不删除其历史文件。

## 配置

本机 Codex 注册为 `pyxis-subagent-mcp`，读取 `config.local.json`，当前开放 `opus-plan` 和 `exec-low`；完整预设保存在 `config.all.local.json`。本机路径和 provider patch 不纳入版本控制。三个执行 specialist 都使用 auto 模式，任务范围写入 tool description 和固定前缀：

| Tool | 模型 / effort | 任务范围 | 请求超时 |
| --- | --- | --- | --- |
| `exec-high` | `gpt-6.1-sol` / medium | 复杂功能或模块实现，长时间任务 | 4 小时 |
| `exec-med` | `gpt-6-luna` / max | 简单功能或模块，边界清晰，中等时长 | 2 小时 |
| `exec-low` | DSH `command-1` / `deepseek/deepseek-v4.1-flash` / max | 非常简单、机械、边界非常清晰的中短任务；不处理系统级或 safety-critical 工作 | 1 小时 |

**当前 `exec-low` 按用户选择保留 max，但该 DSH provider/model 会拒绝此 effort，暂时无法执行任务。** MCP 发现与注册可用；这不表示模型调用成功。`exec-high`、`exec-med` 仅已配置，尚未对本机 Codex 开放。Codex 的 `enabled_tools` 也限制为 `opus-plan` 和 `exec-low`，工具调用超时设为 3700 秒。请求超时是运行上限，不是任务时长保证。

见 `config.example.json` 和 `config.all.example.json`。必填：`executable`、`harness` (`claude` / `codex` / `dsh`)、`mode` (`plan` / `auto`)、`model`、`description`。`description` 原样进入 MCP tool description。

可选字段：

| 字段 | 默认值 / 含义 |
| --- | --- |
| `prefix_prompt` | 空；Claude append-system-prompt、Codex developerInstructions、DSH 固定 system prompt section |
| `reasoning_effort` | 字符串；`null` 或省略表示 provider 默认，适用于不支持 effort 的模型 |
| `args` | 空；放在生成的 CLI 参数之前，例如 `node` 的脚本路径 |
| `cwd` | MCP 启动时的工作目录；同一 tool 生命周期内固定 |
| `request_timeout_seconds` | 3600；包含初始化、完整任务或压缩的上限 |
| `cache.enabled` | true；允许每次任务后的单次空闲压缩 |
| `cache.ttl_seconds` | 300；未观测到 TTL 时的配置估计值 |
| `cache.compact_margin_seconds` | 60；在缓存到期前预留的压缩时间 |
| `cache.use_usage_ttl` | true；Claude usage 有写入桶时更新 TTL |
| `provider` | DSH 必填；账户登录用 `deepseek-account`，API key 路由用 `deepseek-official` |
| `dsh_module_root` | DSH 必填；包含已安装 DSH `node_modules` 的目录 |

`cwd`、包含目录的相对 `executable` 路径和 `dsh_module_root` 相对于配置文件解析；`args` 原样传给子进程。裸 executable 名字使用 PATH。无配置热重载，改模型/effort/前缀需重启 MCP。配置拒绝未知字段，避免拼写错误静默改变行为。

DSH Desktop 在 Windows 上的路径示例：

```json
{
  "executable": "C:/Users/USER/AppData/Local/Programs/DeepSeek Harness/resources/runtime/cli/bin/dsh.cmd",
  "dsh_module_root": "C:/Users/USER/AppData/Local/Programs/DeepSeek Harness/resources/app.asar/dsh"
}
```

上述片段需合并到完整 DSH tool 配置。npm 安装时 `dsh_module_root` 指向相应安装目录，例如 `/usr/local/lib`（其下必须能解析所需 `@deepseek-ai` packages）。本机验证版本：Claude Code 2.1.296、Codex CLI 0.161.0、DSH Desktop 0.2.0-rc.2。模型权限由当前 CLI 登录账户决定，示例不能保证其他账户可用。

已有的 DSH 自定义 provider 若配置在 Desktop profile，可通过 `args: ["--patch", "C:/Users/USER/.dsh/profiles/desktop/cordis.patch.yml"]` 复用该 patch，或引用自己准备的 provider patch；无需把凭证复制进 MCP 配置。本机 `command-1` / `deepseek/deepseek-v4.1-flash` 在原端到端验证中使用 `reasoning_effort: null`；显式 `high` 和 `max` 都会被当前 DSH 路由拒绝。当前执行预设按用户要求保留 `max`，不会静默降级。

## 缓存与压缩

计时从任务请求开始，触发点为 `start + TTL - margin`。Claude usage 的 5 分钟/1 小时写入桶用于推断 TTL；两者都有时取短者。只有缓存读取量时保留上次观测值。首次启动耗时也被算进窗口，计时偏保守。

计时器和任务共用同一把会话锁。新任务覆盖旧 deadline；任务运行中不会压缩。任务结束时若窗口已经过去，只记录 `compact_window_missed`。每段用户活动后最多自动压缩一次，压缩完成不重置定时器，也不发送保温请求。

自动压缩会损失上下文信息，这是本项目按需求启用的行为；设置 `cache.enabled: false` 可关闭。TTL 是配置/usage 推断，provider 可提前驱逐缓存，无法保证命中。Codex、DSH 没有可用的 TTL 写入桶，须按实际 provider 设置配置估计值；示例数字不是它们的官方保留保证。余量大于或等于观测 TTL 时动态限制为 TTL 的一半。

成功信号分别为 Claude `compact_boundary`、Codex `contextCompaction` item 加成功结束的 turn、DSH 原生 `compactNow()` 成功返回。DSH 返回 null 表示暂时没有可压缩历史，记录 `compact_no_history`。不会通过让模型总结来假装原生 compaction。Claude 压缩 result 的 usage 可能全零，不能用它证明压缩命中了缓存。

## Harness 差异

| Harness | 常驻接口 | plan | auto | 手动压缩 |
| --- | --- | --- | --- | --- |
| Claude | `-p --input-format stream-json --output-format stream-json --verbose` | 原生 plan 权限模式 | 原生 auto 权限模式 | 同一输入流发送 `/compact` |
| Codex | `app-server --listen stdio://` | 原生 collaboration plan + read-only sandbox | default + workspace-write / never | `thread/compact/start` |
| DSH | `--profile sdk --patch <临时插件配置>` | 原生 `planMode` | 原生普通模式，审批 policy 为 never | 原生 `compaction.compactNow` |

DSH 自身的 plan mode 是提示词指导，**不是文件系统只读隔离**。三家的 mode 不能提供完全相同的权限保证。无人值守请求不会等待 caller 回答：Claude 使用 `--permission-prompts none`；Codex 拒绝交互审批并返回空问题答案；DSH 沿用无交互通道的 fail-closed 行为。

Claude 固定关闭外部 MCP 配置，避免递归连接本 MCP。Codex 和 DSH 继承本地 harness/profile 配置，部署时需确认其中没有递归调用本 MCP 的配置。登录与凭证由官方 harness 自己管理，本项目不读取、保存或转发登录凭证。

DSH SDK 公开 wire 没有 plan/compact 控制，本项目的 `dsh/bridge.mjs` 插件只调用公开 agent、plan、compaction 服务，并复用官方 JSON-RPC transport。它不实现模型循环或压缩算法。通过插件生成临时 patch，退出时只移除自己创建的文件；不修改安装包或用户 profile 配置。DSH CLI 首次使用 sdk profile 时可能按自身行为初始化该 profile。DSH 插件 API 处于预览期，升级后应重跑端到端验证。

取消、请求超时、协议错误会丢弃当前 harness 进程，下一次调用创建新会话；不会继续使用半读的协议流。Windows 使用 kill-on-close Job Object，Unix 使用独立进程组，清理 harness 及其子进程。Windows harness 以 Idle 优先级、隐藏窗口启动。

## 验证

```powershell
cargo fmt --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --locked
cargo build --locked
python tests/integration.py
```

Python 仅用于无 provider 的集成测试，运行 MCP 无需 Python。集成测试使用独立假 CLI，覆盖三个适配器的 wire framing、参数、复用、并发响应、单次压缩、窗口错过、取消和失败恢复。真实 CLI 验证与证据摘要见 `docs/verification.md`。

`python tests/live_smoke.py config.local.json` 会使用真实 provider 额度，验证多轮、原生压缩、压缩后续接；仅在需要真实验证时运行。可用 `--binary` 指定二进制、`--output` 保存原始结果。测试临时把压缩触发提前到请求开始后 30 秒，不修改原配置。

协议来源：[Claude CLI](https://code.claude.com/docs/en/cli-reference)、[Claude 缓存](https://code.claude.com/docs/en/prompt-caching)、[Codex App Server](https://learn.chatgpt.com/docs/app-server)、[DSH 插件协议与服务](https://github.com/deepseek-ai/deepseek-harness/tree/d743267388641bc76f17c45ce8b4c231aed1d32c/packages)。
