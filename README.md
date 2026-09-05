<div align="center">

<h1>🎭 Theater</h1>
<h3>Run the whole show from one terminal.</h3>
<p>
Let orchestrators direct agents across models, harnesses, worktrees, and
projects.<br>
Follow every turn from the régie, step into any session when needed, and keep
the cast, changes, model choices, and costs under control.
</p>
<p>
<a href="https://github.com/mana-byte/theater/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/mana-byte/theater/actions/workflows/ci.yml/badge.svg"></a>
<a href="https://github.com/mana-byte/theater/releases"><img alt="Release" src="https://img.shields.io/github/v/release/mana-byte/theater?include_prereleases&amp;sort=semver&amp;label=release"></a>
<a href="https://www.python.org/"><img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-4b8bbe"></a>
<a href="https://github.com/tmux/tmux"><img alt="tmux" src="https://img.shields.io/badge/interface-tmux-1f6f5f"></a>
</p>

<img src="docs/assets/theater-hero.svg" alt="Theater régie coordinating four coding-agent CLIs around a staged terminal" width="100%">

</div>

Theater is a local-first orchestration layer for coding-agent CLIs. Agents on
one machine can discover one another, spawn children on any harness, and pick
the model and reasoning effort each child needs — you can steer that choice
too, naming the exact model or depth for a given task. Agents delegate work,
exchange prompts, and await results through MCP. The **régie** gives you one
live view of their lineage, status, panes, trajectories, transcripts, and
usage.

No hosted control plane. No replacement chat UI. Your agents keep their native
CLIs, permissions, sessions, and terminal panes.

## 🎬 See it in action

https://github.com/user-attachments/assets/c6a7d3f4-5d31-4ad6-93f3-8fdd391c5c5b

<table>
<tr>
<td width="50%" valign="top">
<a href="docs/assets/regie-overview.png"><img src="docs/assets/regie-overview.png" alt="Theater régie showing a cross-harness participant tree, a staged Codex pane, and usage by model" width="100%"></a>
<h3 align="center">One stage for every agent</h3>
<p>Coordinate Claude Code, Codex, opencode, and Vibe from one terminal. Keep agent lineage, live descriptions, native panes, and usage visible.</p>
</td>
<td width="50%" valign="top">
<a href="docs/assets/trajectory-view.png"><img src="docs/assets/trajectory-view.png" alt="Theater trajectory view showing a live agent turn, tool calls, timing, costs, and event details" width="100%"></a>
<h3 align="center">Understand the work</h3>
<p>Open a live trajectory to see requests, model work, tool calls, timing, cost, and results as they happen—not just the final response.</p>
</td>
</tr>
</table>

## ✨ Why Theater

| | |
| --- | --- |
| **Cross-harness delegation** | Claude Code can hand work to Codex, Codex can ask opencode, and every child comes back through the same await/transcript loop. |
| **A real operations view** | See who is working, waiting, idle, or dead; follow lineage; inspect trajectories; and stage any addressable pane. |
| **Parallel work without pretending** | Use isolated Git worktrees or explicit named shared worktrees. Theater tells you where isolation ends. |
| **Safety you choose per spawn** | Every child gets an explicit `manual`, `edits`, or `yolo` approval policy. There is deliberately no global default. |
| **Sessions that survive the task** | Durable descriptions, transcript recovery, recall, resume, and a machine-local event history keep context usable. |
| **Local by construction** | One daemon owns SQLite and tmux. MCP servers and the régie are thin clients; no Theater service receives your code. |

## Install

### Nix

The flake provides Theater with `tmux` and `git`:

```sh
nix profile add github:mana-byte/theater
```

From a clone, `nix develop` opens the complete development environment.

### From source

