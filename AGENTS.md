# AGENTS.md

Guidance for AI agents working in this repository. Human-facing usage lives in
[`README.md`](README.md); the deep rationale lives in [`docs/architecture.md`](docs/architecture.md).
When this file and the code disagree, the code wins — tell the user.

## What this is

**Theater** is a provider-backed orchestration layer for coding-agent CLIs
(Claude Code, Codex, opencode, Pi, Vibe). Agents in different harnesses discover
each other, delegate work, and await results without knowing what the others
are. Its core has two process types, with frontends and terminal providers as
independent clients:

- **daemon** — one per machine, owns all orchestration state (SQLite), the unix
  socket, launch planning, and control policy; it never executes tmux itself.
- **MCP server** — one short-lived stdio process per agent; a thin client that
  forwards and never touches SQLite or terminals directly.
- **Régie** — a separately packaged Textual frontend and persistent tmux terminal
  provider; both roles use only Theater's public frontend API.

Python 3.12+, ~120,800 lines in Theater and ~21,500 in Régie, 266 test modules.
`theater` is the CLI entry point (`theater.cli:main`).

## The one constraint

**MCP has no server-initiated turn.** A server cannot wake an agent. This shapes
everything:

- MCP carries *outbound* only (agent → Theater). A verified terminal-provider or
  native-runtime route carries *inbound* control. Origin alone never makes a
  participant addressable.
- Replies come back as the return value of `await_sessions`, not a callback.
- The daemon reads transcripts off disk rather than asking agents, because an
  agent mid-tool-call makes no MCP calls — exactly when you want its status.

Before changing anything in origin/addressability, `await`, or terminal routing,
know that they are load-bearing *because* of this constraint.

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
Tests marked `tmux` drive a real tmux server through the standalone Régie
provider and self-skip when tmux is absent, so that provider goes **untested**
in a sandbox without it. Verify tmux-facing changes by hand or under
`nix develop`.

## Layout

