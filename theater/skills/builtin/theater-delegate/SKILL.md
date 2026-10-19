---
name: theater-delegate
description: Hand a scoped task to a suitable existing Theater peer, or start a builder and independent reviewer pair when the work needs one. Stay or run in the background as the user requested.
---

# Delegate

Reuse explicit user decisions that still cover this task: scope, branch or destination, approval
mode, and any named retirement or cleanup. Ask only about missing, changed or materially
out-of-scope decisions. A turn boundary, compaction, check or retry does not revoke a yes. `yolo`
authorizes execution for a spawn; it does not authorize publishing or deletion. Supply `approval`
on every spawn; there is no global default.

## Existing peer

For a simple handoff, check that the peer's current play role and actual capability fit the work;
`addressable` proves only a physical route. Use one `send` to that peer. Its prompt must state the
goal, owned and forbidden paths or scope, expected output, acceptance criteria, checks, and the
explicit authorization already given. Set `response_format` for a result you will act on. Await
the exact handle returned by that send when the user expects completion here; judge its `state`
and parsed result, then verify the work. For requested background work, give the user the handle
and end the turn. Do not start a nested pair play or require a scratchpad for this handoff. Never
silently delegate independent review to the implementer.

## New builder and reviewer pair

Use a pair when implementation needs a new builder and independent review. Do not start it inside
a live play; use that play's roles and policy instead. Use
`load_skill(name="pair/reviewer", preview=true)` only to inspect its master card without activating
the play; omitting `preview` activates it in this chat. Attach `pair/reviewer` on spawn. Choose
different builder and reviewer models with `list_harnesses(include_models=true)`. The reviewer
needs a native ask tool for any remaining user decision. Obtain a base commit without switching,
staging or stashing the user's checkout; uncommitted work is excluded unless the user explicitly
includes it through a commit.

Choose a unique `run` of at most 8 letters or digits. Give the reviewer a complete brief: goal,
numbered acceptance criteria, owned and forbidden scope, base SHA, applicable user decisions and
authorization, checks, expected output, builder approval mode, chosen models and `pair-<run>`
worktree name. Put it directly in the prompt for a self-contained task. Use `scratchpad_write`
under a `delegate/<run>` namespace only when the brief is large or
needs durable handoff across separately started workers; then pass that namespace in the prompt.

Spawn the reviewer with `skills=["pair/reviewer"]`, `approval="yolo"` for its read-only work,
`worktree="pair-<run>"`, `base_branch=<base>`, and `response_format={"$theater": "delegate"}`.
The reviewer spawns the builder with the chosen explicit approval mode and reviews its exact
commit. Keep the reviewer's returned handle. Await it and inspect its structured report when the
user expects completion here; if the user asked for background work, give them the handle,
worktree and branch, then end the turn. Merge, push and cleanup only within the user's explicit
authorization.
