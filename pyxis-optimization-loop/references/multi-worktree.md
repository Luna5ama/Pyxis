# Multi-Worktree Optimization

Use this mode only when the user explicitly requests parallel or multi-agent
optimization and supplies at least two Git worktree paths plus one unambiguous
target branch.

## Entry gate

- Stop before inspecting code, creating a goal, changing Git state, or spawning
  workers unless at least two distinct worktrees and one target branch are
  supplied.
- Verify that every supplied path is a safe worktree of the intended repository
  and that the target branch exists. Do not substitute paths or choose a branch.
  Stop if fewer than two worktrees remain usable.
- Keep the target branch free from unrelated attached worktrees during the run.
  Supplied workers detach before working; stop rather than detach or move an
  unrelated checkout.
- Do not detach, move, clean, or reuse an unrelated checkout. Supplied worker
  worktrees must be safe for the requested run.
- Spawn exactly one persistent worker for every usable supplied worktree. The
  coordinator does not consume a supplied worktree. Give each worker the
  absolute path to [worker-prompt.md](worker-prompt.md), its worktree, target
  branch, optimization boundary, measurement contract, and coordinator handle.

## Coordination

- The coordinator owns run tracking, disjoint boundary assignment, shared
  runtime scheduling, and report collection. It does not select candidates,
  implement changes, integrate commits, or resolve conflicts.
- Each worker owns candidate selection, implementation, correctness checks,
  measurement, conflict resolution, publication, and cleanup inside its assigned
  boundary and worktree. Reuse the same worker across rounds.
- Serialize access to a shared measurement runtime unless independently isolated
  runtimes are confirmed.
- Keep the scheduler active in the same long-running turn until the requested
  duration or round count and final audit finish. Worker or round completion is
  not a turn boundary.

## Round start and baseline

- At the start of a new round, each worker verifies the current target baseline
  and uses it detached in its supplied worktree. Clean up only owned candidate
  state; do not repeat checkout when already current or reset a live candidate.
- The target branch is the only accepted baseline. Every published win advances
  it; all later comparisons and replays use the new tip.
- One synchronized round ends after all scheduled workers report accepted,
  rejected, or blocked results. A round may publish multiple separately verified
  commits.

## Publication

- Reserve the integration slot before replay and final confirmation so workers
  do not repeatedly invalidate one another by advancing the target. The responsible
  worker inspects intervening changes, replays onto the current tip if needed,
  resolves conflicts, and checks affected correctness and performance obligations.
  Reuse evidence only when the relevant artifact and conditions still match.
- The worker creates one narrow commit, then advances the target branch with a
  compare-and-swap fast-forward from the exact tip it integrated against. Never
  force or overwrite the target branch.
- If the target changed despite coordination, retain the patch and evidence,
  inspect the delta, and recheck affected obligations before retrying. Do not
  blindly repeat every gate until a publication race happens to succeed.
- Rejected or blocked work produces no commit.

## Finish

Audit the target using applicable evidence and checking uncovered obligations.
Collect or clean up in-flight worker-owned state, confirm every accepted commit is reachable from
the target branch, and leave supplied worktrees detached with unrelated state
untouched. Report the final tip, commits, rejected candidates, measurements,
conflicts, worker statuses, and blockers.
