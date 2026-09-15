<div align="center">

<h1>🎭 Theater</h1>
<h3>Run the whole show from one terminal.</h3>
<p>
Start coding agents, follow their work, and jump into any session without losing
the bigger picture.
</p>
<p>
<a href="https://github.com/mana-byte/theater/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/mana-byte/theater/actions/workflows/ci.yml/badge.svg"></a>
<a href="https://github.com/mana-byte/theater/releases"><img alt="Release" src="https://img.shields.io/github/v/release/mana-byte/theater?include_prereleases&amp;sort=semver&amp;label=release"></a>
<a href="https://www.python.org/"><img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-4b8bbe"></a>
<a href="https://github.com/tmux/tmux"><img alt="tmux" src="https://img.shields.io/badge/interface-tmux-1f6f5f"></a>
</p>

<img src="docs/assets/theater-hero.svg" alt="Theater régie coordinating four coding-agent CLIs around a staged terminal" width="100%">

</div>

Theater brings Claude Code, Codex, opencode, Pi, and Vibe into one local tmux
workspace. See every agent in a live tree, open its real terminal, follow its
tools and results, and keep an eye on usage while agents delegate work to one
another.

Theater does not replace your coding agents. They keep their own CLI, account,
permissions, and sessions. Theater gives them one stage—and gives you the
control room.

## 🎬 See it in action

https://github.com/user-attachments/assets/c6a7d3f4-5d31-4ad6-93f3-8fdd391c5c5b

<table>
<tr>
<td width="50%" valign="top">
<a href="docs/assets/regie-overview.png"><img src="docs/assets/regie-overview.png" alt="Theater régie showing a cross-harness participant tree, a staged Codex pane, and usage by model" width="100%"></a>
<h3 align="center">One stage for every agent</h3>
<p>See who is working, waiting, idle, or done. Move from the full cast to any agent's real terminal in one keypress.</p>
</td>
<td width="50%" valign="top">
<a href="docs/assets/trajectory-view.png"><img src="docs/assets/trajectory-view.png" alt="Theater trajectory view showing a live agent turn, tool calls, timing, costs, and event details" width="100%"></a>
<h3 align="center">See the work, not just the answer</h3>
<p>Follow model turns, tool calls, files, timing, cost, and results as they happen.</p>
</td>
</tr>
</table>

## ✨ What Theater gives you

| | |
| --- | --- |
| **One live view** | Follow agent lineage, status, current work, and usage from the régie. |
| **Your actual CLIs** | Step into the original Claude Code, Codex, opencode, Pi, or Vibe terminal at any time. |
| **Cross-agent delegation** | Let one agent ask another harness or model for focused work and wait for its result. |
| **Parallel worktrees** | Give a task its own Git worktree, or deliberately share one between cooperating agents. |
| **Sessions that keep going** | Leave the régie, come back later, and resume previous sessions from the command palette. |
| **Local control** | Theater's state stays on your machine; there is no Theater-hosted control plane. |

## Install

### Requirements

- Python 3.12+
- `tmux`
- `git`
- At least one supported coding-agent CLI, installed and authenticated

Theater installs its own Python packages. It does not install agent CLIs or
provide their subscriptions and API credentials.

| Agent | Command |
| --- | --- |
| Claude Code | `claude` |
| Codex | `codex` |
| opencode | `opencode` |
| Pi | `pi` |
| Vibe | `vibe` |

### Nix

The flake includes Theater, Python 3.12, `tmux`, `git`, and all Python
dependencies:

```sh
nix profile add github:mana-byte/theater
```

### With uv

Install `tmux` and `git` with your system package manager first, then:

```sh
uv tool install git+https://github.com/mana-byte/theater
theater --version
```

## Quick start

```sh
theater
```

That is the entry point. Theater creates or reuses its tmux session, starts its
background service when needed, and opens the **régie**—the control view.

Your first five keys:

| Key | Action |
| --- | --- |
| `o` | Open the spawn menu |
| `Ctrl+P` | Open the command palette for spawn, resume, and views |
| `j` / `k` or arrows | Move through the agent tree |
| `Enter` | Show the selected agent on the right |
| `q` | Leave the régie without stopping your agents |

> [!TIP]
> Press `o` and choose an installed CLI. The new session appears in the tree;
> select it and press `L` to enter its normal terminal.

## Everyday use

### Start agents

The spawn menu (`o`) is the quickest route. From a shell, the equivalent is:

```sh
theater spawn codex "Fix the flaky parser tests" --approval edits
```

Give a task an isolated Git worktree when it should not share uncommitted
changes with your current checkout:

```sh
theater spawn claude "Upgrade the database layer" --approval edits --worktree
```

Approval choices depend on the selected CLI:

| Policy | Meaning |
| --- | --- |
| `manual` | Prefer confirmation before tools make changes |
| `edits` | Allow workspace edits while keeping broader actions guarded |
| `yolo` | Use the CLI's fully autonomous mode |

### Let agents collaborate

