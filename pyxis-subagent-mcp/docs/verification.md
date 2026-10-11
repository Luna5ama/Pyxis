# 验证记录

2026-10-11，Windows，本机已登录的官方 harness。验证使用 release 构建；编译和测试以 Idle 优先级运行。

## 构建与无凭证测试

以下命令全部通过：

```powershell
cargo fmt --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --locked
cargo build --locked
python tests/integration.py
cargo build --release --locked
```

5 项 Rust 测试覆盖取消安全的分帧、超长/截断帧、RPC 响应匹配、usage TTL 推断及提前压缩时间计算。10 项集成测试覆盖三个适配器、延迟初始化、进程内复用、MCP 进程间隔离、单次压缩、运行期间互斥、错过窗口、活动/排队请求取消、请求 ID 重用、超时和协议失败后的新会话、Windows 子进程树清理。

## 真实 provider 验证

```powershell
python tests/live_smoke.py config.all.local.json --output .research/final-live-30s.json
```

测试将三个 tool 的 TTL 临时设为 180 秒、余量 150 秒，关闭 usage TTL 覆盖，触发点为最近任务开始后 30 秒；不修改原配置。每个 tool 都完成以下检查：首次发送随机 marker，第二轮回忆 marker，空闲时执行原生 compact，压缩后第三轮仍回忆同一 marker，三轮 session ID 相同。关闭 MCP stdin 后进程正常退出。

| Tool | Harness / 模型 / effort | 多轮 | 原生 compact | 压缩后续聊 |
| --- | --- | --- | --- | --- |
| `opus-plan` | Claude Code 2.1.296 / `claude-opus-5-5` / high | 通过 | `compact_boundary` | 通过 |
| `codex-plan` | Codex CLI 0.161.0 / `gpt-6.1-sol` / low | 通过 | `contextCompaction` + completed turn | 通过 |
| `dsh-plan` | DSH Desktop 0.2.0-rc.2 / `command-1` / `deepseek/deepseek-v4.1-flash` / 默认 | 通过 | 原生 `compactNow` 成功 | 通过 |

Codex 使用用户指定的 `C:/Users/Luna5ama/AppData/Local/Microsoft/WinGet/Links/codex.exe`。DSH 复用现有 Desktop provider patch；该路由拒绝显式 high，因此使用 `reasoning_effort: null`。

三个 provider 均在第二轮和压缩后第三轮报告了非零缓存读取。字段分别为 Claude `cache_read_input_tokens`、Codex `last.cachedInputTokens`、DSH `cacheReadTokens`。具体数值和测试二进制 SHA-256 见 [verification.json](verification.json)。这些值说明相应任务报告了缓存读取，不能证明压缩请求自身命中缓存，也不能作为不同 provider 的计费比较。

完整本地原始证据在忽略版本控制的 `.research/final-live-30s.json`；其中含每轮响应、原始 usage、session ID 和带时间戳的压缩日志。测试脚本可重新生成记录。

## 验证边界

- 三种真实 harness 均验证了 plan 模式；没有用真实模型执行 auto 模式修改文件的任务。
- DSH 原生 plan 是提示词指导，不提供文件系统只读隔离。
- Claude 观测到 1 小时 cache creation 桶，但没有等待 1 小时验证驱逐时间。Codex、DSH 的真实缓存 TTL 未验证，配置值只是估计。
- 验证了压缩定时和完成信号，没有测量压缩的实际订阅费用或长期节省比例。缓存可能提前驱逐，压缩可能丢失信息。
- Windows 已实际验证；Unix 分支没有在本次 Windows 环境中运行。
- 未做性能基准。约 1 MiB 的 release 二进制、单线程 runtime 和常驻 CLI 复用是实现属性，不代表已测量的延迟或内存承诺。
