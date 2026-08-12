# theater

A tmux-native orchestration layer for coding-agent CLIs.

Theater lets agents running in different harnesses — Claude Code, Vibe — discover each other, delegate work to each other, and wait for the results, all without any of them needing to know what the others are. It is three things at once: a daemon that tracks every session on the machine, an MCP server each agent plugs into, and a TUI that gathers all sessions into one view.

---

## How it works

```
┌─────────────────────────────────────────────────────┐
│  Claude Code         Vibe               Any agent   │
│  (MCP client)        (MCP client)       (MCP client) │
│       │                  │                   │       │
│       └──────────────────┴───────────────────┘       │
│                          │                           │
│                  theater mcp (stdio)                 │
│                          │  JSON-RPC                 │
│                   theater daemon                     │
│                   (registry + bus)                   │
│                          │                           │
│              tmux send-keys (inbound)                │
└─────────────────────────────────────────────────────┘
```

**Why tmux?** MCP has no server-push primitive — a server cannot make an agent take a turn. So Theater uses two channels: MCP carries everything *outbound* (an agent calling a tool), and tmux `send-keys` carries everything *inbound* (delivering a prompt to an agent's pane). Without a tmux pane, an agent can call out but cannot be called.

**Three participant tiers:**

| Tier | How it got there | Addressable? |
|------|-----------------|--------------|
| Spawned | Theater created its pane | Yes |
| Adopted | Pre-existing pane, self-registered via `theater adopt` | Yes |
| External | No pane (e.g. a remote agent) | No — emit only |

**Status lifecycle:** `starting` → `idle` ↔ `working` ↔ `awaiting_input` → `dead`

---

## Requirements

- Python 3.12+
- tmux (hard dependency — inbound delivery requires a pane)
- At least one of `claude`, `vibe` on `PATH`

---

## Installation

```sh
pip install -e .
```

This installs the `theater` binary.

---

## CLI reference

### `theater daemon`

Start the per-machine registry daemon. Usually started implicitly by the first client command, but you can run it explicitly to control log level:

```sh
theater daemon --log-level DEBUG
```

### `theater ls`

List all known participants.

```sh
theater ls              # flat list
theater ls --tree       # show parent/child lineage
theater ls --watch      # live-redraws every second
theater ls --all        # include dead participants
theater ls --json       # machine-readable output
```

Example output:

```
ID              T  HARNESS     STATUS          PANE   DIRECTORY
28aa595af857    S  vibe        idle            %3     ~/Desktop/myproject
4f8a12b6c868    S  claude      working         %4     ~/Desktop/myproject

T: S spawned  A adopted  E external   * not addressable
```

### `theater spawn`

Start a new agent in a background tmux window.

```sh
theater spawn claude "review the auth module for security issues" --approval manual
theater spawn vibe "refactor the payment flow" --approval edits --cwd /path/to/project
theater spawn claude "add tests for the new API" --approval yolo --worktree --foreground
```

`--approval` is required and has no default — it controls what tool calls the child agent can make without prompting:
- `manual` — every tool call requires human approval
- `edits` — file edits are auto-approved, everything else prompts
- `yolo` — all tool calls auto-approved (use with care)

`--worktree` creates an isolated git worktree for the child so parallel agents don't conflict on the index.

### `theater adopt`

Register the current pane as a Theater participant (for agents you started by hand rather than via `spawn`):

```sh
theater adopt
theater adopt --harness claude  # override harness detection
```

### `theater harnesses`

List the coding CLIs Theater knows how to drive, and whether each one is on PATH here. Reads a local registry, so it works before the daemon is running:

```sh
theater harnesses
theater harnesses --json
```

### `theater bus`

Watch the normalized event feed from all agents:

```sh
theater bus                     # last 50 events
theater bus -f                  # follow in real time
theater bus --kind agent.tool   # filter by event kind prefix
theater bus --json              # one JSON object per line
```

### `theater regie`

Launch the régie TUI — a full-screen view of all sessions and their activity. Must be run inside tmux:

```sh
theater regie
```

While it runs, the régie turns tmux's `mouse` option on for its own session and puts the previous value back on exit. Quitting also unstages: whatever agent was joined into the régie's window is moved back to a window of its own, still running.

### `theater kill` / `theater stop`

```sh
theater kill <participant-id>   # kill a specific agent's pane
theater stop                    # shut the daemon down
```

---

## MCP integration (agent side)

Each agent harness connects to Theater by running `theater mcp` as a stdio MCP server. The harness config for Claude Code looks like:

```json
{
  "mcpServers": {
    "theater": {
      "command": "theater",
      "args": ["mcp", "--id", "${THEATER_ID}", "--harness", "claude"]
    }
  }
}
```

Once connected, the agent has access to these tools:

### `whoami`
Returns the agent's own participant record (id, tier, status, cwd, branch). Call this first to learn your own id before addressing others.

### `list_participants`
Lists every participant Theater knows about — id, harness, status, cwd, addressable flag.

### `spawn_session`
Starts a new agent in a child tmux window. Returns the child's id and a handle for `await_sessions`.

```
harness:     "claude" | "vibe"
prompt:      task delivered on the child's command line
approval:    "manual" | "edits" | "yolo"
cwd:         working directory (defaults to caller's cwd)
worktree:    create an isolated git worktree (bool)
base_branch: branch to base the worktree on
```

### `send`
Delivers a prompt to an already-running addressable agent mid-session. Returns a handle for `await_sessions`. Fails with `human_present` if a human is at the target pane, or `busy` if the target is already handling a send.

### `await_sessions`
Blocks until the given handles complete (or `max_wait` seconds elapse). Returns state (`done` | `crashed` | `running`), the agent's final response text (clipped to 2000 chars), and an error code if applicable.

### `read_transcript`
Reads the full unclipped transcript of a participant from disk. Use this when the clipped result from `await_sessions` or `send` isn't enough.

```
target_id:  participant id to read
last_n:     number of events to return (0 = all); default 5
```

Returns events with `role`, `text` (full), `tool_name`, and `turn_end`.

### `register_pane`
Makes the calling agent addressable by registering its tmux pane. Only needed if `whoami` reports tier `external` while the agent is actually inside tmux.

---

## Example: orchestrating two agents in parallel

```python
# From inside a Claude Code or Vibe session via MCP tools:

# 1. Spawn two specialists
security = spawn_session("claude", "audit auth.py for vulnerabilities", approval="edits")
perf     = spawn_session("vibe",   "profile the hot path in api.py",   approval="edits")

# 2. Wait for both
results = await_sessions([security["id"], perf["id"]], max_wait=120)

# 3. Read full transcripts if needed
for r in results:
    if r["state"] == "done":
        full = read_transcript(r["handle"].split("#")[0], last_n=10)
```

---

## Project layout

```
theater/
  cli.py          theater binary entry point
  models.py       Participant, Status, Tier domain types
  client.py       DaemonClient — JSON-RPC over a Unix socket
  protocol.py     wire protocol definitions
  paths.py        XDG-style home directory layout
  daemon/         the registry server
  mcp/            the stdio MCP server and tool implementations
  harness/        per-harness launch and transcript-parsing logic
  regie/          Textual TUI
  tmux/           tmux client wrapper
docs/
  init_idea.md            original design sketch
  init_idea_grilled.md    spec — why each decision went the way it did
  implementation_plan.md  phased build plan
  spike_results.md        findings from early prototyping
  v2_ideas.md             future feature ideas
tests/                    pytest suite (asyncio_mode = auto)
```

---

## Design notes

The full rationale for every architectural decision is in `docs/init_idea_grilled.md`. The short version:

- **MCP cannot push.** No server can initiate an agent turn. MCP handles outbound (agent → Theater), tmux handles inbound (Theater → agent pane).
- **Observation reads transcripts, not screens.** `capture-pane` is used only to detect a human typing; status is derived from the JSONL transcript the harness writes.
- **Approval is per-spawn.** The orchestrator chooses the child's approval policy at spawn time. There is no global default — this is intentional.
