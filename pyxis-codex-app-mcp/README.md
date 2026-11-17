# pyxis-codex-app-mcp

将本机正在运行的 Codex app MCP 接到 Claude Code / Claude Desktop。只用 Node.js 内置模块：启动已经安装的官方 `codex-app-tools/server.mjs`，转发 stdio，并给工具调用补上默认聊天上下文。工具目录、参数、执行和取消仍由官方 MCP 处理，不复制工具实现，不启动模型、HTTP 服务或后台守护进程。

这是基于当前 Codex app 内置接口的本机适配，未声明为官方支持的第三方集成；目前只支持 Windows。连接发现沿用 CHIMERA 试听台已使用的规则：优先使用仍存在的继承管道，否则只选唯一的 `codex-app-tools-<UUID>` 命名管道。插件从 `CODEX_HOME`（默认 `~/.codex`）下选最新已安装版本，不复制插件或固定版本目录。

## 配置

两种客户端均使用以下 `mcpServers` 条目，将 Node 和脚本路径替换为本机绝对路径。Claude Code 使用 `claude mcp add-json --scope user codex_app '<条目 JSON>'`；Claude Desktop 在 `%APPDATA%/Claude/claude_desktop_config.json` 的 `mcpServers` 中合并条目。

```json
{
  "mcpServers": {
    "codex_app": {
      "command": "C:/path/to/node.exe",
      "args": ["I:/code/Pyxis/pyxis-codex-app-mcp/server.mjs"],
      "env": {
        "CODEX_APP_BRIDGE_THREAD_ID": "<已有的本机 Codex 占位聊天 UUID>"
      }
    }
  }
}
```

建议使用专门的占位聊天。它只需完成一次简短回复后保持空闲，无需持续运行 agent。`CODEX_APP_BRIDGE_THREAD_ID` 仅提供调用者上下文：工具参数里的目标 `threadId` 原样转发，因此仍可读写其他聊天。涉及“当前聊天／项目／工作区”的工具以占位聊天为准。不要删除这个占位聊天；更换时更新两个客户端的环境变量。

首次配置后重连 Claude Code MCP，并让 Claude Desktop 重新加载 MCP 配置。使用时保持 Codex app 打开；Codex 重启后重新连接 MCP，以发现新的管道。多个 Codex app 实例同时运行时，需显式配置 `CODEX_APP_TOOLS_PIPE_PATH` 选择实例。适配和子进程均降到 Idle 优先级，客户端断开时退出所拥有的子进程。

`list_threads`、`list_archived_threads`、`read_thread` 支持列出和翻页阅读聊天；没有独立的全文搜索工具。`send_message_to_thread` 等写操作仍遵循各工具说明中的用户授权要求；此适配不改客户端的审批设置。依赖当前窗口、审批交互或语音状态的工具仍受 Codex app 自身条件限制。

## 已验证范围

2026-11-17，Windows / Codex app 26.1111.2228.0 / 内置插件 0.1.6：在清除继承的 Codex 环境变量、仅保留占位聊天配置后，完成 MCP 握手，发现 60 个原生工具，并实际调用 `list_threads` 和 `read_thread`。Claude Code 的用户级 MCP 健康检查返回 Connected；Claude Desktop 5.11492.5 的 Extensions → Local MCP servers 显示 `codex_app` 为 Running，Arguments 指向本仓库的 `server.mjs`。未通过批量写操作测试工具；未把工具可发现视为所有桌面交互均已验收。
