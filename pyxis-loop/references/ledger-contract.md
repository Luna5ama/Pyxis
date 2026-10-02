# Roadmap format and commands

## State and format

Track `.pyxis/scripts/roadmap.py` and `.pyxis/roadmaps/<slug>/` in the owner worktree. Roadmap Markdown is task state; inspector output is a disposable view. The runtime uses the existing `pyxis-loop/v2` schema.

| File | Owns |
|---|---|
| `.owner` | SHA-256 of the owning CODEX_THREAD_ID; no raw ID |
| `CONTROL.md` | Goal, continuation protocol, queue order/status, dependencies, task paths, repository keys, expected subjects |
| `tasks/Txx.md` | Scope, non-scope, acceptance, verification, blockers, evidence; matching heading and Repository field |
| `policies/repositories.md` | Repository/worktree/branch/baseline mappings and protected changes |
| `policies/constraints.md` | Task-specific permissions and constraints |
| `policies/acceptance.md` | Final global checkbox checklist |
| `history/completion-log.md` | One concise receipt per completed/superseded task |
| `history/ownership-log.md` | Explicit transfer audit, created only on transfer |

CONTROL requires Schema, Roadmap ID, Owner repository, Created, Source handoff and Runtime inspector fields; Goal, Continuation protocol, Required reads, Status board and Queue rules sections. Its table has ID, Priority, Repository, Task, Status, Dependencies, Task file, Expected commit title columns. Task documents require Repository, Required reads, Primary files, and Scope, Non-scope, Acceptance, Verification, Blockers, Evidence sections. Generate these from the bundled templates rather than rebuilding the schema by memory.

New placeholders use `{{PYXIS:NAME}}`. Known placeholders from old templates, such as `<OWNER_REPOSITORY_KEY>`, are also rejected until filled; arbitrary business metavariables such as `<ALIAS>` are allowed. Existing v2 roadmaps need no automatic rewrite.

Required reads accepts inline comma-separated paths or a multiline bullet list, all relative to the roadmap. The three core policy files are mandatory. Put source, local instructions, external specifications and future outputs in Primary files/Verification; do not copy them into the roadmap merely to satisfy path checks.

| Status | Meaning |
|---|---|
| READY | First nonterminal task; dependencies terminal; executable now |
| PENDING | Waiting for queue order or dependencies |
| BLOCKED | Named external condition, with exact unblock requirement |
| DONE | Verified scope and evidence recorded; completion checkpoint must be committed before later work |
| SUPERSEDED | Replaced by a named completed task with a recorded reason |

Table order is serial. Normally one READY exists; zero is valid only when the first nonterminal task is BLOCKED or all tasks are terminal. Dependencies refer backward. Keep final audit last, depending on all required work directly or transitively. Do not rewrite completed history to hide failures.

`check` validates the whole roadmap; `inspect` adds the current task, policies, required reads, dependency summaries and recent receipts. Default packet limits are 800 lines and 48,000 UTF-8 bytes. Aim for <=300 CONTROL lines and <=200 per task/policy. PASS does not execute tests or prove Git completion. Standalone Pending evidence markers and unchecked acceptance boxes reject DONE.

## State transitions

Use the repository runtime after reviewing and syncing it when necessary. These commands validate the complete candidate before publishing changes. They do not stage or commit.

```text
python .pyxis/scripts/roadmap.py transition <roadmap> --task T01 --to DONE --evidence-file <utf8-evidence.md> --summary "tests and acceptance results"
python .pyxis/scripts/roadmap.py transition <roadmap> --task T01 --to BLOCKED --blocker "exact condition needed to resume"
python .pyxis/scripts/roadmap.py transition <roadmap> --task T01 --to READY
```

DONE replaces Evidence with the supplied body, appends one receipt and promotes the next PENDING task. Evidence should contain verification commands/results and stable artifact references, not raw logs. Check acceptance boxes only when actually satisfied; the command does not check them automatically. Update any task-specific acceptance text before transition. The input evidence file is temporary input, not another state store.

Only the first nonterminal task may transition: READY to DONE/BLOCKED, or BLOCKED to READY. Retry after a successful transition does not create a duplicate receipt. For external tasks, supply `--commit <verified-full-sha>`; verify the actual external commit and diff before recording it. For owner tasks, the receipt uses `this task's commit`, resolved from Git later.

For a newly discovered defect, prepare a complete task file using the T01 template, then:

```text
python .pyxis/scripts/roadmap.py insert <roadmap> --before T07 --task-file <T07A.md> --commit-title "T07A repair validation defect"
```

