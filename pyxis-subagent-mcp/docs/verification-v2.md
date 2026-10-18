# 三后端多会话验证与本机部署

2026-10-15，Windows。版本 `0.2.0`。编译、检查及合成验收均使用 Idle 优先级；shell 使用 `pwsh -NoProfile`。完整机器记录及原始合成报告位置见 [verification-v2.json](verification-v2.json)。

## 首次完整升级版本

| 项目 | 结果 |
| --- | --- |
| Release 二进制 | `target/deploy/pyxis-subagent-mcp-0.2.0-8131b422204ed741.exe` |
| SHA-256 | `8131b422204ed7412ac33db6ac0fddbb6c74de24c8e51096359f4b5672900b3a` |
| 独立构建目录 | `cargo build --release --locked --target-dir target/candidate` |
| Claude Code | `2.1.296`；`claude-opus-5-5 / high` |
| Codex CLI | `0.161.0`；`gpt-6.1-sol / medium` |
| DSH | `0.2.0-rc.2`；实际 Desktop provider `command-1`，`deepseek/deepseek-v4.1-flash / max` |
| 本机注册 | `J:\.codex\config.toml`，与 `C:\Users\Luna5ama\.codex` 的引用解析到同一目录 |
| 运行配置 | `I:\code\Pyxis\pyxis-subagent-mcp\config.v2.local.json`；保留旧 specialist 定义与缓存配置 |
| 工具白名单 | 原有 `opus-plan`、`exec-low`，新增十个 `agent_*` 管理工具 |
| Companion skill | 源文件和已安装副本字节一致，官方校验脚本通过 |

旧 executable 保留在原 `target/release` 路径，SHA-256 为 `02b9574863395288e70cd8acfe214be6fc0c5c3e239d7bc917276723a0e1fa61`。部署脚本确认安装后其哈希不变。

## 自动检查

- `cargo fmt --check`、Clippy `--all-targets --locked -- -D warnings` 通过。Cargo 在此文件系统上提示增量缓存不能创建 hard link，退回复制；没有 Clippy 源码诊断。
- 10 项 Rust 单元测试通过：分帧与取消、RPC 关联、缓存 TTL、v2/旧配置、preset 继承、未知字段及 Windows canonical cwd。
- 完整升级验收 Release 上 10 项旧接口集成测试、20 项新会话集成测试通过：并发、隔离、FIFO、共享容量、幂等、交互、超时、取消、竞态、压缩、分页、回收及客户端断连后的实际进程树清理。
- DSH 实际插件的服务测试通过：正确创建参数与注入作用域、原生问题工具注册、固定模式、审批往返、运行时控制、子任务完成屏障、usage、压缩与清理。
- 合成注册文件的部署预检、安装、保留无关配置字节/超时、skill 更新及回退通过。

## 真实原生验收

全部任务仅使用合成目录、测试文件和命令，禁止读取项目源码、访问网络、调用 MCP 或委派任务。验证检查实际文件内容、原生命令事件、独立上下文、进程句柄退出及取消后未执行延迟写入。初始化成功不作为任务执行成功。

| 能力 | Claude | Codex | DSH |
| --- | --- | --- | --- |
| 模型发现、默认路由实际请求 | 通过 | 通过 | 通过，保留 max |
| 真实读写、命令、原生压缩、续接 | 通过 | 通过 | 通过 native_cycle；另有下述 provider 失败 |
| 双会话并发、不同 cwd、上下文隔离 | 通过 | 通过 | 有成功记录；最终版本重测另一次收到 400 |
| 原生结构化问答 | 通过 | 通过 | 通过 |
| 中途引导、取消、同会话续接 | 通过 | 通过 | 通过 |
| 审批允许/拒绝、拒绝后未写文件 | 通过 | 通过 | 通过 |

Claude 和 Codex 的完整核心流程、DSH 的命令/压缩与审批、部署后新连接均已在完整升级验收 Release 哈希上验证。其他 DSH 控制、问答、并发成功证据来自此前候选；每份报告记录独立二进制哈希，不把这些成功记录混写成最终版本全面稳定的结论。

