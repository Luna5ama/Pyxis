# {{PYXIS:PROJECT}} roadmap control

Schema: `pyxis-loop/v2`
Roadmap ID: `{{PYXIS:ROADMAP_SLUG}}`
Owner repository: `{{PYXIS:OWNER_REPOSITORY_KEY}}`
Created: `{{PYXIS:YYYY-MM-DD}}`
Source handoff: `{{PYXIS:PATH_OR_DESCRIPTION}}`
Runtime inspector: `.pyxis/scripts/roadmap.py`

This roadmap is durable task state. `.owner` binds it to its owning chat; another chat needs an explicitly authorized `transfer`. Inspector output is a disposable view.

## Goal

{{PYXIS:ONE_PARAGRAPH_OBSERVABLE_OUTCOME}}

## Continuation protocol

1. Recover an interrupted transaction/close before continuing; never restore a closed roadmap. Otherwise run `python .pyxis/scripts/roadmap.py inspect .pyxis/roadmaps/{{PYXIS:ROADMAP_SLUG}}` and read the complete bounded packet.
2. Require PASS, verify live Git identity, rules and protected changes, and reconcile existing commits/jobs.
3. Execute only the first READY task and verify acceptance. BLOCKED requires its exact unblock condition; all-terminal state goes to finalization.
4. Use `transition` for completion/block/resume or `insert` for remediation. Review and validate the resulting state; do not commit a pending recovery journal.
5. Stage exact task-owned scope, inspect the staged diff, commit a verified checkpoint, verify post-commit state and end. Monitoring alone does not require a commit.
6. After committing the final audit, run `close --confirm {{PYXIS:ROADMAP_SLUG}}`, commit only its deletion, verify cleanup and complete the Goal.

## Required reads

- `policies/repositories.md`
- `policies/constraints.md`
- `policies/acceptance.md`

## Status board

| ID | Priority | Repository | Task | Status | Dependencies | Task file | Expected commit title |
|---|---|---|---|---|---|---|---|
| T00 | P0 | `{{PYXIS:OWNER_REPOSITORY_KEY}}` | Persist `.pyxis` roadmap | READY | none | `tasks/T00.md` | `T00 persist .pyxis roadmap` |
| T01 | P0 | `{{PYXIS:OWNER_REPOSITORY_KEY}}` | `{{PYXIS:FIRST_TASK}}` | PENDING | T00 | `tasks/T01.md` | `T01 {{PYXIS:EXPECTED_SUBJECT}}` |
| T99 | P0 | `{{PYXIS:OWNER_REPOSITORY_KEY}}` | Final integrated audit | PENDING | T01 | `tasks/T99.md` | `T99 finalize {{PYXIS:PROJECT}}` |

## Queue rules

- Table order is serial: one READY, unless the first nonterminal task is BLOCKED or all tasks are terminal. Dependencies must be terminal before promotion.
- Complete at most one task per continuation. Long jobs can span continuations without polling commits.
- Keep scope/evidence in task files; insert remediation before its dependent validation.
- No ambient adoption or reuse after close. Only an explicit user-authorized transfer changes the owning chat.
