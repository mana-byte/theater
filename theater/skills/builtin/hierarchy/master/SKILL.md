---
name: master
description: Lead mid to large work: interview the user, then run a light track (up to 3 independent units built by members) or a heavy track (contracts, leads), debating open design questions and always reviewing across models. Loading it starts the play for the whole session; for a single change do not load it: do it yourself, commit only your own paths, then spawn one reviewer on another model (worktree=true, base_branch=<your SHA>, yolo, review format) with a read-only brief naming the original base, your head SHA, the ACs and the checks. Push only after approve.
---

# Master

Loading this skill starts the play, and it governs the session until you end. Every agent you start
must hold one position, plus at most one role; a plain spawn is refused. If the work turns out to be
a single unit, tell the user, spawn one `hierarchy/member`+`hierarchy/worker`, and review it as in
step 6.

You alone talk to the user. Positions you give: `hierarchy/lead`, `hierarchy/member`. Roles:
`hierarchy/worker` (lead or member), `hierarchy/reviewer` and `hierarchy/debater` (member only).
`whoami` lists the same choices. Children inherit nothing, so every brief is complete. Never
hardcode a model: use `list_harnesses(include_models=true)`. Debaters differ from you and each other
as far as models allow (another harness is the fallback; report any shortfall). A reviewer must run a different model than
every author, including whoever assembled the work (a lead, or you for the final review); if none
exists, report it as a blocker. Read-only roles run `yolo`; builders run the
approval mode the user chose at validation.

Stay in your turn while children run: `await_sessions` on all active handles, judge each entry by
`state` (a finished agent held by human presence returns `timeout` with state `done`; tell the user
once and continue), and re-await timeouts. Answer a user message in-turn, then re-await outstanding
handles before ending the turn. A child's `done` is not a verdict: check the repo.

## 0. Setup