Agents started by Theater can discover the other sessions and delegate work.
You can ask naturally:

```text
Use Theater to ask Codex for a second review of this patch.
```

For larger work, ask the agent to use one of Theater's built-in skills:

| Skill | Use it for |
| --- | --- |
| `theater-orchestrate` | Split a task across agents and review the results |
| `theater-debate` | Get independent positions before making a decision |
| `theater-configure` | Set up Theater interactively from an agent session |

### Come back later

Closing the régie does not stop agents. Run `theater` again to return. Use
`Ctrl+P` and choose **Resume dead session** to reopen a previous CLI session.

## Régie key mappings

### Agent tree

| Key | Action |
| --- | --- |
| `j` / `k` or `↑` / `↓` | Move through the agent tree |
| `Enter` | Stage the selected agent |
| `h` / `l` | Stage its trajectory / live terminal; press again to focus |
| `H` / `L` | Open and focus its trajectory / live terminal immediately |
| `o` | Open the spawn menu |
| `Ctrl+P` | Open the command palette |
| `Esc` | Return from a trajectory to the tree |
| `<tmux prefix> h` | Return from a staged terminal to the tree |
| `x` | Kill the selected agent's pane |
| `q` | Leave the régie; agents keep running |

The tmux prefix is usually `Ctrl+B` unless you changed it.

### Trajectory view

| Key | Action |
| --- | --- |
| `j` / `k` / `h` / `l` or arrows | Scroll |
| `g` / `G` | Jump to the oldest record / follow the newest records |
| `H` / `L` | Previous / next page |
| `Enter` | Open details for the selected record |
| `Tab` / `Shift+Tab` | Move between timeline and details |
| `/` | Search |
| `f` | Toggle filters |
| `d` | Order chronologically / by duration |
| `v` | Cycle diagnostic views |
| `b` | Return to the previously viewed record |
| `r` | Clear search, filters, and ordering |
| `R` | Retry the agent's last turn |
| `y` | Copy the selected record as text |
| `Esc` | Return to the tree |

## Useful commands

```sh
theater                         # open the régie
theater harnesses               # show detected coding-agent CLIs
theater ls --tree               # print the current agent tree
theater config                  # show effective settings and their source
theater config path             # print the config file location
theater models                  # show allowed model and reasoning choices
theater restart                 # apply config changes; agents keep running
theater stop                    # stop Theater's background service
```

Run `theater --help` for the complete command list.

## Configuration

Configuration is machine-wide. It lives at `$THEATER_HOME/config.toml`, which
is normally `~/.theater/config.toml`. A config file is optional.

### Defaults

These are the defaults most people will notice:

| Setting | Default |
| --- | --- |
| Favourite agent | None; choose one when spawning |
| Theme | Textual default |
| Agent detail in the tree | Working directory |
| Sidebar width | 52 columns |
| Event panel | Hidden |
| Cost window | Today |
| Maximum delegation depth | 3 levels |
| Maximum agents in one tree | 20 |

### Example

This is a real multi-agent setup, shortened to keep the model lists readable.
The model and reasoning entries are choices Theater may pass to a CLI; they do
not replace that CLI's own default.

```toml
[theater]
favourite = "vibe"

[rails]
budget = 100

[regie]
theme = "catppuccin-mocha"

[models]
claude = ["fable", "opus", "sonnet", "haiku"]
codex = ["gpt-5.5", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-6-astra"]
pi = ["mistral/zai-glm-5-3", "foundry-anthropic/claude-sonnet-5", "foundry-openai/gpt-6-astra"]
opencode = ["anthropic-foundry/claude-sonnet-5", "openai-foundry/gpt-5.5", "mistral/zai-glm-5-3"]
vibe = ["glm-5-3 [high]", "opus-5 [high]", "gpt-6-astra [high]"]

[reasoning]
codex = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
claude = ["low", "medium", "high"]
pi = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]
```

Themes include `nord`, `dracula`, `tokyo-night`, `rose-pine`, and the
Catppuccin variants. The [complete example config](config.example.toml) lists
every theme and setting with its default.

To choose models or reasoning levels explicitly, ask the installed CLI what it
offers and paste the generated block into your config:

```sh
theater models --discover codex
theater models
```

After editing the file:

```sh
theater config
theater restart
```

Or ask a managed agent: **“Use `theater-configure` to set up Theater with me.”**

## Data and troubleshooting

- Theater data lives under `$THEATER_HOME`—normally `~/.theater/`.
- Human-readable logs live under `$THEATER_HOME/var/logs/`.
- `theater harnesses` shows which coding-agent CLIs Theater can find.
- `theater config` validates the config and shows whether each value came from
  your file or a default.
- Quitting the régie only detaches the interface. It does not kill agents.

## Learn more

- [Complete configuration reference](config.example.toml)
- [Architecture and implementation details](docs/architecture.md)
- [Releases](https://github.com/mana-byte/theater/releases)

<div align="center">

<p><strong>Give every agent a pane. Give every task a stage.</strong></p>

</div>
