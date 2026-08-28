---
name: theater-orchestrate
description: Orchestrate implementation, investigation, or review work through Theater when the user asks to orchestrate, delegate, parallelize, or coordinate work with a specified set of harnesses, models, and reasoning efforts. Use for planning worker ownership, spawning isolated sessions, supervising execution, reviewing real artifacts, iterating on failures, and integrating accepted work.
---

# Theater Orchestrate

Use only Theater MCP for orchestration: participant discovery, spawning, messaging, waiting,
transcript inspection, and lifecycle control. Do not delegate through harness-native subagents or
another orchestration system. Remain responsible for the final result.

## Interpret the requested roster

Treat a request such as `orchestrate work with: <harness/model/reasoning profiles>` as the allowed
worker pool. Use only as many profiles as provide useful work unless the user explicitly requests
that every profile be spawned. Preserve explicit multiplicities and restrictions. Validate requested
harnesses, models, and reasoning efforts with Theater before spawning. Never silently substitute an
unavailable profile or spawn outside the pool; explain the mismatch and request a choice.

If the user says to minimize cost or tokens, optimize worker assignment without lowering acceptance
criteria:

- Give bounded, mechanical, or well-specified tasks to cheaper capable profiles.
- Reserve the strongest available profiles for architecture, ambiguous changes, integration, and
  difficult review.
- Avoid duplicate investigations and overlapping ownership.
- Use the fewest workers that still provide useful parallelism.
- Prefer precise prompts and focused checks over broad exploratory turns.
- Keep correctness, safety, review depth, and required validation unchanged.

Do not claim cost savings when current price or quota information is unavailable.

## Plan before spawning

Inspect the repository and relevant instructions. Define:

- starting branch, commit, and existing dirty work that must be preserved;
- goal and non-goals;
- phases and dependencies;
- acceptance criteria;
- files or modules owned by each worker;
- shared interfaces that every affected worker must receive verbatim;
- focused validation each worker must run;
- integration and final-validation responsibility.

Run tasks in parallel only when their file ownership and interfaces are independent. Otherwise use
ordered waves. Keep integration responsibility in the orchestrator.

Show the user a concise plan, then proceed without requesting routine confirmation. Pause only for a
material ambiguity, unavailable profile, expanded authority, destructive action, or external side
effect.

Use the Theater scratchpad when several workers need the same plan, interface, ownership map, or
decision. Keep entries concise and namespaced to the orchestration. Update stale entries when the
plan changes. Never place credentials or other secrets there, and do not treat scratchpad state as a
durable project artifact.

## Spawn workers

Every worker must:

- use `approval="yolo"`;
- run in its own Theater worktree;
- receive one concrete, bounded task;
- receive all task-local context needed to work without reconstructing the plan;
- know its owned files, forbidden scope, required interfaces, invariants, acceptance criteria, and
  focused validation commands;
- receive exact directories, filenames, symbols, and relevant line numbers, plus short code or
  interface snippets when they remove ambiguity;
- preserve unrelated user changes;
- commit its work when changes are requested;
- run only focused tests for its owned change, never the full test suite;
- never create a report unless the user's requested artifact is itself a report;
- return only the commit, changed paths, focused checks run, and blockers.

Yolo approval does not broaden the user's requested scope or authorize destructive or external
actions unrelated to the task.

When a spawn fails, inspect Theater state before retrying. Never silently choose another profile or
create a duplicate participant.

## Supervise and review

For each wait, use the longest duration safely below the current client's tool timeout. If that wait
expires, await the same handles again: timeout means the caller stopped waiting, not that a worker
stopped. Never replace long waits with rapid conversational polling or duplicate spawns. Update the
user at meaningful phase boundaries, not after every wait. Read only a narrow transcript tail when
needed to diagnose status or recover missing context; never load a whole transcript by default.

When the user changes scope or priorities, update the plan first, then redirect or stop affected
workers before continuing. Do not let superseded tasks keep changing their worktrees.

Before replacing a stalled or failed worker, inspect its participant state, transcript tail,
worktree, branch, and commits. Reuse or resume it when safe. Never create a duplicate worker while
the previous worker or its unaccounted work may still be active.

Never accept a worker's self-report as proof. Review the real commit, diff, affected interfaces,
tests, and failure paths against the plan and acceptance criteria.

When work does not pass review:

1. Send one precise correction request citing concrete defects and expected evidence.
2. Review the resulting artifacts again.
3. If still incorrect, send one final correction request.
4. After two correction rounds, stop delegating that correction and finish it directly.

The initial implementation is not a correction round. Clarifying a blocked requirement is not a
correction round unless the worker already submitted work for review.

Do not merge rejected work. After acceptance, integrate deliberately and run final validation in the
orchestrator's branch.

## Run independent final review

After integrating accepted worker changes, create a reviewable commit and run integration checks.
Spawn one independent reviewer with `approval="yolo"` in its own worktree at that exact commit. The
reviewer must use reasoning effort `high` or above and should use a different model from the
orchestrator when the requested pool and Theater configuration allow it. If the pool contains no
eligible reviewer, ask before spawning outside it. If no different eligible model exists, use a fresh
independent session rather than weakening review.

The reviewer remains read-only despite yolo approval. Give it the original request, plan, acceptance
criteria, diff range, repository invariants, and test evidence, but not the orchestrator's conclusions.
Require findings to identify severity, location, evidence, and a concrete failure mode. Do not ask it
to create a report file. The reviewer may run focused checks but never the full test suite.

Verify every finding independently. Debate disputed findings with evidence until both sides agree
that no substantiated correctness, safety, architecture, or acceptance-criteria blocker remains.
Do not prolong subjective style disagreements. After two exchanges without convergence, run a
decisive check when possible; otherwise ask the user to decide a material blocker and let the
orchestrator decide a nonblocking preference with recorded rationale.

Delegate a localized correction to its original worker only when that worker retains useful context,
its branch remains compatible, and its two correction rounds are not exhausted. Otherwise make the
correction directly. Do not create a new fixing worker. Ask the same reviewer to recheck the affected
surface, then run final validation in the orchestrator's branch. The reviewer does not implement
changes.

Keep workers and their worktrees available until final review and integration are complete. Never
kill a participant or remove its worktree while accepted or potentially recoverable work remains
unintegrated. Unless the user already authorized cleanup, ask before killing participants after the
work is complete.

## Finish

Report the integrated outcome, important design decisions, validation performed, and any remaining
risk. Distinguish completed work from unverified worker claims and from deferred scope.