The command inserts the new row/file before T07, inherits T07's prerequisites, and adds the new dependency to T07. The inserted task becomes READY only if T07 was current. An existing blocker on T07 remains BLOCKED after the remediation finishes; resume explicitly when its condition is satisfied. Duplicate IDs, invalid files/dependencies and oversized packets fail before publication. Review and commit the queue change before implementation.

If completed remediation fully replaces a task, record the authorized scope change:

```text
python .pyxis/scripts/roadmap.py transition <roadmap> --task T07 --to SUPERSEDED --replacement T07A --summary "why completed T07A replaces this task"
```

Replacement must be an earlier DONE task. Keep the explicit final audit DONE, not SUPERSEDED. More extensive re-planning may edit Markdown directly, followed by check/inspect before a control-plane commit; preserve completed history and protected changes.

## Interrupted transactions

Transitions, insertion and transfer use a short-lived journal at `.pyxis/transactions/<slug>/`, containing before/after bytes and the initiating owner's hash. The journal is recovery data, not a second live task state. It disappears on success; do not stage it. OS advisory lock files live in the system temp directory and contain no task data. Locks release on process exit.

Publication uses per-file replacement with a journal gate: readers and other runtime mutations reject a pending transaction, so a partial multi-file write cannot be treated as valid state. This is recoverable publication, not a filesystem-wide atomic rename or protection against arbitrary external editors.

```text
python .pyxis/scripts/roadmap.py recover <roadmap> --action apply
python .pyxis/scripts/roadmap.py recover <roadmap> --action rollback
```

Use apply to finish the previously validated candidate, rollback to restore its exact prior bytes. Only the initiating chat can recover, including a transfer whose owner marker changed before interruption. Recovery is restartable and refuses to overwrite files edited outside the transaction. Reconcile such files before retrying; do not delete the journal to bypass the gate. After recovery, inspect and review the diff before any commit. Do not commit a half-finished transaction or infer task completion from its log.

## Ownership transfer

Ordinary check/inspect/close and transitions require the current owner. A copied Goal, a generic “continue,” or opening the same worktree is not a handoff. Transfer is only for an explicit user request to take over this named roadmap. Resolve its worktree and original chat from the supplied context, read only its owner marker and Git metadata before transfer, and confirm the original executor will not keep modifying it.

```text
python .pyxis/scripts/roadmap.py transfer <roadmap> --from-owner <old-sha256> --confirm <slug> --reason "user explicitly asked this chat to take over" --authorized
```

The flag records the caller's assertion of existing authorization; it does not authenticate the user. The command requires the old digest to match, a committed clean roadmap, and a valid candidate under the new owner. It changes only `.owner` and the append-only ownership audit, retaining old/new hashes and the authorization reason. It never changes CODEX_THREAD_ID or raw IDs in Git. It cannot resurrect a closed roadmap.

After transfer, inspect, recheck live branches/dirty state and jobs, and commit the ownership audit before product work. The old owner is then refused. A dirty original roadmap must first be reconciled with its existing work; do not stash or discard it to force transfer.

## Close and recovery

`close <roadmap> --confirm <slug>` requires valid terminal tasks, final task DONE, a checked global acceptance checklist, a fully tracked clean roadmap, and its latest touching commit subject matching the final audit's expected subject. It rejects linked paths and staged, unstaged, untracked or ignored content inside the roadmap. Unrelated dirty files outside it are preserved. Commit the resulting deletion separately as `pyxis: close <slug>`, then complete the Goal.

Recover closure using Git metadata: existing terminal state needs audit commit/close; verified audit plus pending deletion needs cleanup; verified cleanup plus absence needs Goal completion. Never restore, reopen or reuse a closed roadmap. Follow-up product work is separate.

## Legacy migration and scheduling

For an oversized old ledger, create a distinct owned roadmap. Preserve its active goal, constraints, repository mappings, protected state and nonterminal tasks; reduce completed work to concise receipts. Record the source path/hash. Preserve the old ledger unless removal was authorized. Validate and commit the migration before product work.

Reuse the existing Goal when it still accurately locates the task. Update its objective only if the available tool supports doing so. If it cannot be edited, state that limitation, keep the old context as the migration locator and continue authorized work manually; do not pretend a status update rewrites the objective, create competing schedulers, or mark incomplete work complete to replace the Goal. A new owner's scheduler requires the user's handoff/persistent-execution authorization and must not compete with the original executor.
