---
name: theater-delegate
description: Delegate one implementation task to a builder and reviewer pair when the user asks to delegate, hand off, or run a task in the background while the chat continues. Writes a complete brief to the scratchpad, starts the pair play, tells the user where to look and ends the turn.
---

# Delegate

The user hands you a task to run in the background so this chat stays free for more tasks. You write
the brief, start the `pair` play, tell the user where to look, and end your turn. You never await,
poll, read diffs, edit, verify, push, or answer the pair's questions: the pair asks the user
directly.

## Before you start

- You must not be inside a live play; a play cannot start inside another, and the spawn is
  refused. If you are, tell the user. A pair needs two spawn levels below you; on `depth_exceeded`
  tell the user.
- Never `load_skill` `pair/reviewer` yourself: loading a master skill starts the play for this
  chat. Attach it on a spawn.
- Get from the repo or this conversation whatever you can, then delegate. Ask the user only about
  features or design, never raw code.

## Write the brief

Pick a unique `run` id of at most 8 letters or digits. Write each key with `scratchpad_write(value,
namespace="delegate/<run>", key=...)`. The pair infers nothing, so every key is complete:

- `goal`: what to build and why, in the user's words and yours.
- `acceptance`: numbered, checkable criteria.
- `scope`: owned paths, forbidden paths, what is out of scope.
- `base`: `git rev-parse HEAD`; uncommitted work is not included.
- `context`: conventions to follow (AGENTS.md), `path:line` pointers, decisions already made and
  why, failed attempts.
- `checks`: exact commands (lint, types, focused tests, full suite) and what green means.
- `done` and `settings`: the definition of done; approval mode (default `yolo`); the builder's
  harness and model, different from the reviewer's (`list_harnesses(include_models=true)`); round cap 8.

Entries expire (about 7 days) and are not a record; the pair's transcript is.

## Spawn the reviewer

`spawn_session(harness, model, approval="yolo" unless the user chose otherwise,
skills=["pair/reviewer"], worktree="pair-<run>", base_branch=<base>, name="Reviewer-<run>",
description=<the task>, response_format={"$theater": "delegate"}, prompt=<pointer>)`. The prompt
names the namespace `delegate/<run>` and the worktree name `pair-<run>`, states the goal in one
line, and says to read every value with
`scratchpad_get(namespace="delegate/<run>", keys_only=false)`. Choose the reviewer's harness and
model with `list_harnesses(include_models=true)`. The reviewer alone asks the user, so its harness
needs a native ask tool (Claude and opencode have one); the builder needs none. If none is available, tell the user. A pair holds 2 slots; on
`budget_exceeded` tell the user and do not retry.

## Tell the user, then stop

Give the reviewer's name and handle, the branch `theater/named/pair-<run>` with its worktree, the
brief's namespace, and that the reviewer will ask the user directly about features or design,
including the builder's questions. End your turn. When the user asks later, one
`await_sessions(max_wait=0)` on the handle returns the report and `read_transcript` shows every
exchange. Ask before terminating the reviewer; merge and push only on the user's word.