This path requires [uv](https://docs.astral.sh/uv/):

```sh
uv tool install git+https://github.com/mana-byte/theater
theater --version
```

## Quick start

Install and authenticate at least one supported CLI, then:

```sh
theater harnesses
theater plugins
theater
```

The bare `theater` command is the normal entry point:

- Outside tmux, it creates or reuses Theater's tmux session and attaches you.
- Inside tmux, it opens the régie in the current session.
- Commands that need the daemon start it on demand.
- Quitting the régie detaches the client; it does not kill the agents or daemon.

> **Tip:** you don't have to edit `config.toml` by hand — there's a built-in
> `theater-configure` skill in the Theater MCP. Just tell your agent to use the
> theater configure skill in the Theater MCP and it will set the app up with you.

Supported harness packages ship for:

| Claude Code | Codex | opencode | Vibe |
| :---: | :---: | :---: | :---: |
| `claude` | `codex` | `opencode` | `vibe` |

## How it works

<img src="docs/assets/theater-flow.svg" alt="Agents call the Theater daemon through MCP; Theater calls agents through tmux; the régie reads the same daemon state" width="100%">

Theater is three cooperating processes:

1. The **daemon** owns the registry, SQLite state, jobs, and all tmux mutation.
2. Each agent gets a short-lived **MCP server** that forwards requests to the
   daemon.
3. The **régie** is a Textual TUI backed by that same daemon state.

This split follows one hard constraint: MCP has no server-initiated turn. An
agent calls Theater through MCP; Theater reaches an agent through its tmux pane.
No pane means the participant can call out, but cannot be called.

## Useful commands & keybinds

```sh
theater                                          # open the régie and start playing
theater ls --tree                                # every agent, its lineage, and its state
theater spawn codex "Fix the flaky test" --approval edits
theater restart                                  # apply config changes without killing agents
theater bus -f                                   # follow the live event stream
```

In the régie tree, the keys you will use every day:

| Key | Action |
| --- | --- |
| `j` / `k` | Move through the participant tree (arrow keys work too) |
| `Enter` | Stage the selected agent |
| `h` / `l` | Stage the trajectory / live pane, focus on the repeat press |
| `H` / `L` | Open the trajectory / live pane immediately |
| `o` | Spawn a fresh session — harness picker |
| `Ctrl+P` | Command palette — spawn, resume, and views |
| `Esc` | Return from the trajectory view to the tree |
| `<prefix> h` | Return to the tree from the staged pane or trajectory |
| `x` | Kill the selected agent's pane |
| `q` | Quit the régie — it detaches and kills nothing |

Inside a trajectory view:

| Key | Action |
| --- | --- |
| `j` / `k` / `h` / `l` | Scroll the view (arrows work too) |
| `g` / `G` | Jump to the oldest record / resume following the tail |
| `H` / `L` | Previous / next ledger page |
| `Enter` | Open the details of the selected record |
| `Tab` / `Shift+Tab` | Move focus between the timeline and detail regions |
| `/` | Search the trajectory |
| `f` | Toggle the filter panel |
| `d` | Toggle the ledger order — chronological or by duration |
| `v` | Cycle the diagnostic views |
| `b` | Go back to the previously viewed record |
| `r` | Reset the view — clear search, filters, and ordering |
| `R` | Retry the agent's last turn |
| `y` | Copy the selected record as text |
| `Esc` | Return to the tree |

## Configuration

Configuration is machine-scoped at `$THEATER_HOME/config.toml` (normally
`~/.theater/config.toml`). There is no project-local configuration file.

The intended way to set it up is to let an agent do it: any participant with the
Theater MCP server can load the `theater-configure` skill through its skill-listing
tool and follow it. The skill interviews you in plain language, discovers what it
can from the machine on its own, writes exactly what you chose, validates the
file with `theater config` and `theater models`, and switches itself off when
done.

`theater config path` prints where the file lives, and
[config.example.toml](config.example.toml) documents every supported setting.

<details>
<summary><strong>Adopt a hand-started agent</strong></summary>

Adopt the current tmux pane, then use the returned id in that agent's MCP
configuration:

```sh
theater adopt --harness claude
```

```json
{
  "mcpServers": {
    "theater": {
      "command": "theater",
      "args": ["mcp", "--id", "<participant-id>", "--harness", "claude"]
    }
  }
}
```

Transcript trust is separate from pane adoption. Use `theater candidates ID`
and `theater bind ID CANDIDATE --confirm-id ID` for operator recovery.

</details>

## Extend Theater

### Harness plugins

Add a package at `$THEATER_HOME/plugins/<name>/manifest.py` exporting one
immutable `MANIFEST`. Local packages can override shipped packages; invalid
packages are rejected with diagnostics instead of partially loading.

Read the [harness plugin guide](docs/harness-plugins.md) before writing an
adapter. A real adapter defines launch behavior, durable observation, turn
boundaries, resume semantics, and optional native signal enrichment.

### MCP-server plugins

MCP-server packages are participant-scoped stdio sidecars installed in the same
`$THEATER_HOME/plugins/` catalog as harness packages. Each package declares exactly one kind.
MCP-server packages are disabled until explicitly enabled and can use only their declared
capability grants. Use `theater plugins` to inspect both kinds locally without starting the daemon.

Read the [MCP-server plugin guide](docs/mcp-server-plugins.md) for native
`TheaterPluginClient` sidecars and compatibility wrappers using `theater plugin call`.

### Agent skills

Theater ships `theater-orchestrate`, `theater-debate`, `theater-configure`,
and `theater-recover-tmux`. User skills live at
`$THEATER_HOME/skills/<name>/SKILL.md` and are data-only: Theater never executes
scripts or Python from a skill package. Enabled MCP-server plugins may also
declare package-owned skills, which appear through the same `list_skills` and
`load_skill` tools with their plugin owner identified. Any bundled skill can be
switched off individually with the `[skills] disabled` list in
`config.toml`.

## Observability

Human-readable daemon and régie logs are always available under
`$THEATER_HOME/var/logs/`. Optional OTLP traces, metrics, and structured logs are
off by default:

```sh
uv sync --locked --extra observability
```

```toml
[observability]
otlp_enabled = true
```

Agent log content is excluded unless explicitly enabled. See the complete
settings in [config.example.toml](config.example.toml).

## Learn more

- [Architecture](docs/architecture.md) — why Theater is shaped this way.
- [Harness plugin guide](docs/harness-plugins.md) — build a new adapter.
- [Configuration reference](config.example.toml) — every supported setting.

<div align="center">

<p><strong>Give every agent a pane. Give every task a stage.</strong></p>

</div>
