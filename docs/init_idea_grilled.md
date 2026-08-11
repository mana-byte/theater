# Theater — design spec (post-grilling)

*Supersedes `init_idea.md`. Every decision below was interrogated; the reasoning and the rejected branches are recorded so they don't get relitigated.*

---

## 1. What it is

**Theater** — a tmux-native orchestration layer for coding agents.

A daemon maintains an addressable registry of live CLI sessions across harnesses. Agents discover each other, delegate work as asynchronous jobs, and await results. A control pane (`régie`) renders lineage and inter-agent traffic while tmux renders the agents themselves.

> Subagents you don't own, across harnesses you didn't write.

The name carries both senses: the stage, and the *theater of operations*.

## 2. The gap

Every harness has subagents. All of them are intra-harness.

| | Today | Theater |
|---|---|---|
| Who owns the child | the parent process | external registry |
| Protocol | private to the harness | MCP, public |
| Topology lifetime | dies with parent | outlives any process |
| Cross-vendor | no | yes |
| Human visibility into agent-to-agent traffic | none | the bus |

The last row is the strongest near-term value even if orchestration never gets sophisticated: **you currently cannot see what your agents say to each other.**

## 3. Architecture

```
                    ┌─────────────────────────┐
                    │        daemon           │
                    │  registry · router      │
                    │  jobs · bus · lineage   │
                    └──┬──────────────────┬───┘
                MCP    │                  │   unix socket
          ┌────────────┴───┐         ┌────┴──────────┐
          │ agent sessions │         │  régie (TUI)  │
          │  (N harnesses) │         │ (a tmux pane) │
          └────────┬───────┘         └───────────────┘
                   │ hosted in
          ┌────────┴───────┐
          │  tmux server   │  processes · screens · keyboard · persistence
          └────────────────┘
```

Three owners, cleanly split:

- **tmux** — process liveness, rendering, keyboard, detach/survive
- **daemon** — identity, lineage, routing, jobs, transcripts
- **régie** — human view of structure; renders no agent output

## 4. The load-bearing constraint

**MCP cannot push.** Servers respond to clients; they cannot make an agent take a turn. `sampling/createMessage` would be the escape hatch and Claude Code does not implement it. Elicitation notifications only close a flow the client already opened.

Consequences that shape the entire design:

- MCP is how an agent **calls out**, never how it is **called**.
- Inbound delivery must use a side channel: `tmux send-keys` into the target's pane.
- Anything the daemon cannot reach through tmux cannot receive messages.

## 5. Primitives and vocabulary

```
Participant   id · harness · cwd · branch · tier · status · last_activity
Job           an outstanding unit of delegated work; identified by a handle
Lineage       spawn tree; outlives individual processes
Bus           the log of inter-agent traffic
```

**Lineage has two kinds of edge** (amendment, from spike 0.2). They are not interchangeable and the régie must not draw them the same way:

| Edge | Created by | Has a pane | Addressable | Theater controls it |
|---|---|---|---|---|
| **Theater edge** | our `spawn` | yes | yes | yes — kill, budget, await |
| **Harness edge** | the harness's own sub-agent mechanism (Claude `isSidechain`, Vibe `child_sessions[]`) | no | no | no — we can only observe it |

A harness-internal child is a fact we read out of a transcript. It shows in the tree so the user can see where tokens went, but it has no `tmux_pane`, cannot receive a `send`, and cannot be killed independently of its parent. Counting it against the depth cap is correct; treating it as a routing target is not.

Vocabulary stays literal (`participant`, `send`, `spawn`). The metaphor is used only where already load-bearing: **stage** for the pane showing the selected agent, **régie** for the control pane.

## 6. Participant tiers

| Tier | How it arrives | Screen in régie | Addressable | Typeable | Killable |
|---|---|---|---|---|---|
| **Spawned** | daemon runs it in a tmux window | yes | yes | yes | yes |
| **Adopted** | already in the user's tmux, self-registers | yes | yes | yes | yes |
| **External** | runs anywhere else, self-registers over MCP | no | **no** | no | no |

External is **emit-only**: it appears in the tree, its outbound calls show on the bus, and it can never be sent to. This is a direct consequence of §4 and is not a temporary limitation.

### How a participant learns its own id (amendment, phase 1a)

