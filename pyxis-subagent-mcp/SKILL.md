---
name: pyxis-subagent
description: Use Pyxis MCP to delegate work to Claude Code, Codex CLI, or DeepSeek Harness and manage independent agent sessions. Recommended for planning with opus-plan, especially in Plan Mode.
---

# Pyxis Subagent MCP

Rust stdio MCP exposing installed native harnesses. Available tools and defaults depend on the current connection.

When using this skill in Plan Mode, delegate planning to the `opus-plan` subagent.

- Use specialist tools such as `opus-plan` for their advertised scope. They accept only `prompt`, return synchronously, reuse private history, and use unattended interaction handling.
- Use `agent_models` for native model and effort metadata at a specified `harness` and absolute `cwd`. Catalogs describe capabilities, not account entitlement; an unlisted exact model can still be submitted.
- Use `agent_start` to choose harness, cwd, model, effort, provider (DSH only), and plan/auto mode. It immediately returns `session_id` and `task_id`. These settings are fixed for the session; create another session to change them. Omit effort for the local runtime default, or pass null for the native default.
- Each session owns an independent native conversation. All CLIs start lazily when work runs; idle processes exit after 15 minutes by default, and the next task resumes the same native context. Different sessions run concurrently; ordinary tasks within a session run FIFO. Use an existing worktree when isolation is needed; Pyxis does not create checkouts. Coordinate concurrent writes to the same directory.
- Use `agent_send` with `action: queue` for a later turn. For mid-task guidance, use `action: steer` with the current `task_id`. Check its acknowledgement: late guidance is rejected, and a lost acknowledgement means application is unknown. Claude interrupts and submits the correction under the same public task; Codex and DSH steer natively. Existing edits remain.
- Use `agent_list` / `agent_status` to inspect effective configuration, queued work, progress, results, and interactions. `agent_wait` waits for up to eight tasks, at most 60 seconds per call. Intermediate output is progress; only a terminal task state is completion.
- Answer pending approvals and questions through `agent_respond`, preserving native options, parameter scope, and answer constraints. Decide from the user's existing authorization; ask the user for missing decisions. Delegation does not expand authorization, and a timeout is never consent. Never automatically approve requests or replace native permissions policy.
- `agent_cancel` stops one task, `agent_close` cancels its session and cleans the process tree, and `agent_compact` queues native compaction. Confirm the cancellation outcome before reusing a session. Invalid sessions explicitly fail queued tasks and require a new session.
- Submissions accept `idempotency_key` for bounded in-process retry protection. Use the same key and arguments to retry an uncertain submission. Canceling an MCP wait does not cancel the accepted task.
- Events use bounded cursor pagination. Continue from returned `cursor` while `has_more`; inspect `cursor_expired` and `unknown_or_expired` instead of assuming complete history. Usage and available cost carry explicit scopes; cumulative cost is not a per-turn charge.

Harnesses inherit native tools, plugins, project settings, credentials, and permissions. Codex reports effective sandbox and approval settings; its plan sessions are read-only. DSH loads Desktop configuration, and its native plan mode is soft guidance. Native errors remain visible; Pyxis does not substitute models or reduce effort. Nested Pyxis execution tools are suppressed to prevent recursion.

Sessions and results belong to the current MCP process. Idle CLI processes stop while valid session identities remain until explicitly closed or MCP shutdown; suspended sessions still count toward the session limit. Resume failures are explicit and never fall back to a blank conversation. Stopping MCP stops all owned processes. Native compaction can lose context and does not guarantee provider cache hits.
