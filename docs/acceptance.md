# Manual acceptance procedure

The automated acceptance tests in `tests/test_acceptance.py` cover
everything that can be tested without real harnesses: spawn → await →
result, fan-out, lineage tree, restart reconciliation, depth cap, and
cycle detection. This document is the manual procedure for the parts
that need real Vibe and Claude Code sessions.

## Prerequisites

- tmux running, attached to a session
- `theater` installed (`uv run theater` works)
- Vibe on PATH (`vibe`)
- Claude Code on PATH (`claude`)

## Step 1: Start the daemon and the régie

```bash
uv run theater stop                    # kill any old daemon
uv run theater regie                   # launches the TUI
```

The régie should show "no participants" in the tree panel and an empty
bus panel. The daemon auto-starts on first connect.

## Step 2: Start two Vibe sessions by hand

In two separate tmux panes (not the régie):

```bash
vibe
```

```bash
vibe
```

Each session starts. After a few seconds:

- Run `theater adopt` from inside each Vibe session to register it.
- The régie tree should show both sessions as `A` (adopted) with
  harness `vibe`.
- Status should transition from `idle` to `working` to `idle` as
  the agents respond to their initial prompts.

## Step 3: Start a Claude Code session by hand

In a third tmux pane:

```bash
claude
```

Then `theater adopt` from inside that session. The régie tree should
now show three participants: two vibe, one claude.

## Step 4: Verify live status

In the régie, watch the tree. As agents work:

- Status should move `idle` → `working` when an agent starts a turn.
- Status should move `working` → `idle` when an agent finishes.
- The bus panel at the bottom should scroll with `agent.assistant`,
  `agent.tool_call`, `agent.tool_result` events.

## Step 5: Spawn a worker from one Vibe session

In one of the Vibe sessions, ask the agent to use the Theater MCP tools:

```
Use the spawn_session tool to start a Claude Code worker with the
prompt "list the files in /tmp and report their sizes". Use approval
"manual". Then use await_sessions to wait for the result.
```

The agent should:

1. Call `spawn_session(harness="claude", prompt="...", approval="manual")`.
2. Get back a handle (the participant id).
3. Call `await_sessions(handles=[...], max_wait=60)`.
4. The spawned Claude Code worker appears in a new tmux window.
5. When the worker finishes its turn, the await returns with the result.
6. The parent Vibe agent uses the result.

The régie tree should show the worker as a child of the Vibe session
that spawned it, with the correct lineage.

## Step 6: Detach and reattach

```bash
# Detach from tmux (Ctrl-B d)
# Everything keeps running — the daemon, the agents, the observer.

# Reattach
tmux attach

# The régie should still be showing live status.
# The staged pane (if any) should still be visible.
```

## Step 7: Kill and restart the daemon

```bash
# Find and kill the daemon
uv run theater stop

# The régie should show stale data briefly, then...

# Restart the daemon (or just run any theater command)
uv run theater ls

# The tree should be intact. Participants whose panes still exist
# should have their correct status. Any participants whose panes
# vanished during the downtime should be marked dead.

# Any running jobs that were orphaned should report "crashed".
```

## Step 8: Verify the bus

```bash
uv run theater bus -n 100
```

The bus should show the full story:
- `participant.created` for each adopted/spawned session
- `agent.user` / `agent.assistant` / `agent.tool_call` events
- `job.created` and `job.finished` for the spawn→await cycle
- `participant.status` transitions

## Pass/fail criteria

The test passes if:

1. All three hand-started sessions appear in the tree with correct
   harness names and live status.
2. The spawned worker appears as a child of its parent in the tree.
3. The parent agent receives the worker's result via `await_sessions`.
4. A tmux detach and reattach leaves everything intact.
5. A daemon kill and restart leaves the tree intact; orphaned jobs
   report `crashed`.

The test fails if:

- Any session does not appear in the tree.
- Status does not transition (stuck at `idle`).
- The `await_sessions` call hangs or returns no result.
- The tree is empty after a daemon restart.
- The régie crashes or becomes unresponsive.
