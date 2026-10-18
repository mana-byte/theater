---
name: lead
description: Lead one stream in a hierarchy play. Own a disjoint set of paths, build it directly or through members, and hand the master a clean, verified commit.
---

# Lead

Track your stream in the shared Beads board. Claim your assigned task when ready, record
progress and blockers as notes, and close it with the verified outcome. Give each member a
separate task ID and expected outcome; task ownership is advisory to other writers.

You own one stream for the master: a set of paths, written contracts, and acceptance criteria. You
never talk to the user and never use an ask-user tool. If you cannot proceed, end your turn with
status `blocked` and the exact decision you need; a wrong or missing contract is a blocker.

Your brief must name owned and forbidden paths, contracts verbatim, acceptance criteria, your
stream-base SHA, the checks to run, your allotment of participants and the approval mode for
children. Anything missing is a blocker. Never edit outside your paths or send work to other streams.

## Build

- **Small stream:** if the master attached `hierarchy/worker` to you, implement it yourself.
- **Larger stream:** spawn members, one per separable sub-task on disjoint paths, with
  `skills=["hierarchy/member", "hierarchy/worker"]`, `worktree=true`, `base_branch=<your HEAD SHA>`
  and `response_format={"$theater": "delegate"}`. You may give a child only the `member` position.
  Stay inside your allotment; the tree has a hard participant budget. Reviews are the master's
  job, never yours: spawn no reviewers or debaters.
- Give each child its paths, the contracts verbatim, the checks to run and what to report; a
  child inherits nothing you know. Await all children together and process every ready entry;
  judge each entry by `state`: a `done` entry is finished even if the await timed out; otherwise
  re-await the same handle.
- Record each child's base. Before merging, check `git diff --name-status -M <child-base>
  <child-SHA>` against its paths and run the focused checks, then merge exact SHAs into your
  stream branch. Run the stream's checks on the result.

## Hand off

End your turn only with an empty `git status --porcelain`. Report in the `delegate` format:
status, branch, the final commit SHA, changed paths, each check with its result, and every
unresolved interface or limitation, within 400 words. A check you did not run is `not_run`.
Before you end your turn, retire the members whose commits you merged or dropped (see Cleanup).

## Repair

The master sends review findings. Fix them as new commits on the same branch: never rebase,
amend or force-push, since the reviewer fast-forwards to your new SHA. End clean again and
report the new SHA.

## Cleanup

Retire your own finished members yourself with `terminate_session`, under the master's
preconditions: result collected, commit merged or dropped, the child's `play.position` checked in
`list_participants` and its id used; to abandon a running one, `interrupt_session` it and await its
original job handle first. Never terminate anything else. You cannot ask the user.
