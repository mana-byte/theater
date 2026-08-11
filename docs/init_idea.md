# Cross-Harness Agent Orchestration — consolidated spec

*Pre-grilling snapshot. Nothing here is defended yet.*

---

## 1. One-liner

A tmux-native orchestration layer for coding agents. A daemon maintains an MCP-addressable registry of live CLI sessions across different harnesses; agents call `send` / `spawn` / `notify` on each other as peers; a control pane renders lineage and inter-agent traffic while tmux renders the agents themselves.

**Pitch:** subagents you don't own, across harnesses you didn't write.

## 2. The gap it fills

Every harness has subagents. All of them are intra-harness:

| | Today | This |
|---|---|---|
| Who owns the child | the parent process | external registry |
| Protocol | private to the harness | MCP, public |
| Topology lifetime | dies with parent | outlives any process |
| Cross-vendor | no | yes |
| Human visibility into agent-to-agent traffic | none | the bus pane |

The last row is arguably the strongest near-term value even if orchestration never gets sophisticated: **you currently cannot see what your agents say to each other.**

## 3. Architecture

```
                    ┌─────────────────────────┐
                    │        daemon           │
                    │  registry · router      │
                    │  lineage · transcripts  │
                    └──┬──────────────────┬───┘
                MCP    │                  │   local socket
          ┌────────────┴───┐         ┌────┴──────────┐
          │ agent sessions │         │  control TUI  │
          │  (N harnesses) │         │ (a tmux pane) │
          └────────┬───────┘         └───────────────┘
                   │ hosted in
          ┌────────┴───────┐
          │  tmux server   │  processes · screens · keyboard · persistence
          └────────────────┘
```

Three owners, cleanly split:

- **tmux** — process liveness, rendering, keyboard, detach/survive
- **daemon** — identity, lineage, routing, transcripts
- **control TUI** — human view of structure; renders no agent output

## 4. Primitives

```
Participant  id · harness · cwd · capabilities · tier · status
Channel      directed path between two participants
Lineage      spawn tree; outlives individual processes
```

## 5. MCP tool surface (agent-facing)

| Tool | Blocking | Semantics |
|---|---|---|
| `list_participants` | — | who's alive, harness, cwd, declared capabilities |
| `send(to, prompt)` | **yes** | request → response against a warm session |
| `spawn(harness, prompt, opts)` | no | launch a new CLI as child; returns participant id |
| `notify(to, msg)` | no | fire-and-forget |
| `whoami` | — | own id, parent, depth |

Design line: `send` reuses warm context, `spawn` gets a clean room.

## 6. Participant tiers

The tmux decision produced three tiers, not two:

| Tier | How it arrives | Screen in TUI | Typeable | Killable |
|---|---|---|---|---|
| **Spawned** | daemon runs it in a tmux window | yes | yes | yes |
| **Adopted** | already in the user's tmux, self-registers | yes | yes | yes |
| **External** | runs anywhere else, self-registers over MCP | no | no | no |

External is the degraded tier — addressable on the bus, invisible on screen. Its existence is a stated requirement, not an accident.

## 7. TUI

```
┌──────────────┬────────────────────────────┐
│ control pane │  stage pane                │
│  (ours)      │  (real tmux pane)          │
│ ▾ codex#1    │                            │
│   ├ claude#2 │   the selected agent,      │
│   └ claude#3 │   fully interactive        │
│ ▾ vibe#4     │                            │
│ ── bus ───── │                            │
│ #1 → #3 send │                            │
└──────────────┴────────────────────────────┘
```

- Agents park in hidden tmux windows; selection swaps one onto the stage via `break-pane` / `join-pane`
- Zoom for full-screen, grid mode for 2–4 side by side
- We adopt the user's existing tmux session rather than creating our own — no nesting
- Control pane shows lineage, per-node status, bus traffic, kill switch

## 8. Lifecycle

Event-driven, not polled. tmux hooks (`pane-exited`, `pane-died`) push to the daemon. The daemon reconciles its registry against tmux's reality.

`capture-pane` stays available but off the hot path — reserved for future features like detecting a blocked `[y/n]` prompt.

## 9. Decisions locked

1. Daemon-first; MCP and TUI are both clients of it
2. Fidelity tier A, achieved by letting tmux render rather than by writing an emulator
3. tmux is a hard dependency
4. `send` is synchronous
5. Both daemon-spawned and externally-run CLIs are supported
6. `send` / `spawn` / `notify` are distinct primitives

## 10. Proposed but not confirmed

- Adopting the user's current tmux session instead of creating one
- Control-pane-plus-stage layout with `join-pane` swapping
- Capabilities as declared routing metadata (vs. addressing agents by id only)
- Three tiers rather than two

## 11. Open — nothing decided

- Language and stack
- Transport between TUI and daemon
- How a session self-registers and proves who it is
- Concurrency policy: depth limits, fan-out limits, budgets
- What happens when two agents edit the same file
- Whether transcripts persist across daemon restarts
- Trust model — any agent can `send` to any other, or is it gated?
- Name

## Appendix — rejected alternatives

| Option | Why rejected |
|---|---|
| TUI renders agent output via its own vte emulator (`alacritty_terminal`, `vt100`) | Viable, but tmux already does it; kept as escape hatch if tmux proves limiting |
| TUI polls `capture-pane` and blits rendered cells | Adds geometry sync and polling latency for no gain once the TUI became a tmux pane |
| tmux control mode (`tmux -CC`) as the frontend transport | `%output` returns raw escape bytes; still needs an emulator, so buys little |
| Our TUI *is* tmux (no custom TUI at all) | Cannot render a lineage tree as panes; fights tmux's layout engine |
| Structured-only dashboard, tier C, no agent screens | Honest MVP but loses interactive approval prompts, which are load-bearing |
