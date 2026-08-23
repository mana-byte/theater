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
│                          │  NDJSON over Unix socket  │
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

**Status lifecycle:** `idle` ↔ `working` ↔ `awaiting_input` → `dead`

**Transcript provenance:** Theater labels session identity as `heuristic`
(cwd/time only), `operator` (same-UID `theater bind`), `proven` (daemon-side
process proof), or `exact` (spawn/receipt proof). Spawned participants get their
Theater id and pane by construction; adopted participants start with only a pane
and must not have transcript text attributed from a mere cwd guess. For
transcript-backed adopted Claude, Vibe, OpenCode, and unproven Codex sessions,
`send` and `read_transcript` are refused until provenance is
operator/proven/exact. Screen-only status observation still runs before binding,
so the tree can show working/idle while the operator recovers attribution.
If a trusted bound transcript later disappears, is positively no longer a file, or a newer
same-harness/cwd candidate appears while the pinned transcript is inert and the
pane is visibly working, Theater enters `transcript_identity_lost`: screen status
stays live, but transcript attribution, turn completion, `send`,
`read_transcript`, `recall_read`, and `resume` refuse until `theater bind`
re-arms the participant.

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

This installs the `theater` binary. OpenTelemetry export is an optional extra —
see [Observability](#observability) below:

```sh
pip install -e '.[observability]'
```

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

`--worktree` creates an isolated git worktree for the child so parallel agents don't conflict on the index. `--worktree <name>` creates or joins a named shared linked worktree — multiple children spawned with the same name share the same directory and branch. This is an expert-mode collaboration primitive: the index and HEAD are shared, concurrent `git add`/`commit` operations can interfere, and Theater does not enforce file ownership.

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

Adoption is deliberately not transcript trust. To recover an adopted session,
list candidates and bind the one you inspected:

```sh
theater candidates <id>
theater bind <id> <candidate> --confirm-id <id>
```

`bind` is same-UID operator authority, like `kill`: a process that can run
`theater` or speak the daemon socket can invoke it. The `--confirm-id` stable id
check is there to prevent accidental name/alias binding, not to authenticate a
human.

The same workflow recovers `transcript_identity_lost`. Theater keeps the old
trusted location pinned and never auto-repoints to a heuristic candidate. Inspect
`theater candidates <id>`, then bind the candidate you verified; after the bind,
the next observation poll attaches from that trusted location again.

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
theater stats                   # all retained history
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

Underneath both is a coverage line naming the oldest row each half of the table
was computed from:

```
coverage: jobs from 2026-06-16 09:12
          bus from 2026-08-08 09:12
```

Retention is finite, so "all of history" is never true and the two halves are
retained for different spans — the turn counts and the refused counts do not
reach back equally far. Asking for a `--window` that starts before a floor is
not an error; the answer simply begins where the data does, and says so.

### `theater gc`

Sweep old rows out of the database now, rather than waiting for the hourly loop:

```sh
theater gc                      # sweep bus, jobs, touch and dead participants
theater gc --vacuum             # sweep, then rewrite the file to reclaim space
theater gc --json
```

The daemon does this on its own — the settings are under `[retention]` and it is
on by default. This command exists for the moment you want the space back now.

Deleting rows does not shrink the file. SQLite keeps the freed pages and reuses
them, so `theater gc` alone reports the same size before and after, and says so
rather than letting you conclude it did nothing. `--vacuum` rewrites the whole
file under an exclusive lock — the daemon blocks for the duration — and is the
only thing that returns space to the filesystem. On a 31.5 MB database that had
just been swept, it gave back 22.9 MB. It is never run in the background for
that reason: an hourly lock over a growing file is a worse problem than a large
file.

### `theater` / `theater regie`

Run `theater` to start or attach to Theater's tmux session, ensure the daemon is
running, and open the régie. The explicit `theater regie` form still requires an
existing tmux session and runs in the current pane:

```sh
theater
theater regie
```

The shared `theater` session remains alive while agent windows remain. Multiple
clients attached to that session share its selected window, as they do in tmux
normally. Quitting a régie opened by the bare `theater` command detaches the
current client; it does not stop the tmux session or the Theater daemon. The
explicit `theater regie` command only exits the TUI.

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
cwd_segments  = 2         # trailing cwd segments kept in the sidebar tree
sidebar_width = 60        # sidebar columns; used for both Textual and tmux
startup_reveal = true     # animate initial tree and later agent-spawned children

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

[retention]               # how long the database keeps things; on by default
enabled            = true
interval           = 3600.0   # seconds between sweeps
batch              = 5000     # rows per DELETE, so a sweep never blocks the loop
bus_days           = 7        # bus events; `send.refused` is exempt
jobs_days          = 60       # finished jobs and their touch rows — recall's reach
refused_cap        = 10000    # `send.refused` rows kept, oldest dropped first
stale_running_days = 7        # when a job orphaned by a daemon crash is closed

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

### Observability

Theater writes a rotating human-readable log and, when explicitly enabled,
exports traces, metrics, and structured logs via OpenTelemetry. Export is off
by default — Theater starts no exporter thread and makes no network call
unless you ask.

To enable export, install the optional dependency and set the config key:

```sh
pip install -e '.[observability]'
```

```toml
[observability]
otlp_enabled = true
# otlp_protocol = "grpc"               # or "http"
# otlp_endpoint = "http://localhost:4317"  # gRPC default; HTTP default is 4318
# service_name = "theater"
# export_interval_ms = 5000
# gauge_interval_s = 5.0
# log_max_bytes = 10485760              # 10 MB per file
# log_backup_count = 3
```

`otlp_endpoint` is a collector **base** endpoint, not a signal-specific URL.
For gRPC (default) the base endpoint is passed unchanged to all three
exporters; the default is `http://localhost:4317`. For HTTP the base endpoint
gets `/v1/traces`, `/v1/metrics`, and `/v1/logs` appended; the default is
`http://localhost:4318`. Do not pair gRPC with port 4318. A configured
endpoint must be an absolute `http` or `https` URL with a host and no query
or fragment.

Missing optional packages with `otlp_enabled = true` is fatal with an
actionable message: `install theater[observability] or disable
observability.otlp_enabled`.

`theater restart` applies any change — config is read once at daemon start.

#### Log files

The daemon writes two files under `$THEATER_HOME` that are never the same file:

| File | Contents | Rotation |
|---|---|---|
| `daemon.log` | routine Python logs | always active, 10 MB active file + 3 backups |
| `daemon.<token>.stderr.log` | raw interpreter/native crash output | not rotated; 3 generations kept by mtime |

Routine log growth — the observed 25 MB over 9 hours — goes to the bounded
rotating `daemon.log`. Raw stderr files are pruned to 3 total (including the
current generation) on each daemon start. A direct `theater daemon` keeps
stderr on the terminal and creates no generation file.

#### Restart and troubleshooting

- **Config changes require `theater restart`.** Config is read once at daemon
  start and never written by Theater.
- **`theater stop` followed by `theater daemon` or any client command** starts
  a fresh daemon. The first client command autostarts one if none is running.
- **If the daemon fails to start**, check `$THEATER_HOME/daemon.log` for
  routine errors and the newest `daemon.*.stderr.log` for crash output. A
  timeout waiting for the socket names the exact generation file when one was
  created, or `daemon.log` plus the `daemon.*.stderr.log` pattern otherwise.
- **OTLP enabled but nothing exports**: confirm the collector is reachable at
  the configured endpoint; a misconfigured endpoint shows up as export errors
  in `daemon.log`.
- **An existing global tracer provider causes a startup refusal** — Theater
  inspects `get_tracer_provider()` and rejects anything that is not a
  `ProxyTracerProvider`, because publishing over a real provider it does not
  own would silently export only some signals.

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

For Theater-spawned Claude sessions, the launch plan also layers local
`SessionStart` and `PreCompact` hooks that call `theater transcript-receipt`
(the generic entry point) with a private token file. The token is not a
seven-day lease; it is valid while the participant is live and is deleted
when the participant dies or GC removes the orphaned credential. Live
Claude sessions shipped in v3.2.0 have `settings.json` files on disk that
invoke `theater claude-receipt` by that exact name; the old command name is
kept as a forwarding alias so those sessions keep working.

Once connected, the agent has access to these tools:

### `whoami`
Returns the agent's own participant record (id, session id, tier, status, cwd, branch). Call this first to learn your own id before addressing others. `session_id` is the harness's opaque resume identifier and may initially be null; call `whoami` again after Theater has discovered the transcript.

### `list_participants`
Lists every participant Theater knows about — id, session id, harness, status, cwd, addressable flag. Session ids are populated asynchronously, so a newly discovered participant may report `session_id: null`; list again later to refresh it.

### `spawn_session`
Starts a new agent in a child tmux window. Returns the child's id, session id, and a handle for `await_sessions`. A cold spawn normally returns `session_id: null` because the observer learns the harness session id only after attaching to its transcript; use `list_participants` to retrieve it later.

```
harness:     registered harness name (see `list_harnesses`; `theater harnesses` for what this machine has)
prompt:      task delivered on the child's command line; optional for a plain CLI
approval:    "manual" | "edits" | "yolo"
cwd:         working directory (defaults to caller's cwd)
worktree:    true for an isolated git worktree; a string for a named shared linked worktree; false/omitted for none
base_branch: branch to base the worktree on; for a named worktree, set at first creation
model:       model the child runs on; omit for the CLI's own default
resume:      trusted session id to continue; only allowed when the trusted owner row is dead
```

Live resume is refused even for exact/proven/operator session ids. A live
participant should receive work through `send`; resuming is for continuing a
session after its previous owner is dead. If the trusted owner row was already
garbage-collected, Theater has no tombstone yet: resume outside Theater, then
adopt and bind that pane, or wait for tombstone support. Harness-specific
resume validation runs after the generic identity gates: the Vibe plugin
validates the signed isolated transcript domain marker, and the other shipped
plugins validate the predecessor's transcript domain against their own
observation namespace.

### `list_models`
The models `spawn_session` will accept for each spawnable harness. Answered by the daemon, which is what enforces the list — `theater models` reads the file on disk, and the two differ until a `theater restart`.

```
harness:    harness name
models:     allowlist from [models] in the config; empty means no model may be named
supported:  whether the adapter can select a model at all
```

An empty `models` is the default and is not a dead end: spawn without `model` and the child comes up on its own CLI's default. `supported: false` cannot be fixed by config.

### `send`
Delivers a prompt to an already-running addressable agent mid-session. Returns a handle for `await_sessions`. Fails with `human_present` if a human is at the target pane, `busy` if the target is already handling a send, `transcript_untrusted` for an adopted transcript-backed target that still needs `theater candidates` / `theater bind`, or `transcript_identity_lost` for a trusted pin that must be rebound.

### `await_sessions`
Blocks until ANY of the given handles reaches a terminal state, or `max_wait` seconds elapse — it does not wait for all handles. If any handle is already terminal, returns immediately. Returns one entry per requested handle with state (`done` | `crashed` | `killed` | `running`) and an error code if applicable. Process the terminal entries; re-await the still-running ones to keep waiting for them. The agent-facing reply drops prompt and result text — use `read_transcript` for the full content.

### `read_transcript`
Reads the full unclipped transcript of a participant from disk. The agent-facing `await_sessions` reply drops prompt and result text; use this when you need the full content of what a child said or did.

```
target_id:  participant id to read
last_n:     number of events to return (0 = all); default 5
```

Returns events with `role`, `text` (full), `tool_name`, and `turn_end`.

For adopted sessions this is intentionally refused until the transcript is
operator/proven/exact. That is part of the same recovery workflow as `send`: the
operator binds first, then attribution-bearing reads are allowed. If a trusted
pin later loses identity, `read_transcript` and `recall_read` refuse with
`transcript_identity_lost` and the same candidates/bind recovery instructions.

### `register_pane`
Makes the calling agent addressable by registering its tmux pane. Only needed if `whoami` reports tier `external` while the agent is actually inside tmux. The returned participant record includes the nullable, asynchronously discovered `session_id`.

---

## Example: orchestrating two agents in parallel

```python
# From inside a Claude Code or Vibe session via MCP tools:

# 1. Spawn two specialists
security = spawn_session("claude", "audit auth.py for vulnerabilities", approval="edits")
perf     = spawn_session("vibe",   "profile the hot path in api.py",   approval="edits")

# 2. Await — returns when ANY handle becomes terminal (or max_wait expires)
pending  = [security["id"], perf["id"]]
completed = []
while pending:
    batch = await_sessions(pending, max_wait=120)
    completed += [r for r in batch if r["state"] != "running"]
    pending    = [r["handle"] for r in batch if r["state"] == "running"]

# 3. Read full transcripts for every handle that became terminal
for r in completed:
    if r["state"] == "done":
        full = read_transcript(r["handle"].split("#")[0], last_n=10)
```

---

## Project layout

```
config.example.toml       every setting with its default, safe to copy
theater/
  cli/                    entry point, parser, commands, render
  config/                 config loading, models, validation
  constants/              immutable values split by domain
  observability/          one package, one process-level lifecycle (logging, metrics, tracing)
  timing.py               compatibility facade re-exporting the observability engine
  models.py               Participant, Status, Tier, Job, error codes
  client.py               DaemonClient (NDJSON over Unix socket, autostarts the daemon)
  protocol.py             wire protocol definitions (NDJSON, not JSON-RPC)
  paths.py                $THEATER_HOME layout
  formatting.py           shared CLI/régie rendering — imports neither rich nor textual
  proc.py                 process facts from ps / proc / lsof
  daemon/                 the registry server (only writer of SQLite + tmux)
    observation/          status policy, job completion, rescue, identity, screen, turns
    persistence/          store, database, repositories (participants, jobs, bus, …)
    rpc/                  handler modules registered via @method into METHODS
    runtime/              socket dispatch, maintenance loops, lifecycle
    spawning/             launch planning, resume, service
    worktrees/            unique and named shared worktree paths and repos
    observer.py           compatibility facade re-exporting the observation package
    store.py              compatibility facade re-exporting the persistence package
    methods.py            compatibility facade re-exporting the rpc package
    server.py             lifecycle only: socket, pidfile, wiring (composition surface)
    spawner.py / worktree.py   compatibility facades for the spawning/worktrees packages
    registry.py           tier assignment, pane eviction, lineage
    jobs.py               JobManager, one asyncio.Event per handle
    gc.py                 retention sweep: bus, jobs+touch, dead participants
    rails.py              depth / cycle / budget guards
    schema.py             the one place table columns are declared
    migrations/           alembic env + versions/
  harness/                plugin loader + adapters
    contracts/            Harness, Source, HarnessObserver, launch, events
    registry/             plugin lookup, install, capabilities, claims
    transcript/           transcript-file source and observer, attachment
    builtin/plugins/      claude.py · codex.py · opencode.py · vibe.py
    base.py / source.py / observation.py   compatibility facades
  mcp/                    stdio MCP server and tool implementations
    toolsets/              delegation, participants, recall, transcripts
    session.py             Session — tool-call context shared with the daemon
    server.py              14 agent tools (composition surface)
    tools.py              compatibility facade re-exporting toolsets
  tmux/                   client, command, panes, presence, delivery, facts, options
  regie/                  Textual TUI
    controllers/           animation, navigation, polling, session, staging, usage
    render/                layout, glyphs, routing
    widgets/               chrome, leaf, tree, usage breakdown and footer
    app.py                 the Textual application (composition surface)
    tree.py                compatibility facade re-exporting render modules
    palette.py             ctrl+p command-palette entries
    bus_view.py            live event-stream widget
  pricing/                usage pricing tables
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