The obvious mechanism does not work. An MCP stdio server does **not** inherit its parent's environment: when a server config omits `env`, the SDK substitutes `get_default_environment()`, an allowlist of six variables — `HOME LOGNAME PATH SHELL TERM USER` on posix (`mcp/client/stdio/__init__.py:28-44,127`). Setting `THEATER_ID` on the pane would be silently dropped before the server ever sees it. The same is true of `TMUX_PANE`, which is why adoption cannot simply read it either.

Identity therefore travels on **argv**, which nothing filters:

```
theater mcp --id <participant-id>
```

Each harness needs a different lever to get that argv in place:

| Harness | Lever | Notes |
|---|---|---|
| `claude` | `--mcp-config FILE` | we write one JSON file per participant under `$THEATER_HOME/mcp/` |
| `vibe` | `$VIBE_MCP_SERVERS` on the *harness* process | any `VIBE_*` var overrides the matching config field; `mcp_servers` is union-merged by `name` (`vibe_schema.py:321`), so the user's other servers survive |

Consequence for phase 1b: the primary adoption path ("MCP server reads inherited `$TMUX_PANE`") is dead on arrival for any harness that uses the reference SDK. The fallback — the agent runs `echo $TMUX_PANE` with its own shell tool and calls `register_pane` — is now the primary path.

## 7. Tool surface (agent-facing, MCP)

| Tool | Returns | Semantics |
|---|---|---|
| `list_participants()` | list | static facts + one-line last activity |
| `read_transcript(id, last_n)` | text | opt-in deep read; caller pays for its own curiosity |
| `send(to, prompt)` | handle | deliver to a live session; async |
| `spawn(harness, prompt, approval=, worktree=)` | handle + id | launch a new CLI as a child |
| `await(handles[], max_wait)` | `done` \| `still_running` \| `error` | block this caller only |
| `notify(to, msg)` | — | fire-and-forget |
| `register(session_id)` | — | adoption; binds a harness session to a pane |
| `whoami()` | self | id, parent, depth, budget remaining |

Design line: `send` reuses warm context, `spawn` gets a clean room. A **worker is a job, not a participant type** — both `send` and `spawn` return handles and `await` has one meaning.

Discovery is **unrestricted**: any participant may list, read, and address any other. Rich metadata is deliberate — agents are expected to decide for themselves whether to call an existing peer or spawn a new one.

## 8. Delivery and observation

Two separate channels, in opposite directions.

```
IN   →  tmux send-keys into the target pane
OUT  ←  tail the harness's JSONL transcript
```

**Inbound** is `send-keys` into the live pane. Not `-p --resume`: that forks a session rather than attaching, so the agent that answers would not be the agent on screen, and the tree would stop corresponding to the things doing the work. Resume-fork is retained as the implementation of `spawn(warm: true)`, where a detached clone is exactly the intent.

**Outbound** is transcript tailing. Both harnesses write structured JSONL. This collapses three separate heuristics — *did it finish*, *what did it say*, *is it idle* — into one structured feed. Cost: a per-harness parser coupled to an undocumented format. Parsers are version-pinned and fail loud when the shape changes.

`capture-pane` survives for exactly one job: detecting a non-empty input buffer (§10).

**Correlating a pane to its transcript:** `register(session_id)` is the primitive. For Spawned panes the daemon infers by newest-transcript-after-spawn as a convenience; for Adopted it must ask.

## 9. Job lifecycle

`send` and `spawn` return immediately with a handle. The caller may fan out, then `await`.

```
send(b, "...")  → h1
send(c, "...")  → h2
await([h1,h2], max_wait=120)
```

`await` blocks **that agent's MCP request only** — the daemon and every other participant continue. This is the key insight that made async work without an inbound-reply channel: the reply is the return value of a tool call the agent already made.

**The await ceiling.** MCP clients time out individual requests (Claude Code: `MCP_TOOL_TIMEOUT`, typically 60s). `await` is therefore bounded and returns `still_running`, expecting the agent to re-await in a loop. Progress heartbeats are an optimization, never the mechanism — depending on undocumented timeout behavior across two vendors is the more fragile bet.

**Failure is structured.** When a worker dies the awaiting caller receives a reason code — `crashed`, `killed`, `budget_exceeded`, `timeout` — plus whatever partial transcript exists. The code matters: retry is correct for a crash and catastrophic for a budget stop. Agents recover well from explicit failure and terribly from silence.

## 10. Safety rails

