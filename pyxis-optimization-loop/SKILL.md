---
name: pyxis-optimization-loop
description: Search iteratively for useful performance improvements, from algorithms and data flow to local code; screen candidates cheaply and commit confirmed wins. Use for an explicitly requested optimization loop, continued optimization, or time/round-bounded performance search. Not for ordinary implementation or review.
---

# Pyxis Optimization Loop

Use the search budget to improve the user's outcome. Keep a known accepted
baseline and follow the repository's runtime, measurement, and commit conventions.
For explicitly requested parallel optimization across supplied worktrees, use
[the existing multi-worktree procedure](references/multi-worktree.md).

## Set the boundary

- Preserve the requested target, checkout, quality, and runtime constraints.
  Choose a primary metric tied to the real consumer and relevant guardrails;
  faster helpers do not alone establish faster loading or higher FPS.
- Track the requested duration or round count in the existing run record, with
  time reserved for confirmation and cleanup. With neither, run one search round;
  a round may test several candidates before one win or exhaustion. Create a goal
  only when explicitly requested, not merely because a duration was given.
- Continue within the agreed boundary after each candidate and across compaction.
  Do not require another user message to continue, stop after a default single
  round during a timed run, or fill time with redundant tests. Explain a real
  blocker, exhausted leads, or user stop that ends the run early.
- Preserve unrelated work. Restart or replace a stateful runtime only through a
  recovery path authorized by the user or repository instructions; check actual
  job termination before retrying or reusing it.

## 1. Choose a useful hypothesis

Start with what the consumer needs and why the current algorithm performs this
much work. Consider eliminating work, incremental updates, reuse, a different
algorithm or representation, and the dependency chain before settling on local
tuning. Use profiles, work counts, scaling, and counterexamples to decide which
level matters; keep the user's scope and resolve changes to required semantics.

Choose by expected useful benefit and the cost of learning whether it works.
Find the cheapest test of the uncertain premise before building a large design.
One candidate means one attributable hypothesis, which can span an algorithm or
several files. If nearby parameter changes keep producing noise-sized gains,
reconsider the algorithm and critical path. Large changes need not win, and
candidate count is not the objective.

## 2. Spend testing effort where it changes the decision

Use the existing harness and recipe. Verify the loaded baseline/candidate, input,
quality, workload, and measurement conditions; keep the actual consumed files
stable through the last case, including later reloads. Retain raw results and
comparison settings so the claim can be checked.

Start with the cheapest decisive correctness or performance check. Stop a clear
failure early. Only promising candidates receive broader, risk-appropriate
correctness checks and fresh performance confirmation; screening samples alone
are not an accepted win. Keep valid reversals and check displaced costs.

Before repeating a noisy result, ask whether another block can resolve a useful
effect within the remaining budget. If not, save the candidate and uncertainty
and try another direction. Invalid runs or inconclusive results are not measured
regressions. Do not impose an unrequested whole-frame precision gate that the
harness cannot resolve; a user-required bound remains unverified until supported.

Use existing asynchronous jobs while preparing the next hypothesis on isolated
inputs. A separate low-cost runner is optional when substantial repetitive tool
operation would otherwise occupy the coordinator. Use the existing delegation
mechanism; the loop does not need its own routing protocol. Serialize measurements
that share a GPU/runtime and keep candidate preparation out of the measured state.

## 3. Accept and continue

Accept only correct, confirmed improvements within the required guardrails.
Invoking this loop authorizes a local commit for each such win unless the user
says otherwise; it does not authorize a push. Stage only the optimization, run
required commit checks, and follow the receiving branch's commit style. Include
baseline/candidate values, units, direction, relative change, conditions, and the
supported scope in the commit body. Advance the accepted baseline after the win.

Restore owned candidate edits after rejection or deferral and keep useful evidence
and patches. At integration and finish, reuse evidence that still applies to the
actual artifact and conditions; check affected interactions and uncovered risks.
A changed commit ID or the end of a run alone does not require a full test matrix.

Report accepted revisions and supported gains, rejected or deferred directions,
remaining evidence gaps, and the next useful hypothesis. A local win does not
establish that the user's entire performance problem is solved.
