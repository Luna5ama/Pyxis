# Repository and worktree policy

## Repositories

| Key | Repository root | Worktree | Branch | Baseline HEAD |
|---|---|---|---|---|
| `{{PYXIS:OWNER_REPOSITORY_KEY}}` | `{{PYXIS:ABSOLUTE_OWNER_REPOSITORY}}` | `{{PYXIS:ABSOLUTE_OWNER_WORKTREE}}` | `{{PYXIS:EXACT_OWNER_BRANCH}}` | `{{PYXIS:FULL_BASELINE_SHA}}` |

Add one row for every repository named by the status board. Re-read that repository's local instructions before editing it.

## Protected and user-owned state

| Repository | Path or state | Owner | Required handling |
|---|---|---|---|
| `{{PYXIS:OWNER_REPOSITORY_KEY}}` | `{{PYXIS:PATH_OR_DIRTY_STATE}}` | `{{PYXIS:USER_OR_TASK}}` | `{{PYXIS:PRESERVE_OR_ALLOWED_ACTION}}` |

## Repository protocol

- Verify repository root, worktree, branch, HEAD, worktree registration, and dirty state at every continuation.
- Preserve all pre-existing and unrelated changes.
- Stage exact paths; never use a whole-tree add when unrelated changes exist.
- Commit each repository independently.
- Record an external repository commit in the owner roadmap during a later owner-repository continuation.
