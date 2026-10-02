---
name: pyxis-loop
description: Run explicitly requested persistent implementation loops using compact Git-tracked roadmaps, task ownership, recoverable state transitions, and verified completion commits. Use when invoked or when the user asks to start or continue such a loop. Reviewing this skill does not start a loop.
---

# Pyxis Loop

The roadmap is durable task state; one Goal schedules continuations; a Plan holds disposable steps; Git commits preserve completed work. Work on one READY task at a time. A task may span continuations, and monitoring alone does not require a commit.

## Enter or initialize

Use only the roadmap named by this chat's context or Goal. Do not scan `.pyxis` for work to adopt. The runtime checks `CODEX_THREAD_ID` against `.owner` before reading content. On mismatch, stop ordinary execution; an explicit user-authorized handoff uses `transfer` as described in the [command contract](references/ledger-contract.md#ownership-transfer).

Verify the exact worktree, branch, HEAD, local instructions, and protected dirty state. Respect the user's existing commit and execution authorization; a draft/review request creates no Goal or product commits.

For a new roadmap, read the [format and command contract](references/ledger-contract.md), then run:

```text
python <skill-dir>/scripts/pyxis_roadmap.py init --repo <owner-worktree> --slug <slug> --project "<title>"
```

Fill the scaffold's reserved placeholders, repository mappings, constraints, observable acceptance and executable verification. T00 records setup; the last task is the integrated audit. Keep later tasks PENDING with dependencies in table order. `Required reads` contains roadmap-relative inputs only; source files, AGENTS.md and external specifications belong in Primary files/Verification and are read separately.

Validate with `check` and `inspect`. If authorized, complete T00 using `transition`, commit the control plane, and activate the Goal before ending. If the user requested initialization followed by a stop, leave scheduling inactive. A differing repository runtime requires reviewing its diff before `sync-runtime --repo <worktree> --replace`.

## Schedule and resume

Create a Goal only for explicitly requested persistent execution. Reuse one that names this roadmap; never replace an unrelated unfinished Goal or set an unrequested budget. Suggested objective:

```text
Execute <absolute-roadmap> from <owner-worktree> in its owning chat.
On each continuation, reconcile interrupted mutation/close first, then inspect the roadmap and verify live Git/job state. Work on only the first READY task, verify acceptance, record completion and promote the next task using transition, commit exact scope, and end. A running job may span continuations without polling commits. BLOCKED follows the Goal tool's blocking rules. When all tasks are terminal, commit the final audit, close, commit cleanup, and complete this Goal. Never restore a closed roadmap.
```

If Goal creation or objective editing is unavailable, say so and continue authorized work in the current turn. Do not claim an autonomous scheduler exists. For migration, preserve the old objective as a locator until it can be updated through a supported tool; do not use a status-only tool to rewrite it.

At a normal continuation:

1. Run `python .pyxis/scripts/roadmap.py inspect <roadmap>` and capture its complete output. On a transaction error, use `recover` first. If already closed, use the closure recovery below instead.
2. Read the bounded packet and verify live Git identity, existing commits, protected changes, and any job's real status. PASS means structural validity, not proof that product tests passed.
3. Work only on the first READY task. Verify its actual acceptance; retain concrete commands/results, artifact hashes and limitations in Evidence.
4. Use `transition` to finish/block/resume, or `insert` for remediation. Review the resulting diff and rerun `check`. A completion commit includes product changes, task evidence, receipt and next-task promotion. Check staged scope/whitespace and post-commit HEAD/dirty state, then end.

Do not load every terminal task or all history. Shrink oversized packet components instead of raising limits. For interrupted completion, finish the missing commit/receipt after inspecting what already exists; do not repeat implemented work merely because the row still says READY.

## Long tasks and external repositories

Before launching a long job, record the command, worktree, input/tool hashes, checkpoint/output paths, completion criterion and a realistic check interval. Retain its job handle or PID plus creation time and command identity, logs and exit receipt. Verify liveness after resumption; a saved PID or an old “running” note is insufficient.

Reuse a matching running job; resume validated compatible checkpoints if it stopped. A tool timeout is not a job failure. Use bounded waits or available completion notification, avoiding immediate end/reawaken loops. Record meaningful phase, failure/recovery and completion evidence rather than each poll or counter increment. Run time-insensitive work at lowest process priority; keep benchmark measurement conditions intact. Diagnose stalled progress and follow the Goal tool's blocker threshold for a genuine external blocker.

For an external repository task, verify and commit there first. On the next owner continuation, verify its full SHA and use `transition --commit <sha>` to record the owner receipt. If the external commit already exists, finish only the receipt. Two repositories do not share an atomic commit.

## Finalize and recover closure

Audit global acceptance, task-to-commit mappings, protected state, and required generated/documentation/runtime results. Use targeted historical reads rather than adding every completed task to the packet.

1. Complete the final task and commit its audit with the expected subject. The roadmap must be fully tracked and clean.
2. Run `python .pyxis/scripts/roadmap.py close <roadmap> --confirm <slug>`.
3. After `CLOSE: COMPLETE`, stage only the roadmap deletion and commit `pyxis: close <slug>`. Verify absence, the cleanup commit and protected state, then complete the Goal. Retain the shared runtime.

After interruption, distinguish a terminal roadmap still needing its audit commit/close, verified audit history plus pending deletion needing cleanup, and verified cleanup plus absence needing only Goal completion. Use Git metadata and deletion status; do not restore or read removed content. Absence alone does not prove successful close. Later product requests are new work.

## Conditional operations

Read only the relevant part of the [command contract](references/ledger-contract.md) for ownership transfer, state transitions, interrupted transactions or legacy migration. Commands modify local roadmap files, not Git commits, Goals, running jobs, or global skill installs. Keep authorization and product acceptance decisions with the user and agent; the runtime enforces structural and recovery invariants.
