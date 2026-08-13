# theater

A tmux-native orchestration layer for coding-agent CLIs.

Theater lets agents running in different harnesses — Claude Code, Codex, Vibe — discover each other, delegate work to each other, and wait for the results, all without any of them needing to know what the others are. It is three things at once: a daemon that tracks every session on the machine, an MCP server each agent plugs into, and a TUI that gathers all sessions into one view.

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
- At least one of `claude`, `codex`, `opencode`, `vibe` on `PATH` — or any other CLI
  you teach it about, see [Configuration](#configuration)

---

## Installation

```sh
pip install -e .
```

This installs the `theater` binary.

### Nix

The flake builds from the same `uv.lock` the repo already uses, so a Nix install
and a `uv run` install resolve to identical dependencies.

```sh
nix run . -- ls              # run it once from a clone, install nothing
nix profile install .        # or put it on PATH for good
```

There is no remote yet; once there is one, the same commands take a
`github:owner/theater` flake reference in place of the `.`.

The packaged binary is wrapped with `tmux` and `git` appended to `PATH` — as a
*fallback*, not an override. A tmux client refuses to talk to a server running a
different protocol version, so if you already have tmux (and you do, since
Theater lives in your session) yours is still the one that gets used. The
bundled copy only matters on a machine with none.

For development:

```sh
nix develop     # venv with the dev group, plus uv, tmux and git
```

This shell has the real tmux the `tmux`-marked tests need; without it they skip
themselves and delivery goes untested.

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

An adapter maps these onto whatever its CLI actually offers, and may have to
round down: `opencode` has one flag, `--auto`, so `edits` there behaves as
`manual` rather than silently granting more than was asked for.

`--worktree` creates an isolated git worktree for the child so parallel agents don't conflict on the index.

`--model` picks the model the child runs on, overriding whatever that CLI would
have chosen for itself:

```sh
theater spawn claude "audit the crypto helpers" --approval manual --model opus-4.1
theater spawn opencode "port the CSV reader" --approval edits --model anthropic/claude-sonnet-4
```

A model has to be listed for that harness under `[models]` in the config file
first — see `theater models` below. Nothing is listed by default, so `--model`
refuses everything until you write the section; omit the flag and the CLI's own
default applies, which is what every spawn did before this option existed.

Within the allowlist the name is passed through untouched. Theater checks
membership and nothing else, because model namespaces change faster than this
project releases: a listed name the CLI does not recognise still fails in the
child's pane, where the vendor that owns the namespace can say so properly.

Every built-in harness supports it, by whichever lever its CLI offers: `claude`,
`codex`, and `opencode` take a flag, `vibe` takes `VIBE_ACTIVE_MODEL` in the
child's environment. A third-party harness plugin written before this option
existed will refuse `--model` by name rather than start the wrong model.

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

### `theater models`

Show which models a `theater spawn --model` may name, per harness. Reads the
config file, so like `theater config` it works before the daemon is running —
and the file is the honest thing to report, since the file is what enforces it:

```sh
theater models
theater models --json
theater models --discover opencode
```

Bare, it lists every registered harness, including the ones with nothing
listed, because those are exactly the harnesses `--model` currently refuses:

```
~/.theater/config.toml

claude    opus-4.1, sonnet-4
codex     -  (--model refused)
opencode  -  (--model refused)
vibe      -  (--model refused)
```

`--discover <harness>` asks that CLI what it can run and prints a `[models]`
block to paste — the on-ramp, since the list starts empty and an empty list
refuses every `--model`. Paste it, then delete down to the models you actually
want spawns to be able to spend:

```
# 31 found — paste into ~/.theater/config.toml,
# keeping only the models you want spawns to be able to name
[models]
opencode = [
  "anthropic/claude-sonnet-4",
  ...
]
```

Not every CLI can be asked, and the two ways of finding nothing are reported
differently, because they call for different next steps:

- **models found** — the block above, on stdout, exit 0
- **asked, none reported** — usually a provider that is not logged in yet; log
  in and ask again. Exit 1
- **cannot be asked** — the CLI has no command or config file to read, so no
  amount of retrying will help. Exit 1, pointing at the manual route. `claude`
  and `codex` are both in this bucket today

Discovery is an authoring aid and never a gate. Nothing here is consulted when
a spawn happens: the allowlist is whatever the config file says, whether you
wrote it by hand or pasted it from here.

### `theater bus`

Watch the normalized event feed from all agents:

```sh
theater bus                     # last 50 events
theater bus -f                  # follow in real time
theater bus --kind agent.tool   # filter by event kind prefix
theater bus --json              # one JSON object per line
```

### `theater stats`

How turns have been ending, per harness:

```sh
theater stats                   # all of history
theater stats --window 24       # only turns started in the last 24 hours
theater stats --json
```

```
HARNESS       TURNS  CLEAN  RESCUED  FAILED  RUNNING  RESCUE RATE
claude           24     22        0       2        0           0%
opencode         46     43        3       0        0           7%
```

The column to watch is RESCUED. When the observer never sees a turn end, the
daemon waits out the rescue timer and hands the caller the last thing the agent
was heard to say — which reads as a real, slightly odd answer. A harness whose
transcript format has drifted therefore looks slow rather than broken, and this
is the only place that shows it. A high rate for one harness is a parser
problem; a high rate everywhere is a problem with how turn ends are matched to
jobs.

Sends refused before delivery — a human at the pane, a target already busy, a
target with no pane — are counted underneath. They leave no job behind, so they
are recorded as `send.refused` on the bus.

### `theater regie`

Launch the régie TUI — a full-screen view of all sessions and their activity. Must be run inside tmux:

```sh
theater regie
```

Children hang off their parent on lineage rails, so a sibling is never mistaken for a nephew. `ctrl+p` opens the command palette, which carries a `Spawn <harness>` entry for every harness Theater can drive — that starts a plain CLI in the current session, with no prompt and no parent.

While it runs, the régie turns tmux's `mouse` option on for its own session and puts the previous value back on exit. Quitting also unstages: whatever agent was joined into the régie's window is moved back to a window of its own, still running.

### `theater kill` / `theater stop` / `theater restart`

```sh
theater kill <participant-id>   # kill a specific agent's pane
theater stop                    # shut the daemon down
theater restart                 # stop, then start again — how config is applied
```

### `theater config`

Show the resolved settings and where each value came from:

```sh
theater config                  # every key, tagged default / config.toml / plugin
theater config path             # the file to edit, whether or not it exists
theater config --json
```

This prints what the daemon holds, not what the file says. The failure it exists
to diagnose is "I set the key and nothing happened".

---

## Configuration

`$THEATER_HOME/config.toml` (default `~/.theater/config.toml`). Optional —
Theater runs with no config file at all. Read at start-up and never written by
Theater, so your comments and ordering survive. An unknown key is a loud error
naming the file, the key and the closest legal spelling; a typo that is silently
ignored is the defect this file is meant to remove. Run `theater restart` to
apply a change.

Machine-scoped only: there is no project-local config, because there is one
daemon per machine holding one registry.

[`config.example.toml`](config.example.toml) in the repository root documents
every setting with its default and is safe to copy as-is:

```sh
cp config.example.toml ~/.theater/config.toml
```

Every setting in it is written out at the value Theater uses anyway, so copying
it changes no behaviour — edit what you want to differ and delete the rest.
Deleting a key is not the same as leaving it: a key you keep is yours, and will
not follow a default that changes in a later version. The short version:

```toml
[theater]
favourite = "vibe"        # harness used when `theater spawn` omits one

[regie]
theme = "nord"            # any Textual theme name
tree_interval = 1.0
bus_interval  = 0.4
bus_batch     = 50

[rails]
depth_cap = 3             # how deep a spawn chain may go
budget    = 20            # how many descendants one root may have

[observer]
poll_interval          = 0.25
search_interval        = 2.0
screen_interval        = 1.0
sync_interval          = 1.0
relocate_timeout       = 5.0
awaiting_input_timeout = 10.0
rescue_timeout         = 60.0

[harness]
disabled = []             # plugin names to leave out of the registry

[models]                  # what `theater spawn --model` may name, per harness
claude = ["opus-4.1", "sonnet-4"]
```

`[models]` is the one section with no default to write out, so
`config.example.toml` ships it commented: an absent or empty list means no
model may be named for that harness and children run on whatever their own CLI
config picked, which is what every spawn did before the option existed. Listing
a model is therefore a deliberate grant, not a default being restated. Write it
by hand or start from `theater models --discover <harness>`.

Deliberately not configurable: the default approval mode. There is none anywhere
in Theater, because the choice is the whole safety story for a child nobody is
watching, and a key setting it to `yolo` once and forever defeats that.

### Teaching Theater a new CLI

One way: write a plugin. A Python file that implements the adapter, dropped in
`$THEATER_HOME/harnesses/`.

The four adapters Theater ships — `claude`, `codex`, `opencode`, `vibe` — are the
same kind of file, living in `theater/harness/builtin/plugins/`. There is no privileged
built-in tier and no second, weaker mechanism to declare one in TOML: a config
schema could only ever express the shallow half of an adapter, and the deep half
is where turn boundaries, bus messages, `read_transcript` and native sub-agents
come from. One mechanism means the shipped adapters exercise the extension point
every time Theater runs, instead of being the reason it is untested.

`theater harnesses` lists what loaded, where each came from, and why any were
rejected. Writing one: **[docs/harness-plugins.md](docs/harness-plugins.md)**.

`[harness] disabled` is the only switch over the registry — a denylist, so a
harness added in a later release appears without anyone editing config.

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
harness:     "claude" | "codex" | "opencode" | "vibe"
prompt:      task delivered on the child's command line; optional for a plain CLI
approval:    "manual" | "edits" | "yolo"
cwd:         working directory (defaults to caller's cwd)
worktree:    create an isolated git worktree (bool)
base_branch: branch to base the worktree on
model:       model the child runs on; omit for the CLI's own default
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
config.example.toml   every setting with its default, safe to copy
theater/
  cli.py          theater binary entry point
  models.py       Participant, Status, Tier domain types
  client.py       DaemonClient — JSON-RPC over a Unix socket
  protocol.py     wire protocol definitions
  paths.py        XDG-style home directory layout
  daemon/         the registry server
  mcp/            the stdio MCP server and tool implementations
  harness/        the plugin loader, and the adapters that ship in builtin/plugins/
  regie/          Textual TUI
  tmux/           tmux client wrapper
docs/
  init_idea.md            original design sketch
  init_idea_grilled.md    spec — why each decision went the way it did
  implementation_plan.md  phased build plan
  architecture.md         how the pieces fit together
  harness-plugins.md      writing a harness adapter in Python
  v1.4_configuration.md   the configuration release, decision by decision
  spike_results.md        findings from early prototyping
  v2_ideas.md             future feature ideas
tests/                    pytest suite (asyncio_mode = auto)
```

---

## Design notes

The full rationale for every architectural decision is in `docs/init_idea_grilled.md`. The short version:

- **MCP cannot push.** No server can initiate an agent turn. MCP handles outbound (agent → Theater), tmux handles inbound (Theater → agent pane).
- **Observation reads what the agent wrote, not the screen.** Status is derived from the transcript the harness leaves behind — a JSONL file for three of the four shipped adapters, a SQLite event log for `opencode`, which brings its own reader; `capture-pane` only detects a human typing. Two narrow exceptions: a plugin that declares `has_transcript = False` has nothing to read, so its turns end when its prompt returns on screen; and after a minute of silence over an idle screen, a job still waiting is finished anyway, so a turn boundary the parser never saw cannot strand the agent that sent the prompt.
- **Approval is per-spawn.** The orchestrator chooses the child's approval policy at spawn time. There is no global default — this is intentional.
