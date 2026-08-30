# Theater

Theater is a tmux-native orchestration layer for coding-agent CLIs—Claude Code
(`claude`), Codex (`codex`), `opencode`, and `vibe`. Agents on one machine can
discover one another, delegate work, and await results through MCP, while the
régie gives a human one live view of every session.

Theater is three cooperating processes:

- The daemon owns the registry, `SQLite` state, and `tmux` operations.
- Each agent gets a short-lived stdio MCP server that forwards to the daemon.
- The régie is a Textual TUI for the participant tree, event bus, transcripts,
  and usage.

## What it provides

- Cross-harness discovery, delegation, messaging, and turn-aware waiting.
- Live participant status, lineage, transcript reading, recall, and an event
  bus.
- Isolated `git` worktrees or named shared worktrees for parallel tasks.
- Explicit approval policy for every spawned agent.
- Built-in and user-provided orchestration skills loaded on demand.
- A machine-local daemon and a human-friendly régie, without a hosted service.

## Requirements

- Python 3.12 or newer.
- `tmux` for spawned or adopted addressable sessions; external MCP
  participants can call out without a pane but cannot be addressed by Theater.
- `git` when using worktrees or developing Theater.
- At least one supported coding-agent CLI, installed and authenticated:
  Claude Code (`claude`), Codex (`codex`), `opencode`, or `vibe`.

## Install

Theater supports two setup paths: Nix or a source checkout using `uv`.

### Nix

The flake provides a wrapped runtime with `tmux` and `git`:

~~~sh
nix profile add github:mana-byte/theater
~~~

From a clone, use `nix develop` to enter the development environment.

### From source

This path requires `uv` and `git`:

~~~sh
git clone https://github.com/mana-byte/theater.git
cd theater
uv sync --locked
uv run theater --version
~~~

`uv sync --locked` installs the versions recorded in `uv.lock`; run commands
with `uv run` from the clone, or activate the environment created by `uv` and
use `theater` directly.

## Quick start

First check which adapters are available, then spawn an agent. Replace `claude`
with another installed harness if needed:

~~~sh
theater harnesses
theater spawn claude "Review the authentication code for security issues" --approval manual
theater ls --tree
theater
~~~

The bare `theater` command behaves according to where it starts. Outside
`tmux`, it ensures the daemon is running, creates or reuses Theater's
`theater` session and `régie` window, then attaches the terminal. Inside
`tmux`, it runs the régie in the current session. When entered through bare
`theater`, quitting the régie detaches that `tmux` client without killing the
session or daemon. Commands that contact the daemon start it on demand.

## Core workflow

Spawn work from the CLI:

~~~sh
theater spawn codex "Add focused tests for the parser" --approval edits
theater spawn vibe "Investigate the flaky integration test" --approval manual --worktree
theater ls --watch
theater bus -f
theater stats --window 24
~~~

The approval choice is required on every spawn; Theater deliberately has no
global default.

| Mode | Meaning |
| --- | --- |
| `manual` | Use the harness's normal permission prompts. |
| `edits` | Allow edits where the harness supports that policy; an adapter may fall back to `manual`. |
| `yolo` | Use the harness's unattended or approval-skipping mode. Choose it deliberately. |

Bare `--worktree` creates an isolated linked worktree with its own index and
`HEAD`. A named worktree, such as `--worktree review`, is a shared collaboration
directory: it is useful when children own separate files, but it is not file or
Git-operation isolation.

### Agent-to-agent work through MCP

The built-in launchers configure Theater's MCP server for spawned agents,
including the participant identity. An agent can then use tools such as:

`whoami` → `list_participants` → `spawn_session` → `await_sessions` →
`read_transcript`

It can also use `send`, `recall`, and `list_models`. `list_skills` discovers
optional workflow instructions; `load_skill` loads only the selected skill's
full content. `spawn_session` can attach a concise durable description and a
live-only name; `update_participant` changes either later, and
`interrupt_session` stops a direct child's current turn without killing its
session. The normal loop is to
inspect the participant list, spawn a child with an explicit approval mode,
await its turn, read the transcript, and inspect the repository before keeping
the work.

For a hand-started Claude Code session, adopt its `tmux` pane and use the
returned participant id in its MCP configuration:

~~~sh
theater adopt --harness claude
~~~

~~~json
{
  "mcpServers": {
    "theater": {
      "command": "theater",
      "args": ["mcp", "--id", "<participant-id>", "--harness", "claude"]
    }
  }
}
~~~

Adoption identifies the pane; transcript trust and recovery are separate
operator decisions. Use `theater candidates ID` to inspect candidates and
`theater bind ID CANDIDATE --confirm-id ID` when a recovered transcript must be
bound.

### Agent skills

Theater ships `theater-orchestrate` and `theater-debate`. Run `theater skills`
or `theater skills --json` against a running daemon to inspect available skills
and rejected user packages.

