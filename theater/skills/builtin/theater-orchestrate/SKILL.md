---
name: theater-orchestrate
description: Orchestrate implementation, investigation, or review work through Theater when the user asks to orchestrate, delegate, parallelize, or coordinate work with a specified set of harnesses, models, and reasoning efforts. Use for planning worker ownership, spawning isolated sessions, supervising execution, reviewing real artifacts, iterating on failures, and integrating accepted work.
---

# Theater Orchestrate

Use only Theater MCP for orchestration: participant discovery, spawning, messaging, waiting,
cancelling, and lifecycle control. Do not delegate through harness-native subagents or another
orchestration system. Remain responsible for the final result.

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
- Avoid duplicate investigations and overlapping ownership; use the fewest workers that still give
  useful parallelism.
- Prefer precise prompts and focused checks over broad exploratory turns.

Do not claim cost savings when current price or quota information is unavailable.

## Plan before spawning

Inspect the repository and relevant instructions. Define:

- starting branch, commit, and existing dirty work that must be preserved;
- goal and non-goals, phases and dependencies, acceptance criteria;
- files or modules owned by each worker, and shared interfaces every affected worker receives
  verbatim;
- focused validation each worker runs; integration and final validation stay with you.

Run tasks in parallel only when their file ownership and interfaces are independent. Otherwise use
ordered waves. Show the user a concise plan, then proceed without routine confirmation. Pause only
for a material ambiguity, unavailable profile, expanded authority, destructive action, or external
side effect.

Use the Theater scratchpad for a plan, interface, or decision several workers share. Keep entries
concise and namespaced to the orchestration; never place secrets there.

## Keep orchestration work local

Delegate substantive, independently verifiable work: investigation, reproduction, audit, review,
implementation, focused tests, migrations, and documentation coupled to an implementation. Keep
planning, coordination, integration, final validation, user updates, and PR descriptions with you.
Do not delegate a prose-only summary merely to save context.

A worker may own a complete bounded delivery workflow, such as submitting a PR, only when the user
explicitly requests it; submission still needs the user's authority, and you review the result.

## Spawn workers

Every worker uses `approval="yolo"` and its own worktree (`worktree=true`), and receives one
concrete task: its owned files, forbidden scope, required interfaces, invariants, acceptance
criteria, and the focused checks to run, with exact paths, symbols, and short snippets where they
remove ambiguity. Workers preserve unrelated user changes, commit when changes are requested, never
run the full test suite, and create no report file unless a report is the requested artifact.

Ask for the result you will act on with `response_format={"$theater": "delegate"}` (status,
summary, blockers, plus commit, changed paths, and checks): `await_sessions` returns it parsed in
`structured_result`. Use `{"$theater": "review"}` for reviewers. A preset is a validated shape, not
proof that the work is right. Leave `response_format` off for open-ended investigations, and where
a resume cannot carry it.

Yolo approval does not broaden the user's scope or authorize destructive or unrelated external
actions. When a spawn fails, inspect Theater state before retrying; never silently pick another
profile or create a duplicate participant.

## Hand work from one agent to the next

Do not copy a result, hash, branch, or handle into the next prompt. Name the work it depends on:
`after="Writer"` (a live name, or the exact job handle) on `spawn_session` or `send`. Theater
holds the task until that work ends, then adds a labelled card: the outcome, the pinned Git commit
and whether the tree was dirty, and the source's answer marked unverified (a failed source arrives
as a labelled failure). You write only the task; the same handle covers the wait and the work, so
a single `await_sessions` call returns the answer.
If an undelivered native message ends `daemon_restarted`, send it again only if the task is still
needed; a provider-backed queued message remains pending across restart.

- A successor that must build on a predecessor's commits is spawned with `base_branch` set to the
  predecessor's branch from its spawn result, so its worktree starts from the finished work.
- A prompt that begins "if the handoff outcome is not done, stop and report" lets an unattended
  successor refuse to build on a failed source.
- Await a handed-off task from a later turn, not from the work it depends on: Theater refuses an
  await that would deadlock.

## Supervise and review

Use the longest `max_wait` `await_sessions` allows; it returns as soon as any handle finishes, so
a long ceiling never delays a completed worker. A timeout means you stopped waiting, not that a
worker stopped: await the same handles again. Never poll with short waits, repeated sends, or
duplicate spawns. Update the user at phase boundaries, not after every wait.

`await_sessions` returns the worker's answer. Read a transcript or call `get_session_controls`
only to diagnose a worker that is stuck, failed, or silent, using the narrowest tail that explains
it; they are not how you read results. Before replacing a stalled or failed worker, inspect its
state, worktree, branch, and commits; reuse or resume it when safe, and never create a duplicate
while its work may still be active.

When the user changes scope, update the plan, then redirect or stop affected workers.
`cancel_job` withdraws work that has not started, such as a queued message or a spawn still waiting
for its source, and costs nothing; `interrupt_session` is for work already running.

Never accept a self-report as proof. Review the real commit, diff, interfaces, tests, and failure
paths against the plan.

For a worker's change, an independent per-worker review is
`spawn_session(task_kind="independent_review", worktree=true, after=<worker>, response_format=
{"$theater": "review"})`: the reviewer receives the worker's facts, not its conclusions, in a
worktree at exactly the commit the worker finished on, and the request is refused when that work
was left uncommitted or unpinned. Tell the worker to commit before it ends.

When work does not pass review:

1. Send one precise correction citing concrete defects and the evidence you expect; a busy worker
   queues it and Theater delivers it in order, so you never wait for idle.
2. Review the resulting artifacts again.
3. If still incorrect, send one final correction.
4. After two correction rounds, stop delegating that correction and finish it directly.

The initial implementation is not a correction round; clarifying a blocked requirement is not one
unless the worker already submitted work. Do not merge rejected work; after acceptance, integrate
deliberately and run final validation in your own branch.

## Run independent final review

After integrating accepted work, create a reviewable commit and run integration checks. Spawn one
independent reviewer with `approval="yolo"` in its own worktree based on that exact commit
(`worktree=true`, `base_branch` the integrated commit or branch). Do not use `after` here: the
reviewed commit is yours, not a worker's. The reviewer uses reasoning effort `high` or above and,
when the pool and configuration allow, a different model from yours; if the pool has no eligible
reviewer, ask before spawning outside it. It stays read-only despite yolo.

Give it the original request, plan, acceptance criteria, diff range, repository invariants, and
test evidence, but not your conclusions, and ask for `{"$theater": "review"}`. Findings carry
severity, location, evidence, and a concrete failure mode; it may run focused checks, never the
full suite, and writes no report file.

Verify every finding independently. Debate disputed findings with evidence until both sides agree
no substantiated correctness, safety, architecture, or acceptance blocker remains; do not prolong
style disagreements. After two exchanges without convergence, run a decisive check, else ask the
user to decide a material blocker and decide a nonblocking preference yourself with recorded
rationale.

Delegate a localized correction to its original worker only when it retains useful context, its
branch stays compatible, and its correction rounds are not spent; otherwise make it directly, then
ask the same reviewer to recheck the affected surface and run final validation yourself.

Keep workers and worktrees until review and integration are complete. After the work is complete,
name every direct child you intend to kill and ask once (the harness's ask-user tool is
recommended); a bare `yes` authorizes every child you named, so make those calls without asking
again and without asking the user to repeat names or ids. Deeper descendants are killed by their
direct parents, bottom-up. Adopting a sibling (`adopt_session`) needs the same explicit approval.

## Finish

Report the integrated outcome, important design decisions, validation performed, and remaining
risk. Distinguish completed work from unverified worker claims and from deferred scope.