| Rail | Rule | Why |
|---|---|---|
| Depth cap | default 3 | wide fan-out is often correct; deep recursion rarely is |
| Tree budget | hard stop on the subtree | the only backstop when heuristics fail |
| Cycle detection | reject an `await` that closes a loop | async killed deadlock; `await` revived it |
| Busy target | queue | a slow caller beats a corrupted session |
| Human present | **error back to caller** | never inject into a session a human is using |
| Approval mode | `spawn(approval=)` explicit, no default | auto-approving agents the human didn't launch is how this generates a horror story |

**Human-presence detection** combines `pane_active` + `session_attached > 0` (an attached human is looking at this pane), `pane_in_mode` (copy mode — definitely present), and a `capture-pane` scrape of the input line for a non-empty buffer. Tuned to accept false negatives and never false positives: when unsure, queue rather than error.

## 11. Workspace model

Each spawned child gets a **real `git worktree`** — isolated index and HEAD. Vibe ships `--worktree NAME` already.

Rejected: one shared worktree with children confined to distinct subfolders. The git index and HEAD are worktree-global, so concurrent staging or commits corrupt each other; directory confinement is advisory, nothing stops an agent editing `../`; and real changes routinely cross directories. It reads as isolation and isn't. The subfolder rule survives as a *soft scope hint in the child's prompt*, not as the isolation mechanism.

**Merge-back:** the child commits to its own branch and reports the branch name in its result. The parent decides. Auto-merge would mean the daemon silently making integration decisions on unreviewed code; handing back a branch name keeps the merge an explicit act that shows up on the bus.

## 12. The régie

```
┌──────────────┬────────────────────────────┐
│ régie        │  stage                     │
│  (ours)      │  (real tmux pane)          │
│ ▾ codex#1    │                            │
│   ├ claude#2 │   the selected agent,      │
│   └ claude#3 │   fully interactive        │
│ ▾ vibe#4     │                            │
│ ── bus ───── │                            │
│ #1 → #3 send │                            │
└──────────────┴────────────────────────────┘
```

- We **adopt the user's existing tmux session** rather than creating one — no nesting, ever. If `$TMUX` is unset, create and attach.
- Agents park in hidden tmux windows; selection swaps one onto the stage via `break-pane` / `join-pane`. Zoom for full screen, grid mode for 2–4.
- The régie is **read-mostly**: navigate, select, zoom, kill, and one write action — spawn. All prompting happens by focusing the stage and typing at the real agent, because that is already a better interface than anything we would build.

## 13. Daemon

- **Singleton per machine**, fixed socket path, auto-started on first connect. Per-project would immediately break the cross-repo case that unrestricted trust enables; auto-start matters because an Adopted agent from a pane we didn't launch must not fail on a missing daemon.
- **Transport:** unix domain socket, newline-delimited JSON. Local-only by construction, which is most of the security model for free.
- **Persistence:** SQLite for bus history and job state. Registry and lineage are reconstructible from tmux; the bus is not, and losing it defeats the feature identified as the strongest near-term value.
- **Lifecycle events:** tmux hooks (`pane-exited`, `pane-died`) push to the daemon. Event-driven, not polled.

## 14. Stack

Python 3.12 + Textual + `uv`, matching Vibe exactly.

The workload is I/O-bound subprocess orchestration, which is asyncio's actual strength, and Vibe's MCP client and Textual code are a working reference to read. Rust or Go would give a single-file daemon and better concurrency ergonomics while sharing nothing.

## 15. Scope

**No compromise on scope.** v1 is the full design above, both harnesses, all rails.

The single accepted constraint is **ordering**: the two assumptions everything rests on — `$TMUX_PANE` correlation and transcript tailing — are front-loaded, and the Claude Code transcript adapter lands early rather than last. Writing the second adapter is what proves the observation layer is an interface rather than a Vibe-shaped function. A plan reduces schedule risk; only the second implementation reduces integration risk.

Detailed sequencing lives in `implementation_plan.md`.

### Acceptance test

> *I can see every agent on this machine in one tree that I can interact with, and they can interact with one another in an organized and non-destructive way.*

Concretely, one evening's demo: two Vibe sessions started by hand plus one Claude Code session all appear in the tree with live status; one Vibe agent spawns a worker, awaits it, and uses the result; the whole thing survives a tmux detach and a daemon restart.

---

## 16. Verified facts

Established by inspection of `../mistral-vibe` and the MCP spec, not assumed:

