# AGENTS.md

Guidance for AI agents working in this repository. Human-facing usage lives in
[`README.md`](README.md); the deep rationale lives in [`docs/architecture.md`](docs/architecture.md).
When this file and the code disagree, the code wins — tell the user.

## What this is

**Theater** is a tmux-native orchestration layer for coding-agent CLIs (Claude
Code, Codex, opencode, Vibe). Agents in different harnesses discover each other,
delegate work, and await results without knowing what the others are. It is three
processes:

- **daemon** — one per machine, owns all state (SQLite) and the unix socket, the
  only thing that writes to SQLite or shells out to tmux.
- **MCP server** — one short-lived stdio process per agent; a thin client that
  forwards, never touches SQLite or tmux directly.
- **régie** — a Textual TUI; also just a client, holds no state the daemon lacks.

Python 3.12+, ~31,500 lines, ~80 test files. `theater` is the CLI entry point
(`theater.cli:main`).

## The one constraint

**MCP has no server-initiated turn.** A server cannot wake an agent. This shapes
everything:

- MCP carries *outbound* only (agent → Theater). tmux `send-keys` carries
  *inbound* (Theater → an agent's pane). No pane ⇒ can call out, can never be
  called (the `EXTERNAL` tier).
- Replies come back as the return value of `await_sessions`, not a callback.
- The daemon reads transcripts off disk rather than asking agents, because an
  agent mid-tool-call makes no MCP calls — exactly when you want its status.

Before changing anything in the tier system, `await`, or the tmux layer, know
that they are load-bearing *because* of this constraint.

## Dev commands

Run from the repo root. This project uses `uv`.

```sh
uv run pytest                 # full suite (asyncio_mode = auto)
uv run pytest tests/test_x.py # one file
uv run pytest --cov           # coverage gate (fail_under = 80); NOT in addopts
uv run ruff check             # lint
uv run ruff format            # format (line-length = 100)
uv run mypy                   # type check (theater/ only, lenient — no --strict)
```

Schema changes (see architecture §5):

```sh
uv run alembic revision --autogenerate -m "add a column"
uv run alembic check          # fails if schema.py and versions/ disagree; CI runs this
```

Nix: `nix develop` gives a dev shell with the dev group plus real tmux and git.
Tests marked `tmux` drive a real tmux server and self-skip when tmux is absent —
so tmux delivery goes **untested** in a sandbox without it. Verify tmux-facing
changes by hand or under `nix develop`.

## Layout

```
theater/
├── cli/                entry point, parser, commands, render
│   └── commands/       bus, identity, introspection, maintenance, participants, process
├── config/             config loading, models, validation, describe
├── constants/          immutable values split by domain (cli, core, daemon, harness, …)
├── models.py           Tier, Status, Participant, Job, error codes
├── client.py           DaemonClient (NDJSON over Unix socket, autostarts the daemon)
├── protocol.py         NDJSON framing, PROTOCOL_VERSION = 1 (NOT JSON-RPC)
├── paths.py            $THEATER_HOME layout
├── formatting.py       shared CLI/régie rendering — imports neither rich nor textual
├── proc.py             process facts from `ps` / `/proc` / `lsof`: descendants, open files
├── daemon/             the registry server (only writer of SQLite + tmux)
│   ├── observation/    status policy, job completion, rescue, identity, screen, turns
│   │   ├── service.py  the watch loop and orchestration (largest module)
│   │   └── reducer.py  QuietClock — the three quiet timers live here
│   ├── persistence/    store, database, repositories (participants, jobs, bus, …)
│   ├── rpc/            handler modules registered via @method into METHODS
│   ├── runtime/        socket dispatch, maintenance loops, lifecycle
│   ├── spawning/       launch planning, resume, service
│   ├── worktrees/      unique and named shared worktree paths and repos
│   ├── observer.py     compatibility facade — re-exports the observation package
│   ├── store.py        compatibility facade — re-exports the persistence package
│   ├── methods.py      compatibility facade — re-exports the rpc package
│   ├── server.py       lifecycle only: socket, pidfile, wiring (composition surface)
│   ├── spawner.py / worktree.py   compatibility facades for spawning/worktrees
│   ├── registry.py     tier assignment, pane eviction, lineage
│   ├── jobs.py         JobManager, one asyncio.Event per handle
│   ├── gc.py           retention sweep: bus, jobs+touch, dead participants
│   ├── rails.py        depth / cycle / budget guards
│   ├── recall.py / recall_read.py  path-touch history + segment reader (v2)
│   ├── schema.py       the one place table columns are declared
│   └── migrations/     alembic env + versions/
├── harness/            plugin loader + adapters (no privileged built-in tier)
│   ├── contracts/       Harness, Source, HarnessObserver, launch, events
│   ├── registry/       plugin lookup, install, capabilities, claims
│   ├── transcript/     transcript-file source, observer, attachment
│   ├── builtin/plugins/  claude.py · codex.py · opencode.py · vibe.py
│   ├── base.py         compatibility facade — re-exports contracts
│   ├── observation.py  compatibility facade — re-exports contracts + transcript
│   ├── source.py       compatibility facade — re-exports contracts + transcript
│   └── plugins.py      the plugin loader
├── mcp/                server.py (14 agent tools) · session.py · toolsets/
│   ├── toolsets/       delegation, participants, recall, transcripts
│   ├── server.py       composition surface — registers @mcp.tool entries
│   └── tools.py        compatibility facade — re-exports toolsets + session
├── tmux/               client.py · command.py · panes.py · presence.py · delivery · facts · options
└── regie/              Textual TUI
    ├── controllers/    animation, navigation, polling, session, staging, usage
    ├── render/         layout, glyphs, routing
    ├── widgets/        chrome, leaf, tree, usage breakdown, usage footer
    ├── app.py          the Textual application (composition surface)
    ├── tree.py         compatibility facade — re-exports render modules
    ├── palette.py      ctrl+p command-palette entries
    └── bus_view.py     live event-stream widget
```

## Conventions

- **Style is French-flavoured on purpose**: "régie", "théâtre", em-dashes in
  prose. `ruff` rules RUF001–003 (homoglyph checks) are disabled for this reason —
  don't "fix" the accents.
- **Error class names are the CamelCase of their wire code**: `Busy` ⇒ `busy`,
  `HumanPresent` ⇒ `human_present` (`models.py`). Renaming one desyncs the code
  agents branch on. `N818` (Error-suffix rule) is disabled for this.
- **Long, inline error messages are deliberate** — every error tells the caller
  what to do about it (`TRY003` disabled). Keep that when adding errors.
- **Imports inside functions are intentional** (`PLC0415` disabled): harness
  plugins load lazily and the CLI keeps Textual off the import path of non-régie
  subcommands.
- Line length 100. Type annotations are checked where present; unannotated code is
  left alone (the target bug class is `None`-attribute access).

## Invariants — do not break these

- **The daemon is the sole writer** of SQLite. Only the daemon may create,
  destroy, respawn, or inject input into participant panes; other processes may
  query tmux, and the régie may change presentation — session-local options,
  focus, size, or window placement — only while preserving each participant's
  pane ID and occupant. Every write to registry state goes through the daemon —
  MCP servers and the régie forward RPCs for that; keep it that way.
- **`Participant.addressable` is physical, not a permission.** No pane, no
  `send-keys`. Never treat `EXTERNAL` as merely "unprivileged".
- **Human-presence uses copy mode (`pane_in_mode`) only** (`tmux/presence.py`).
  It accepts false negatives but *never* false positives — a wrong "no human
  present" injects keystrokes into a pane a human is using, which is unrecoverable.
  Do not add screen-scraping heuristics here (one was removed for this).
- **`AWAITING_INPUT` is a display hint** — never gate a control decision on it.
- **The three quiet timers stay separate** (`RELOCATE`, `AWAITING_INPUT`,
  `RESCUE` in `observation/reducer.py`). Sharing them was a v1 bug; the comments call it a
  scar, not a preference.
- **Approval has no default** anywhere — it is chosen per-spawn (`manual` /
  `edits` / `yolo`). This is the whole safety story for an unwatched child; do not
  add a global default.
- **Job states are `running`/`done`/`crashed`/`killed`.** `timeout` is not a state
  — it's what `await` returns when the caller stops waiting.
- **Observer job 1 (get the text) is the replaceable seam; job 2 (decide what it
  means) is not.** Per-harness behaviour belongs in a `Source` / `HarnessObserver`,
  never in the reducer's policy.
- **Schema edits go through Alembic.** A bare `CREATE TABLE IF NOT EXISTS` change
  is a silent no-op against existing databases (the v1.2 hazard). Regenerate a
  revision and run `alembic check`.
- **Rows are deletable, handles are not.** The send-sequence counter lives in the
  `meta` table because it used to be re-seeded from `MAX(jobs)`; once the GC can
  delete old jobs, that regresses the counter and re-mints handles pruned jobs
  already used. Anything else derived from `MAX(some table)` at start-up is the
  same bug waiting to happen — persist it.
- **The GC sweeps jobs on `finished_at`, never `created_at`,** so a running job
  (`finished_at IS NULL`) can never be deleted out from under a caller that is
  awaiting it. **And every sweep is batched** — the store is synchronous on the
  event loop, so one unbatched `DELETE` of 30k rows freezes every status poll
  and every await while it runs.
- **`VACUUM` is never background.** Only `theater gc --vacuum`, because it locks
  the whole file. Plain deletion does not shrink the file, and every path that
  reports a sweep has to say so — a user who deletes 94% of the database and
  sees the same file size reports GC as broken.

## When adding a harness

Write a plugin: a Python file implementing the adapter, dropped in
`$THEATER_HOME/harnesses/` (or `theater/harness/builtin/plugins/` to ship it).
There is no TOML shortcut — the deep half of an adapter (turn boundaries, bus
messages, `read_transcript`, native sub-agents) can't be expressed in config.
Full guide: [`docs/harness-plugins.md`](docs/harness-plugins.md). `theater
harnesses` reports what loaded and why anything was rejected.

## Further reading

- [`docs/architecture.md`](docs/architecture.md) — why every piece is shaped this way (the authoritative doc)
- [`docs/init_idea_grilled.md`](docs/init_idea_grilled.md) — the original design interrogation
- [`docs/harness-plugins.md`](docs/harness-plugins.md) — writing an adapter
- [`config.example.toml`](config.example.toml) — every setting at its default
- `docs/v2_*.md` — where the project is heading (recall, régie)