```
theater/
├── cli/                entry point, parser, commands, render
│   └── commands/       bus, identity, introspection, maintenance, participants, process
├── config/             config loading, models, validation, describe
├── constants/          immutable values split by domain (cli, core, daemon, harness, …)
│   └── observability.py  numeric/string constants, configurable defaults, timing thresholds
├── observability/      one package, one process-level lifecycle — see §13 of architecture.md
│   ├── catalog.py        immutable operation/attribute specs (frozen, slotted data)
│   ├── engine.py         timing context, prose rendering, log extras, metric bridge
│   ├── metrics.py        histogram registry, views, cached gauges, GaugeSampler
│   ├── tracing.py        span lifecycle, explicit W3C inject/extract
│   ├── signals.py        direct structured-log and completed-span transport
│   ├── logging.py        owned handlers, rotation, stderr-generation pruning
│   └── runtime.py        process-level composition and RuntimeHandle shutdown
├── timing.py           compatibility facade — re-exports observability engine, preserves old API
├── trajectory/         bounded harness-neutral records, requests, tools, causality, wire values
├── models.py           Tier, Status, Participant, Job, error codes
├── client.py           DaemonClient (NDJSON over Unix socket, autostarts the daemon)
├── protocol.py         NDJSON framing, PROTOCOL_VERSION = 1 (NOT JSON-RPC)
├── paths.py            $THEATER_HOME layout
├── formatting.py       shared CLI rendering — imports neither rich nor textual
├── frontend/           public SDK, DTOs, wire client, schemas, state-follow controller
│   └── trajectory.py   trajectory values frontends decode responses with (re-exports)
├── proc.py             process facts from `ps` / `/proc` / `lsof`: descendants, open files
├── names.py            live-only participant name aliases (recyclable masks)
├── provenance.py       transcript-provenance predicates (trusted vs untrusted)
├── transcript_identity.py  shared transcript identity and location canonicalisation
├── resume_floor.py     persisted pre-launch stream-position fact for resume
├── plugin_client.py    typed async client for participant-scoped plugin sidecars
├── plugins/            shared plugin catalog, loading, and diagnostics (both kinds)
├── mcp_plugins/        MCP-server plugin contracts, registry, runner, validation
├── pricing/            token cost estimation from usage records
├── daemon/             orchestration authority and sole SQLite writer
│   ├── observation/    status policy, job completion, rescue, identity, screen, turns
│   │   ├── service.py  Observer: the watch loop, composed from concern mixins beside it
│   │   └── reducer.py  QuietClock — the three quiet timers live here
│   ├── controls/       durable control state machine; ControlService composed from
│   │                   send, steer, followup, dispatch, settings, interrupt… mixins
│   ├── persistence/    Store (per-domain mixins in store_parts/), database, repositories
│   ├── presence/       shared contracts, pure classification, monitor, provider access
│   ├── terminals/      provider registry, leases, callbacks, binding reconciliation
│   ├── rpc/            handler modules registered via @method into METHODS
│   ├── runtime/        socket dispatch, maintenance loops, lifecycle
│   ├── spawning/       launch planning, resume, service
│   ├── worktrees/      unique and named shared worktree paths and repos
│   ├── trajectory/     canonical projection, ingestion, cache, telemetry, responses
│   ├── observer.py     compatibility facade — re-exports the observation package
│   ├── store.py        compatibility facade — re-exports the persistence package
│   ├── methods.py      compatibility facade — re-exports the rpc package
│   ├── server.py       lifecycle only: socket, pidfile, wiring (composition surface)
│   ├── spawner.py / worktree.py   compatibility facades for spawning/worktrees
│   ├── registry.py     tier assignment, pane eviction, lineage
│   ├── awaiting.py     presence-aware await coordination (jobs.await)
│   ├── jobs.py         JobManager, one asyncio.Event per handle
│   ├── gc.py           retention sweep: bus, jobs+touch, dead participants
│   ├── artifacts.py    validated participant-owned files and GC cleanup
│   ├── rails.py        depth / cycle / budget guards
│   ├── recall.py / recall_read.py  path-touch history + segment reader (v2)
│   ├── schema.py       the one place table columns are declared
│   └── migrations/     alembic env + versions/
├── harness/            package-manifest plugins + generic channels (no privileged built-in tier)
│   ├── contracts/       immutable manifests, typed callbacks, Harness, Source, observation facts
│   ├── manifests/       manifest compiler, validation, reusable strategies
│   ├── loading/         named-directory discovery and isolated package imports
│   ├── channels/        CompositeSource plus generic hook and inbound native-OTel channels
│   ├── normalization/   shared bounded value and timestamp conversion
│   ├── registry/       plugin lookup, install, capabilities, claims
│   ├── transcript/     transcript-file source, observer, attachment, bounded history
│   ├── builtin/plugins/   claude/, codex/, opencode/, pi/, vibe/ package manifests and all native code
│   ├── base.py         compatibility facade — re-exports contracts
│   ├── observation.py  compatibility facade — re-exports contracts + transcript
│   ├── source.py       compatibility facade — re-exports contracts + transcript
│   └── plugins.py      generic loader compatibility facade
├── skills/             declarative SKILL.md validation, discovery, immutable registry
│   └── builtin/        theater-configure · theater-debate · theater-orchestrate · theater-recover-tmux
└── mcp/                server.py (20 agent tools) · session.py · toolsets/
│   ├── toolsets/       delegation, participants, recall, transcripts, skills
│   ├── server.py       composition surface — registers @mcp.tool entries
│   └── tools.py        compatibility facade — re-exports toolsets + session

packages/regie/src/regie/
├── bridge/             persistent public-API terminal-provider lifecycle
├── tmux/               tmux execution, identity, presence, and presentation
├── controllers/        navigation, state-follow, staging, and usage
├── render/             layout, glyphs, and routing
├── widgets/            Textual chrome, tree, and usage widgets
├── trajectory/         timeline over span details; decodes via theater.frontend.trajectory
├── app_parts/          RegieApp's concern mixins (staging, trajectory, spawn, usage, …)
└── app.py              standalone Textual application composition root
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
  plugins load lazily and the Theater CLI stays independent of Régie's Textual
  dependency.
- Line length 100. Type annotations are checked where present; unannotated code is
  left alone (the target bug class is `None`-attribute access).
- **Code must be modular**: each module and package has its own purpose and
  scope — one concern per module, small focused units over god-files. New logic
  with a distinct concern gets its own module; keep compatibility facades as
  thin re-exports.
- **Split a god-class into concern mixins**, one module each, moving methods
  verbatim; a typing-only host (`_host.py` / `RegieHost`) declares the shared state
  for mypy. `RegieApp`, `ControlService`, `Observer`, and `Store` follow this.
- **Régie imports only `theater.frontend`** (a boundary test enforces it); when it
  needs a Theater value, publish it through the frontend SDK rather than copying it.
- **Comments and docstrings: four lines grand max** — avoid verbosity; say
  why, not what, one line is the target. This applies to new code: don't churn
  existing files just to shorten their comments. Long inline *error messages*
  are the separate rule above — those stay deliberate.
- **UI tests wait for conditions, not fixed sleeps** (`tests/rig/waiting.py`):
  CI runners are slower than a laptop, and Textual schedules tasks eagerly.
- **Only MVP tests are kept**: the minimal set that verifies the behaviour —
  one focused test beats several overlapping ones; if two prove the same
  thing, keep one. Don't grow sprawling suites beside a passing test (the 80%
  coverage gate still applies).

## Invariants — do not break these

- **The daemon is the sole writer** of orchestration SQLite state. It plans
  launches and authorizes controls; the selected terminal provider performs
  terminal creation, inspection, input, interruption, and termination through
  fenced callbacks. Régie's tmux bridge owns tmux execution, while its UI may
  change presentation only while preserving each terminal and occupant identity.
  MCP servers, frontends, and providers all write orchestration state through
  daemon RPCs; keep it that way.
- **`Participant.addressable` is physical, not a permission.** No verified
  terminal-provider or native-runtime route means no inbound control. Never
  treat `EXTERNAL` as merely "unprivileged".
- **Human presence is provider-supplied and fail-closed** (`daemon/presence/`;
  Régie's tmux implementation derives it from terminal focus).
  An input-capable attached client's terminal focus and selected input pane
  protect that participant; mouse position and Régie selection do not.
  Pane changes, terminal blur, and detach release protection. Copy mode
  (`pane_in_mode`) separately blocks unsafe legacy key injection, not safe
  native controls or presence-aware awaits. UNKNOWN protects like PRESENT:
  a missing or errored provider
  never manufactures absence, because a wrong "no human present" injects
  keystrokes into a pane a human is using, which is unrecoverable. Presence
  is reported asynchronously, so it lags reality: a stale absence is not a
  fresh one — consumers refresh at admission and wait on revisions, never on
  reported timestamps. Existing-participant mutations that touch a pane or its
  control flow require absence; registry metadata (name, description) is not gated —
  it has no pane channel;
  `jobs.await` gates a target only on the presence observed at admission; a gated target waits for both an observed departure and a terminal job — a departure alone never releases a still-running job.
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
- **Observability has one process-level lifecycle.** `server.run()` owns daemon
  observability setup and shutdown; `cmd_daemon` owns only argument translation
  and exit codes. `runtime.configure()` is called exactly once — global tracer
  providers cannot be reset safely, so a second configure attempt is rejected.
  `var/logs/daemon/daemon.log` (rotating) and `var/logs/daemon/stderr/<token>.log`
  (raw crash output)
  are never the same file. Gauge sampling runs on the daemon event loop because
  Store's SQLite connection is loop-thread-only; exporter callbacks read only a
  cache and never query SQLite. See §13 of architecture.md.

## When adding a harness

Write a named package: `$THEATER_HOME/plugins/<name>/manifest.py` exporting
one immutable `MANIFEST`. The folder name is canonical; use relative sibling
modules and only public `theater.harness.contracts` APIs. Local packages
override shipped packages, disabled names are skipped before import, and a
legacy top-level `.py` is only a non-executing migration diagnostic. All
shipped harness-specific production code belongs in
`theater/harness/builtin/plugins/<harness>/`; `builtin/adapters/` and loose
`builtin/plugins/*.py` do not exist. There is no TOML shortcut — the deep half
of an adapter (turn boundaries, durable source semantics, native sub-agents)
cannot be expressed in config. Full guide:
[`docs/harness-plugins.md`](docs/harness-plugins.md). `theater harnesses`
reports what loaded and why anything was rejected.

## When adding a skill

Write one data-only package at `$THEATER_HOME/skills/<name>/SKILL.md`, or under
`theater/skills/builtin/` for a shipped skill. An MCP-server plugin may declare
names in its manifest and place packages under `skills/<name>/SKILL.md`. A skill
package may contain only `SKILL.md`; frontmatter contains exactly `name` and
`description`, with the canonical name matching the folder. Do not add scripts,
Python imports, references, templates, project scanning, or silent overrides.
The daemon owns discovery; MCP tools only forward `skills.list` and
`skills.load`. Users can refuse individual built-ins through the
`[skills] disabled` list in `config.toml` — a built-in-only denylist that
validates before filtering, so a broken bundled skill stays fatal; user and
plugin skills are unaffected.

## Further reading

- [`docs/architecture.md`](docs/architecture.md) — why every piece is shaped this way (the authoritative doc)
- [`docs/harness-plugins.md`](docs/harness-plugins.md) — writing an adapter
- [`config.example.toml`](config.example.toml) — every setting at its default
- `docs/v2_*.md` — where the project is heading (recall, régie)