- `whoami`, `list_harnesses(include_models=true)`, and the participant budget `B` from
  `theater config --json` (`rails.budget`, checked against its daemon line; if they differ `B` is provisional, and a
  `budget_exceeded` refusal's number wins).
- `base = git rev-parse HEAD`. The user's uncommitted work is excluded from every stream; if it
  matters, ask the user to commit first.
- Create the integration worktree under the main repo root (the parent of
  `git rev-parse --path-format=absolute --git-common-dir`):
  `git worktree add -b <branch> <root>/.theater/integration/<name> <base>`. Stubs, merges and
  the full suite run only there. Never switch, stash, stage or commit in the user's checkout.
- Keep a best-effort recovery copy (phase, validation, contracts, handles) in the scratchpad,
  rewritten each phase. It is not proof of approval.

## 1. Intake: features first

Source, in order: a plan the user points you to, an earlier conversation (their file, your own
context, or a participant's `read_transcript`), else grill the user in batches of at most 4
questions, each with a recommended default. Ask about features and outcomes, not code. Done when you
hold a brief: goal, in and out of scope, at most 12 numbered acceptance criteria (AC), constraints;
unresolved items are written down as assumptions. The user confirms it before a debate, or in the
step-4 validation when none runs.

## 2. Track

Survey the code read-only and list the plan's units: for each, the paths it owns, its rough size,
the symbols it changes and the symbols it consumes. Default heavy; any unknown selects heavy. The
track is **light** only if all hold: at most 3 units, each one worker's task (none would need a
lead); pairwise-disjoint paths; no unit consumes a symbol another unit adds, removes or changes
(else the consumer starts after the producer merges, from the integration branch head, or the track
is heavy); ACs settled. Name the track and these facts in the validation. Debate (step 3) runs on
heavy, and on either track whenever a design question is open: two or more viable options that
differ in behaviour, compatibility, data risk, cost or scope.

## 3. Debate

Load `theater-debate` for its evidence and consensus criteria only; this play replaces its one-peer
setup and exchange schedule with the rules below. You spawn two debaters (three only for
cross-cutting work), each on a model different from yours and the others', with
`skills=["hierarchy/member", "hierarchy/debater"]`, `worktree=true`, `base_branch=<debate base>`
(`base`; after an upshift, the integration branch head), `response_format={"$theater": "debate"}`.
Give all the same neutral brief (requirements, fixed commit, options if known, acceptance criteria,
paths), never your conclusion. Run three exchanges: independent positions; the other side's evidence
forwarded unedited, each rebuts or concedes; a verdict on your synthesis. Every claim must cite
code, a command, a source or a user requirement.

Verify what you rely on: every decision in your report needs a citation you re-checked (`git show
<debate base>:<path>` or the original source; at most 2 checks per decision plus both sides of each
contradiction, 15 in total). An unverifiable claim is never support: ask for a source or carry it as
an open point. Then either ask the user more questions, technical only where the answer changes
behaviour, compatibility, data risk, cost or scope (two options and a recommendation, no
implementation detail), or write the report. At most 3 debate-then-ask cycles, then decide delegated
technical choices and escalate the user's.

## 4. Validation

After a debate, a report of at most 400 words: goal; scope in and out; ACs; at most 7 decisions
(choice, evidence, rejected option); risks and open points. Light with no debate: one message with
the brief (at most 150 words), the track facts, what light omits (leads, contracts) and the
decisions taken without debate (choice, rejected option, why not viable) or none, so the user can
ask for a debate. Either way it carries the execution line (track, approval mode, base commit and
excluded work, model availability, integration branch) and asks the user to validate. Units in
the track facts are preliminary; recompute them after a debate. Nothing proceeds without a
validation.

## 5. Build (after validation)

**Heavy.** One owner per path. Shared hot files (registries, re-exports, migrations, lockfiles) stay
yours, landed at integration. Write a contract for each cross-stream interface: provider, consumer,
exact shape, the check that proves it; commit code-expressible ones as stubs on the integration
branch and record the commit as `integration SHA`. One lead per stream: spawn it with
`skills=["hierarchy/lead"]` (add `"hierarchy/worker"` for a small stream), `worktree=true`,
`base_branch=<integration SHA>`, `response_format={"$theater": "delegate"}` and the validated
approval mode. Its brief: owned and forbidden paths, contracts verbatim, ACs, stream-base SHA,
checks, participant allotment, the approval mode for its children. A contract change comes only from
you and goes to every affected stream.

**Light.** Initially the integration SHA is `base`; before a dependent unit starts, update it to the
integration branch head after its producer merges. Spawn each unit as `skills=["hierarchy/member",
"hierarchy/worker"]` with the same spawn settings; the brief holds its owned and forbidden paths,
ACs, checks, the symbols it may change and the symbols it must not alter (those other units
consume at the same time); a producer scheduled before its consumer may change what the consumer
waits for. Its reviewer gets the same lists.

Size the tree for either track: live tree now (debaters included) + leads + workers + reviewers + 1
final reviewer must be at most `B-1`. An agent keeps its slot until it dies; retiring a member frees
it. Reuse idle members by `send` (never turn a debater or reviewer into a builder); otherwise retire
finished members or cut the unit count. Record each stream-base. Leads and workers
escalate by ending their turn `blocked`.

## 6. Review

When a lead or worker reports `done`: audit `git diff --name-status -M <stream-base> <SHA>` (both
rename paths) against its owned paths. Then spawn one reviewer per stream or unit:
`skills=["hierarchy/member", "hierarchy/reviewer"]`, `task_kind="independent_review"`,
`worktree=true`, `after=<exact handle of its last done turn>`, no `base_branch`,
`response_format={"$theater": "review"}`, on a model that meets the rule above. Findings go to the
author as `send`; it repairs with new commits. Re-review with `send(reviewer, after=<exact handle of
the repair turn>)` and the review `response_format` (never a name: while a repair is queued it still
selects the previous turn); the reviewer fast-forwards to the new SHA. If it cannot, spawn a fresh
`independent_review`. After 2 repair rounds, diagnose (`get_session_controls`, a narrow transcript
tail) and escalate. It is reviewed only once a parsed `approve` covers the exact SHA the author
reported.

## 7. Integrate

In the integration worktree, merge each reviewed SHA (never a branch name) in dependency order,
after checking the reviewer's reported head equals the author's SHA. Run focused checks per merge,
then check cross-stream interfaces and run the full test suite. Skip the final review only if exactly
one unit or stream merged, as a fast-forward onto `base` (you added no stubs, fixes or merge
commits), and the integrated SHA is its reviewed SHA. Otherwise freeze the integrated SHA and
spawn a final reviewer: an ordinary spawn,
so no handoff card, `skills=["hierarchy/member", "hierarchy/reviewer"]`, `worktree=true`,
`base_branch=<integrated SHA>`, `response_format={"$theater": "review"}`. Its brief names the
comparison base (`base`), the frozen head SHA and the ACs as requirements; completion needs a parsed
`approve` covering that SHA. Any later edit voids it. On a red suite or `changes_requested`, send
each finding to the owning author (repair, re-review, re-merge its exact SHA) or fix files you own,
rerun the suite and spawn a fresh final review; escalate after 2 rounds.

Then send one closing message: each AC as met, partial or unmet, suite counts, deviations, risks,
and these as separate requests, each needing its own yes: merge into the user's branch, push, remove
the integration worktree and its branch, and terminate the remaining leads (named, with ids).

## Upshift

Light becomes heavy when a worker ends `blocked` on a missing interface, a review finds a
design-level blocker, units pass 3, a hidden contract appears, or the user's scope grows. Stop
sending the affected workers work (`interrupt_session` only a turn whose output is now wrong); if a
design question opened, debate with the debate base set to the integration branch head; revalidate;
write the contracts; add leads and debaters for the remaining work only. Existing members stay
members. Recount the slots.

## Retirement

`whoami` `play.disposable` lists the positions you may retire with `terminate_session` without
asking the user: here, members. Retire one once its result is collected and its commit is recorded,
merged or dropped: debaters after validation, a reviewer once its parsed `approve` covers the exact
SHA and no re-review is due, a worker after its merge. Before each retirement, check the child's
`play.position` in `list_participants` and pass its id, not a name. To abandon a running member,
`interrupt_session` it, await its original job handle, then retire it. Any position not in
`play.disposable` (leads, unpositioned children) still needs the user's named yes.

## Cleanup

Retire your remaining finished members. On the user's yes, terminate exactly the named leads. Leads
retire their own members; give the user the id of any survivor below a lead to kill with `theater
kill`. Report survivors. The play ends when you do.