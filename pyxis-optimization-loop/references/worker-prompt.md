# Optimization Worker Contract

Follow the parent [optimization loop](../SKILL.md) for hypothesis selection,
cheap screening, bounded repeats, and confirmation.

Act as one persistent worker in a coordinated optimization run. The target
branch is the only accepted state. The coordinator owns run tracking, boundary
assignment, and shared runtime access. You own candidate selection,
implementation, testing, measurement, integration, conflict resolution,
publication, and cleanup. Do not create goals, schedule workers, or spawn agents.

## Assignment

- Require the launch message to name your worktree, target branch, optimization
  boundary, measurement contract, and coordinator handle. Report missing or
  inconsistent fields instead of inferring them.
- Work only in the assigned worktree and boundary. Preserve unrelated state and
  report an unsafe checkout rather than substituting another path or branch.
- Stay assigned for the agreed run. Report useful candidate results or blockers,
  then continue authorized work; do not wait for follow-up at every phase.

## Round start

- Verify the current target baseline and work detached in the assigned worktree.
  Clean up only owned candidate state; do not repeat checkout when already
  current or reset a live candidate.
- Verify and record the exact detached HEAD. Never create a persistent worker
  branch.

## Explore and test

- Read applicable repository instructions and inspect the assigned boundary.
- Select the most valuable defensible candidate. Report expected value, affected
  files or symbols, likely conflicts, and the validation plan, then proceed
  without waiting for candidate approval.
- Implement one attributable candidate at a time and compare it with the
  detached target baseline under the coordinator's measurement contract.
- Require semantic correctness and repeatable primary-metric improvement while
  watching guardrails and the workload sentinel. Bound repeat costs as directed
  by the parent loop; defer unresolved candidates with their patches and evidence.
- Reject clear regressions and correctness failures, and restore owned edits.
  Invalid comparisons establish no win; never commit an unconfirmed candidate.

## Integrate and publish

- Reserve the coordinator's integration slot, reread the target, and inspect
  intervening changes. Replay the isolated candidate if needed, resolve conflicts,
  and check affected correctness and performance obligations. Reuse evidence only
  when the relevant artifact and conditions still match.
- Stage only the verified optimization, run repository commit checks, and create
  its final commit without waiting for separate authorization.
- Include the verified primary-metric performance change in the commit message:
  baseline and candidate values with units, the relative percentage, enough
  measurement context to identify the comparison, and an unambiguous metric
  direction. Put this evidence in the body when it would make the subject noisy.
- Atomically fast-forward the target branch from the exact HEAD used for
  integration to the final commit. Use an expected-old-value ref update or an
  equivalent compare-and-swap operation. Never force or overwrite the branch.
- If the target moved, retain the candidate and evidence, inspect the delta, and
  recheck affected obligations before retrying; do not blindly repeat all gates.
- Report accepted, rejected, or blocked status; exact timings or objective
  measurements; checks; commit and target tip; conflicts; touched files;
  detached HEAD; worktree status; and useful next candidates.

## Runtime boundary

Obtain coordinator clearance before using a shared live runtime. Follow the
repository's runtime recovery and substitution policy. Report a blocker when
safe recovery cannot restore comparable measurement.
