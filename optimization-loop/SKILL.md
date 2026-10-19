---
name: optimization-loop
description: Run evidence-driven iterative optimization rounds that analyze a target, test one attributable candidate, measure it against the accepted baseline, and commit only verified wins. Use when the user explicitly asks for an optimization loop, asks to continue optimizing, specifies a duration or round count, or requests parallel optimization across supplied Git worktrees. Do not use for ordinary one-shot implementation or review.
---

# Optimization Loop

Use the repository's instructions and measurement tools as the project-specific
contract. For explicitly requested parallel work across two or more supplied Git
worktrees, read [references/multi-worktree.md](references/multi-worktree.md).
Otherwise run the single-agent workflow below.

## Scope and tracking

- Pin the requested repository, worktree, branch or revision, target, and output
  boundary before editing. Preserve the user's literal choices.
- Establish the measurement contract: primary objective metric and direction,
  accepted baseline, correctness gates, guardrail metrics, workload sentinel,
  environment identity, harness configuration, warmup, sample count, statistic,
  comparison ordering, and noise policy. Discover these from repository
  instructions and current tooling when the user does not specify them.
- If the user specifies a duration, call `create_goal` once with that duration
  and target in the objective, track it with `get_goal`, and complete it only
  after the duration and final audit are satisfied. Set a token budget only when
  explicitly requested.
- If the user specifies a round count, run exactly that many rounds. Otherwise
  run one round.
- A round is a bounded search on one target. It ends with one verified win, no
  defensible win, or a blocker, and may include multiple rejected candidates.

## Continuous execution

- Keep the loop active in one long-running turn until the requested duration or
  round count and final audit are complete. Candidate or round completion is
  never a turn boundary, and continuing must not require another user message.
- Send concise progress updates while working without returning a final response.
  After an accepted commit, begin the next round immediately. After rejection,
  fully restore the candidate and continue searching immediately.
- If infrastructure forces continuation or context compaction, resume the same
  tracked run and accepted baseline as one logical turn. Do not create a new
  goal, reset elapsed time, or reinterpret the requested boundary.
- End early only for a genuine blocker, required new authority, an exhausted
  search with no defensible candidate, or an explicit user stop.

## Optimization rule

- Analyze the current target and choose the most valuable defensible candidate.
  Treat previous sessions as process evidence, not a limit on possible ideas.
- Change one attributable candidate at a time and compare it with the latest
  accepted baseline under the same measurement contract.
- Verify the exact revision, executable or artifact, configuration, workload,
  and successful load before accepting measurement samples.
- Require semantic correctness and repeatable improvement in the primary metric.
  Watch relevant guardrails and the workload sentinel for displaced regressions.
- Alternate source ordering or use adjacent/reverse pairs when drift matters.
  Remeasure small or noisy deltas according to the contract.
- Reject and fully restore regressions, ambiguous tradeoffs, correctness
  failures, missing metrics, invalid samples, and incomparable runs. Remember
  rejected ideas so a long loop does not repeat them.
- Use each accepted result as the next baseline.

## Image comparisons

When visual output is part of correctness acceptance, use `$pyxis-image-compare`
(Pyxis image compare) for an offline baseline/candidate viewer when available.
Record the exact revisions, capture settings and image paths in the manifest or
accompanying evidence. Use PNG or lossless WebP captures for exact comparisons;
the skill preserves them with lossless AVIF. JPEG and lossy WebP are recompressed
with lossy AVIF and cannot establish pixel equality. A viewer supports visual
review; it does not replace the measurement or correctness gates.

## Commit rule

Invoking this loop authorizes a commit for each verified win unless the user
explicitly says not to commit. After the correctness and measurement gates pass,
stage only that optimization, run the repository's commit checks, and commit it
immediately. Use a concise imperative subject that names the optimization
directly and matches the wording and capitalization of recent first-parent
commits on the receiving branch, unless repository instructions specify another
convention. The commit message must include the verified performance change for
the primary metric: baseline and candidate values with units, the relative
percentage, and enough measurement context to identify the comparison. Put this
evidence in the body when it would make the subject noisy. State the metric
direction unambiguously so a lower-is-better negative delta is not mistaken for
a regression. A rejected or blocked candidate produces no commit. When one
logical optimization owns tracked source and generated outputs across
repositories, create one atomic commit in each repository without including
unrelated state.

## Runtime boundary

Use the repository-declared measurement harness and verify its current capability
instead of relying on stale assumptions. Do not restart, replace, or silently
switch a stateful external runtime unless the user authorized it or applicable
repository instructions explicitly make it part of the workflow. Exhaust safe,
non-destructive recovery, then report a blocker when comparable measurement is
not possible.

## Finish

Run a final comparable correctness and performance audit from the accepted tip.
Confirm unrelated work is untouched. Report the final revision, accepted
commits, rejected candidates, measurement conditions, absolute and relative
metric changes, checks performed, and blockers. Do not close a timed goal before
its requested duration and final audit are complete.