| Fact | Source |
|---|---|
| Claude Code does not support `sampling/createMessage` | MCP docs; anthropics/claude-code#1785 |
| MCP has no server-initiated "take a turn" primitive | 2025-11-25 spec |
| Vibe **does** implement sampling | `vibe/core/tools/mcp_sampling.py:22` |
| Vibe passes `env=None` to the MCP SDK — no filtering, full inheritance | `vibe/core/tools/mcp/registry.py:300`, `tools.py:318` |
| Vibe has headless mode: `-p`, `--resume <id>`, `--output json` | `vibe/cli/entrypoint.py:50-107,170-177` |
| Vibe transcripts: `~/.vibe/logs/session/{prefix}_{ts}_{id}/messages.jsonl` | `vibe/core/paths/_vibe_home.py:8`, `session_logger.py:113` |
| Vibe ships `--worktree NAME` | `vibe/cli/entrypoint.py:135-141` |
| Vibe permission bypass: `--yolo` / `--auto-approve` / `bypass_tool_permissions` | `vibe/cli/entrypoint.py:117-122` |
| Vibe is Python 3.12 + Textual 8.2.8 + uv | `pyproject.toml:2-3,102` |

## 17. Unverified assumptions — test these first

1. **`$TMUX_PANE` reaches the MCP server process in practice**, for both harnesses. Inferred from Vibe's code; never observed. Everything in §8 fails without it.
2. **tmux format variables** `cursor_x`, `pane_active`, `pane_in_mode`, `session_attached` behave as expected. tmux was denylisted in the environment where this spec was written.
3. **Claude Code's transcript format** — location, schema, and whether end-of-turn is unambiguously detectable.
4. **Actual MCP client timeout values** in both harnesses, and whether either honors `notifications/progress` for timeout extension.
5. **`send-keys` into a repainting TUI** is reliable — bracketed paste, multi-line prompts, and mid-render injection are all untested.
6. **`--resume` against a session that is currently live** in another process. Assumed unsafe; unconfirmed.

## 18. Residual risks

- **Two lifecycles.** tmux's view of what's alive and the daemon's registry will disagree. Hooks narrow the window; they don't close it.
- **Transcript formats are undocumented** and will change without notice in both harnesses.
- **Unrestricted trust** (§7) is the right call for a single-user machine and becomes wrong the moment anything multi-tenant appears.
- **`send-keys` is a hack.** It works because nothing better exists. If either vendor ships a real session API, that path should be replaced rather than kept alongside.
- **Cross-harness remains unproven** until the second adapter is written, regardless of how well v1 is planned.

---

## Appendix — rejected alternatives

| Option | Why rejected |
|---|---|
| TUI renders agent output via its own vte emulator | Viable; tmux already does it. Kept as escape hatch |
| TUI polls `capture-pane` and blits rendered cells | Geometry sync + polling latency for no gain once the TUI became a tmux pane |
| tmux control mode (`tmux -CC`) as frontend transport | `%output` returns raw escape bytes; still needs an emulator |
| Our TUI *is* tmux, no custom TUI | Cannot render a lineage tree as panes; fights tmux's layout engine |
| Tier C only — structured dashboard, no agent screens | Loses interactive approval prompts, which are load-bearing |
| `-p --resume` as the primary inbound channel | Forks the session; the tree would stop matching reality |
| Screen-scraping to detect turn completion | Misfires on spinners and long tool calls; transcripts are structured |
| `check_replies()` polling for async results | Requires the model to remember to poll. It will not |
| Exploiting Vibe's `sampling` support | Gives a bare completion, not a turn with session context and tools; and creates cross-harness asymmetry |
| Synchronous `send` | Replaced by handles + bounded `await`; strictly more expressive |
| Fan-out limits as the concurrency rail | Wrong knob — wide is often correct, deep is not |
| One worktree, children in distinct subfolders | Git index/HEAD are worktree-global; confinement is advisory |
| Daemon auto-merging child branches | Silent integration decisions on unreviewed code |
| Lineage-scoped trust | Overridden: agents need full discovery to route themselves |
| Per-project daemon | Breaks the cross-repo case that unrestricted trust enables |
| Prompt composer in the régie | Duplicates the harness's input UI, badly |
| Full theatrical vocabulary for primitives | Clever for a week, then a translation table for every contributor |
| Observability-only v1 | Rejected by owner: no scope compromise, sequence instead |