Add a user skill at `$THEATER_HOME/skills/<name>/SKILL.md`. The directory may
contain only `SKILL.md`; its YAML frontmatter contains exactly `name` and
`description`, and `name` must match the directory. Bundled names are reserved,
so a user package cannot silently replace a Theater skill. Skill content is
data only: Theater does not execute Python, scripts, hooks, or templates from a
skill package.

## Régie

Run `theater` to open the normal TUI, or run `theater regie` inside an
existing `tmux` session. The latter does not create a session and refuses to
run outside `tmux`.

The tree accepts `j`/`k` and the arrow keys. Lowercase `h`/`l` stage a
trajectory/tmux pane, then focus it on the second press; uppercase `H`/`L`
stage and focus it immediately. `Enter` stages or unstages a selected tmux
pane. When keyboard navigation is on a usage tile, `Enter` toggles detailed
usage. Clicking any usage tile does the same. The five tiles cover input,
output, cache, cost, and average/active day. `Ctrl+P` opens the command palette.

Detailed usage shows today, this week, and this month by harness and model,
with global totals. It is fetched lazily and reused while the usage overlay is
open.

## Daemon, safety, and compatibility

- The daemon is the sole writer of Theater's `SQLite` state and the only
  process that creates panes, changes the registry, or delivers `tmux` input.
- A participant without a pane is physically external: it can call out, but
  Theater cannot call it through `tmux`.
- Spawn approval is explicit and translated by each harness adapter. Review
  the pane and the repository before trusting a child's report.
- The daemon speaks to clients over a local Unix socket; the agent-facing
  interface is MCP over `stdio`. There is no hosted control plane.

Daemon lifecycle commands:

~~~sh
theater restart
theater stop
~~~

`theater restart` applies configuration changes while keeping running agents
alive. `theater stop` stops the daemon; it does not act as a general agent-kill
command.

## Configuration

Configuration is machine-scoped at `$THEATER_HOME/config.toml`, defaulting to
`~/.theater/config.toml`. There is no project-local configuration file.
Inspect the resolved settings with `theater config` and print the exact path
with `theater config path`. The daemon reads configuration at startup;
`theater restart` applies edits.

Start from the complete example:

~~~sh
mkdir -p ~/.theater
cp config.example.toml ~/.theater/config.toml
~~~

For a custom `THEATER_HOME`, place the file in that directory instead. A small
configuration might look like:

~~~toml
[theater]
favourite = "claude"

[regie]
theme = "nord"
bus_visible = true
~~~

The favourite is used when `theater spawn` has no harness argument. The
optional `[models]` and `[reasoning]` sections are exact per-harness
allowlists for `--model` and `--reasoning-effort`; names are passed through
in the spelling used by the underlying CLI. `theater models` shows the current
lists, and `theater models --discover HARNESS` can print a starting block when
that CLI supports discovery.
See [config.example.toml](config.example.toml) for every setting and its
defaults.

## Harness plugins

The shipped harnesses are Claude Code (`claude`), Codex (`codex`),
`opencode`, and `vibe`. An additional harness is a package at
`$THEATER_HOME/harnesses/<name>/manifest.py` that exports `MANIFEST`; the
folder name is its canonical name. The default plugin directory is
`~/.theater/harnesses/`. A local package can override a shipped harness. Run
`theater restart`, then check `theater harnesses` for load and rejection
details.

Read [docs/harness-plugins.md](docs/harness-plugins.md) before writing one.
Plugins cover launch arguments, durable transcript/database observation, turn
boundaries, and optional native signal enrichment.

## Optional observability

Human-readable diagnostics are always written to rotating
`$THEATER_HOME/logs/daemon.log` and per-pane
`$THEATER_HOME/logs/regie/pane-<id>.log` files. Outside tmux, régie uses a
`pid-<pid>.log` name instead.

OTLP traces, metrics, and structured logs are off by default. In a source
checkout, install the optional dependencies:

~~~sh
uv sync --locked --extra observability
~~~

Then enable the exporter in the machine configuration:

~~~toml
[observability]
otlp_enabled = true
~~~

This exports daemon/RPC health signals plus accepted agent trajectory metrics,
structured record logs, and request/tool spans. Agent log content is excluded
by default; enable `agent_log_content` only when transcript payloads may leave
the machine. Individual agent signal families can be disabled with
`agent_metrics`, `agent_logs`, or `agent_spans`.

This is Theater's outbound observability. It is separate from the optional
inbound native-OTel harness channel, which no shipped harness currently enables.

## Development

~~~sh
uv sync --locked
uv run pytest
uv run ruff check
uv run mypy
~~~

See [AGENTS.md](AGENTS.md) for repository conventions and the development
workflow.

## Further reading

- [Architecture](docs/architecture.md): the design constraints and rationale.
- [Harness plugin guide](docs/harness-plugins.md): build and test an adapter.
- [Complete configuration](config.example.toml): every supported setting.
- [GitHub repository](https://github.com/mana-byte/theater): project source and
  history.