Claude 真实问答需要 SDK host 身份和 stdio 权限回调。DSH 的创建 setup 必须位于顶层，其原生工具回调需要保留注入作用域。Codex 的 `turn/interrupt` 可留下 unified-exec 终端，因此取消完成前通过原生终端列表与 terminate 接口清理归属于当前任务的终端，不清理前轮的后台服务。

审批验收策略与部署默认值分开：Codex 合成测试使用更保守的 `approvals_reviewer=user`，并只批准明确的合成命令；本机部署仍继承原生配置。DSH 审批测试通过仅对测试进程生效的 overlay，将原生 preset 收紧到 read-only + ask；未修改 Desktop 的全局设置。运行配置与有效 sandbox/审批策略均可查询。

## 已知本机限制

DSH 当前 `command-1` provider 在部分工具结果后返回 HTTP 400 `invalid_request_error`。同样的读文件后续任务在不加载 Pyxis 的已安装官方 SDK 上复现，trace ID `ea4ad274e45f7cf18d95e8dbe4fb628c`。原生 SDK 和 Pyxis 的 max 纯文本、基础读写及多个工具流程已成功，因此不能把此错误解释为 max 不支持。Pyxis 保留原始错误并使失败会话失效，不降低 effort、不更换模型、不静默开启空白会话重试。

Codex 当前 Windows sandbox helper 对部分命令报告 `setup refresh had errors`。合成测试由模型按原生审批机制请求重试同一条明确命令，并检查其成功执行；没有禁用 sandbox 作为回退。本机部署保留原生策略，此环境问题仍可能出现在实际命令中。

## 部署与新连接

部署仅改 Pyxis MCP table 的 command、args、enabled_tools，其他配置字节保持不变；安装 companion skill。原注册 table、旧二进制与哈希保存在：

`I:\code\Pyxis\pyxis-subagent-mcp\.research\deploy\d1c37ea2022546418eaa51497e048075\manifest.json`

回退命令：

```powershell
python scripts/deploy.py --codex-config C:/Users/Luna5ama/.codex/config.toml --rollback .research/deploy/d1c37ea2022546418eaa51497e048075/manifest.json --apply
```

`codex mcp get pyxis-subagent-mcp --json` 确认已启用且指向新二进制及 12 个工具。部署后新建直接 MCP 连接再次发现全部工具，三家用实际部署默认值分别完成 PONG 请求；报告 ID `39e544dd-74fa-4de9-b277-5390a993e8e3`。

当前聊天持有此前的 MCP 工具目录，仍只有两个 specialist。重连 Pyxis MCP 或新开聊天后使用新目录；本次没有中断正在使用的旧连接。

## 后续更新：DSH 400 原生重试

已安装带重试策略扩展的独立二进制 `pyxis-subagent-mcp-0.2.0-c5a00b8b16d38341.exe`，SHA-256 `c5a00b8b16d38341ec985b19ba48ee0c5754171e5f899c520f27e18dbb75a1ae`。前述 `8131b422204ed741` 版本作为回退保留。最近一次部署 manifest 为 `.research/deploy/bd0526db7ce24b16bc070678b827e9e1/manifest.json`；先用此 manifest 可回退本次重试改动。

薄插件只在本会话的原生 `agent/request-error` 扩展点给 normal policy 加入 `INVALID_REQUEST`。重试次数、退避、jitter、禁用设置、计数与取消仍由已安装的 DSH 原生插件负责。当前默认最多重试 5 次，约 0.5/1/2/4/8 秒退避；不改变模型、effort 或凭证，也不重新提交整轮 prompt。

真实失败任务观察到同一原生 turn/step 的 5 次 `llm/retry` 和 5 次 `llm/retry-started`，前面的 read 工具只调用一次。5 次全部仍返回 400，耗尽后明确失败。此结果验证了自动重试和不重放工具，不能宣称 provider 故障已经恢复。证据 `target/retry-acceptance/1d8a9fea-43c1-49d0-9af8-d362267c6d72/report.json`，可用 `python tests/retry_evidence.py REPORT` 校验。格式、Clippy、插件服务策略测试和部署回退测试通过。

更新后的新连接再次发现 12 个工具并完成 DSH PONG。另一次拟验证退避取消的合成请求直接成功，没有触发重试，因此不计为本次退避取消的真实验收。
