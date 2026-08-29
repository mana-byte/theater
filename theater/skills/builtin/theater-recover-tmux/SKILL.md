---
name: theater-recover-tmux
description: Recover Theater participants after a tmux server restart or kill-server using retained participant metadata, native session resume, and an explicitly chosen conservative recovery mode.
---

# Theater Recover tmux

Use this skill to diagnose or recover Theater participants after a tmux server restart or
`kill-server`. This is a workflow over existing Theater tools, not tmux cleanup or native mutation.

## Find the incident

Begin with `list_participants(include_dead=True, limit=100)`. If a page is full, continue with the
last row's stable id as `after_id`; repeat with the same limit until all retained rows have been
examined. Do not use names: dead names are absent and recyclable.

Ignore rows missing either required reset field—`termination_reason` or `termination_incident`—or
`terminated_at`. Keep only rows with `termination_reason="tmux_restart"`, then group them by
`termination_incident`. Use the incident the user names; otherwise choose the group whose maximum
`terminated_at` is newest. If no complete group exists, say that Theater has no retained tmux-restart
candidates and stop.

Show a concise recovery plan before spawning. For every candidate, include its stable id, harness,
cwd, branch, session_id, resume_state, and the incident id and time. State which rows are expected
to resume, which need workspace-only review, and which will be skipped.

## Choose recovery and approval

If the user did not specify a recovery mode, ask exactly once with the host harness's native
structured ask-user tool:

1. Best effort (recommended)
2. Review plan first
3. Manual per participant

If the host has no native structured ask-user tool, ask the same question in the normal response and
stop until the user answers. Do not infer a mode from silence.

Obtain one explicit approval choice—`manual`, `edits`, or `yolo`—before any spawn. Reuse a choice
the user already gave; otherwise ask once. Theater has no approval default: never invent one.

In review-plan-first mode, make no spawn until the user approves the shown plan. In manual-per-
participant mode, confirm each candidate before acting on it. Best effort proceeds only within the
limits below.

## Recover conservatively

Best effort never deletes, resets, overwrites, kills, invokes GC, or alters worktrees or branches.
It neither recreates nor cleans up anything. It only attempts the retained participant context where
the current state permits it.

Before each attempted recovery, re-list that stable id with
`list_participants(include_dead=True, ids=[id])`. Skip it if it is now live, owned by a live
participant, missing, or its `resume_state` is no longer `resumable`; report the observed state.

For a resumable row, call `spawn_session` with:

- `resume` set to the stable predecessor participant id;
- the retained `cwd`;
- `worktree=False`;
- the same `harness` and the chosen `approval`.

Do not invent a model or reasoning effort. Pass `model` or `reasoning_effort` only when the row
actually records that native value; otherwise omit the parameter. Do not change the retained branch
or request a new worktree.

For every non-resumable row, explain its `resume_state` rather than claiming conversational recovery.
Workspace-only recovery can spawn a replacement auditor only when it is safe and the selected
recovery mode or user confirmation explicitly permits the fallback. Use the same retained harness and
cwd with `worktree=False` unless the user explicitly chooses otherwise. Approval is execution policy,
not authorization for the fallback. Describe it as workspace review, not a resumed conversation; apply
the same duplicate check first.

Verify every spawn using its returned record and, when needed, `list_participants`. Summarize each
candidate as recovered, skipped, or failed, with its stable predecessor id and the reason. Do not
perform cleanup after the summary.
