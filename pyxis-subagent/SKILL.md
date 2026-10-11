---
name: pyxis-subagent
description: Pyxis specialist MCP capabilities. Recommended for planning with the opus-plan subagent, especially in Plan Mode.
---

# Pyxis Subagent MCP

Rust stdio MCP exposing Claude Code, Codex, and DeepSeek Harness as independent specialist tools.

When using this skill in Plan Mode, delegate planning to the `opus-plan` subagent.

- Each tool has its own description, model, reasoning effort, plan/auto mode, and fixed prompt prefix. Available tools depend on the current MCP connection.
- Tools accept a `prompt` and return the final response, plus usage and cost information when supplied by the harness.
- Harnesses start lazily. Each tool reuses its session and history and serializes requests; different tools can run concurrently. Sessions belong to the MCP process, with no caller-managed session IDs.
- Idle sessions can use native context compaction before estimated cache expiry. Cache hits depend on the provider, and compaction can lose information.
- Requests support cancellation and timeouts. Harness failures discard the affected process; subsequent calls create a fresh session.
