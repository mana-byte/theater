# Theater — implementation plan

*Companion to `init_idea_grilled.md`. That document says what to build and why; this one says in what order and how to know each step worked.*

Scope is the full design — no cuts. The only concession to risk is **ordering**: the six unverified assumptions in §17 of the spec are resolved before anything is built on top of them, and the Claude Code adapter lands at phase 2 rather than last.

Day estimates are uncalibrated. Treat them as relative sizing.

> **Revision — 2026-08-11.** After `spike_results.md`, the dependency graph was redrawn. The original plan treated identity and delivery as monolithic, which made 0.1 and 0.3 gate the entire project. They don't.
>
> - **Identity has three independent paths.** When the daemon spawns a session it already knows the pane, and passes the id to that participant's MCP server. `$TMUX_PANE` inheritance is only needed for **adoption** of hand-started sessions, and even there an agent can shell out `echo $TMUX_PANE` and call `register_pane()` itself.
>
> **Revision — 2026-08-11, later.** Spike 0.7 (run while building 1a) invalidated the mechanism above, though not the conclusion. A stdio MCP server does **not** inherit the harness's environment: when a server config omits `env`, the SDK substitutes `get_default_environment()`, an allowlist of six posix variables (`mcp/client/stdio/__init__.py:28-44,127`), and Vibe passes `env=srv.env or None` (`registry.py:302,319`). So a `THEATER_ID` *environment* stamp never arrives, and neither does `$TMUX_PANE` — for anyone, ever. **Identity rides on argv instead: `theater mcp --id <id>`.** Two consequences downstream: spike 0.1's answer is now known to be *no* without running it, and phase 1b's primary adoption path is dead — `register_pane` is promoted from fallback to primary.
> - **`send-keys` only gates `send`.** A spawned agent's first prompt goes on argv. `spawn` + `await` + worktrees + rails are all reachable without ever injecting keystrokes.
>
> Consequently: phase 1 splits into a spawned path (unblocked) and adoption (needs 0.1); phase 5 splits into jobs (unblocked) and live delivery (needs 0.3). **No phase is gate-blocked.**

---

## Phase 0 — Spikes (½–1 day)

Six throwaway scripts. No production code. The output is `docs/spike_results.md` recording what is true.

**Status: 0.2 resolved (green). 0.1 / 0.3 / 0.4 blocked — tmux denylisted in the agent environment. 0.5 / 0.6 not run — require live harness invocation.** None of these block the start of implementation; see the revision note above. Procedures for manual execution are in `spike_results.md`.

| # | Question | Procedure | If it fails |
|---|---|---|---|
| 0.1 | Does `$TMUX_PANE` reach the MCP server process? | Register a trivial stdio MCP server with Vibe and Claude Code that writes `os.environ` to a file. Launch each inside tmux. Inspect. | Affects **adoption only**. Spawned sessions get identity from the `THEATER_ID` stamp. Fall back to the agent shelling out `echo $TMUX_PANE` and calling `register()`. |
| 0.2 | Claude Code transcript format | Locate the JSONL, run a session with a tool call, diff the file. Determine: end-of-turn marker, tool-call records, session id, path pattern. | If end-of-turn is ambiguous, CC becomes observe-only in the tree with coarse status. |
| 0.3 | Does `send-keys` survive a repainting TUI? | Inject a single-line prompt, a multi-line prompt, and a prompt during an active tool call, into both harnesses. | Affects **`send` only**, not `spawn`. Investigate `send-keys -l` with bracketed paste, or `load-buffer` + `paste-buffer`. If still unreliable, drop `send` to live panes and keep `spawn` + `await`. |
| 0.4 | tmux presence variables | `display-message -p` for `pane_active`, `pane_in_mode`, `session_attached`, `cursor_x`. Confirm behaviour when detached. | Degrade human-presence detection to input-buffer scrape only. |
| 0.5 | MCP client request timeouts | Register an MCP tool that sleeps N seconds. Bisect the ceiling for both harnesses. Test whether `notifications/progress` extends it. | Sets the default for `await(max_wait=)`. No failure mode, just a number to learn. |
| 0.6 | `--resume` against a live session | Resume a Vibe session id while that session is still open. Check for corruption of `messages.jsonl`. | If safe, `spawn(warm=true)` is cheap. If not, drop warm spawn from v1. |

**Exit criteria:** `spike_results.md` exists and every spike is either answered or explicitly deferred with a recorded workaround. There are no longer any go/no-go spikes — 0.1 and 0.3 each constrain one feature rather than the architecture.

**Findings folded into later phases:**
- Claude Code exposes `message.stop_reason == "end_turn"`; Vibe has no stop field and ends a turn on an `assistant` record with no `tool_calls`. `is_turn_end()` stays in the `Harness` interface and is implemented differently per adapter.
- Claude Code transcripts are locatable from cwd alone (`~/.claude/projects/<cwd-slug>/<sessionId>.jsonl`) with no env inheritance required.
- Both harnesses record their own internal subagent trees (`isSidechain`, `meta.json.child_sessions[]`). Lineage therefore has **two edge kinds** — Theater-spawned and harness-internal — which the régie must distinguish. `init_idea_grilled.md` §5 needs amending.

---

## Phase 1a — Daemon core + spawned identity (2–3 days) — **DONE** (pending one live check)

The daemon becomes real and can create participants it identifies by construction. No adoption, no observation yet.

The ordering here is deliberate: **spawn before adopt.** A spawned participant's identity is bookkeeping — the daemon called `tmux new-window`, holds the pane id, and hands that participant's MCP server its id on argv. Nothing is inferred, so nothing depends on 0.1.

**Build**
- `theater/daemon/store.py` — SQLite schema and migrations
- `theater/daemon/server.py` — unix socket, newline-delimited JSON, one connection per client
- `theater/daemon/registry.py` — participant CRUD, tier assignment, lineage
- `theater/tmux/client.py` — minimal subprocess wrappers: `new-window -d`, `list-panes`, `kill-pane` *(pulled forward from phase 3)*
- `theater/daemon/spawner.py` — create window, write per-participant MCP config with `THEATER_ID`, launch harness with the initial prompt **on argv** *(minimal slice of phase 4)*
- `theater/mcp/server.py` — the stdio MCP server each agent spawns; reads `THEATER_ID`, connects to the daemon socket, auto-starting it if absent
- `theater/mcp/tools.py` — `whoami`, `list_participants` (static fields only)
- `theater/cli.py` — `theater ls`, `theater daemon`, `theater spawn`

**Schema**

```sql
participants(
  id TEXT PRIMARY KEY, harness TEXT, tier TEXT,
  tmux_pane TEXT, cwd TEXT, branch TEXT,
  session_id TEXT, parent_id TEXT,
  status TEXT, last_activity TEXT, created_at TEXT
);
jobs(
  handle TEXT PRIMARY KEY, caller_id TEXT, target_id TEXT,
  kind TEXT,            -- send | spawn
  prompt TEXT, state TEXT,   -- queued | running | done | error
  result TEXT, error_code TEXT,
  created_at TEXT, finished_at TEXT
);
bus(
  id INTEGER PRIMARY KEY, ts TEXT,
  from_id TEXT, to_id TEXT, kind TEXT, payload TEXT
);
budgets(tree_root_id TEXT PRIMARY KEY, tokens INTEGER, cents INTEGER, limit_cents INTEGER);
```

**Tier assignment on connect**, in priority order:

```
--id on argv, id known        → Spawned   (daemon already holds pane; authoritative)
register_pane(pane_id)        → Adopted   (agent reads its own $TMUX_PANE and calls back)
none of the above             → External  (emit-only)
```

`$TMUX_PANE present → Adopted` was the third row here. It is gone: per spike 0.7 that variable cannot reach an MCP server process, so the row could never fire.

**Exit criteria:** `theater spawn vibe "say hello"` creates a hidden window, the participant appears in `theater ls` with the correct pane id, cwd, and `Spawned` tier, and the harness answers the argv prompt. Kill the pane; it disappears within a second.

**Demo:** `theater ls` is already mildly useful on its own.

### Delivered

52 tests green (`uv run pytest`). Modules: `paths`, `models`, `protocol`, `client`, `cli`, `daemon/{store,registry,server,spawner}`, `tmux/client`, `harness/launch`, `mcp/{tools,server}`.

Verified out of process with a real CLI and a real socket: `theater ls` auto-starts the daemon, participants registering over the socket appear with the right tier and the unaddressable marker, `theater stop` shuts it down cleanly.

**Deviations from the plan above**

| Planned | Built | Why |
|---|---|---|
| identity via `THEATER_ID` env | `theater mcp --id <id>` on argv | spike 0.7: env does not survive the SDK's stdio launch |
| tmux hook for death detection | 1 s poll of `list-panes -a` | a hook lives in the user's tmux config, which does not survive `kill-server` or a reload; the daemon's correctness should not depend on it |
| `mcp>=1.28.0` | `mcp>=2.0.0` | 2.0 renamed `FastMCP` to `MCPServer`. Interop with 1.x clients is unaffected: an `initialize` offering `2025-11-25` is echoed back verbatim (`mcp/server/runner.py:425`), and the `2026-07-28` revision is only reachable via `server/discover` |
| — | `spawn_session` exposed as an MCP tool | agents can create children, not just the human. Requested mid-phase |
| — | one-pane-one-holder eviction in the registry | two records on one pane means a delivery could be typed into the wrong terminal |

**The real run found a bug, now fixed and guarded.** A user ran `theater spawn vibe "say hello" --approval manual` inside tmux and it failed with `create window failed: index 0 in use`. The user's tmux session is the unnamed one (named `0`); `new-window -t 0` is parsed as *window index 0*, not as the session named `0`. The session target must carry a trailing colon: `-t 0:`. tmux forbids `:` in session names, so appending is safe. The same latent bug existed in session-scoped `list_panes`, which used `-t <name>` (a window target) instead of `-s -t <name>`; it had no callers so it never surfaced. Both are fixed in `theater/tmux/client.py`. The module docstring now documents the rule, and `tests/test_tmux_client.py` asserts the exact argv of `new_window`, `list_panes`, `kill_pane`, and `send_keys` — argv is the thing that broke, and it is the only thing that can be checked without a tmux server.

**Still unverified — needs a human at a terminal.** The argv layer is guarded; the behaviour is not. One incidental real run proved `ensure_session`, `new-window -d -P -F`, argv-prompt launch and pane-id capture. Not yet proven: that a spawned child's MCP server connects back with its id (the run was killed too early), `send-keys`, `capture-pane`, and pane-death reaping. The 1a exit criteria are met only when someone runs, inside tmux:

```
theater spawn vibe "say hello" --approval manual
theater ls          # expect tier=spawned once the child's MCP server has connected
                    # then kill the pane; expect it to drop within ~1 s
```

---

## Phase 1b — Adoption (1 day) — ~~needs spike 0.1~~ **DONE**

Hand-started sessions join the registry.

Spike 0.1 is answered, negatively, without needing to be run: `$TMUX_PANE` cannot reach an MCP server process, so the planned primary path does not exist. What was the fallback is now the mechanism.

- **Primary: `register_pane(pane_id)`** — the agent runs `echo $TMUX_PANE` with its own shell tool and calls back. Built and tested in 1a; the registry promotes External -> Adopted on receipt.
- **`theater adopt` CLI command.** A human-run command that resolves the caller's own `$TMUX_PANE` and calls a new `adopt` daemon method. The daemon does the tmux lookup (it has tmux access; the CLI process may not have the venv's PATH) to learn the pane's `current_command` and `current_path`, maps the command to a harness via `_detect_harness`, and calls `register`. A `--harness` override covers the case where `pane_current_command` is the python interpreter rather than the harness binary. No model in the loop.
- **Harness-name normalization.** `register()` now normalizes the harness name through `_ALIASES` (`claude_code` -> `claude`, `Claude` -> `claude`, `mistral-vibe` -> `vibe`, etc.) before storing. Unknown names pass through unchanged — a genuinely unknown harness is not an error at first contact, just an unobservable one, and the observer's `_warn_unobservable` already covers that case. This was the logged phase-2 follow-up: without normalization, a misreported name registers happily and is then unobservable forever.
- **Unmanaged pane sweep.** A new `participants.unmanaged` daemon method runs `list-panes -a` (now with `#{pane_current_command}` added to the format), filters to panes whose command matches a known harness binary, and excludes panes that already have a participant record. `theater ls` shows them in a separate section below the participants list — not in the tree (they have no lineage and no id), but visible, because a tree that silently omits half the agents on the machine is worse than one that admits ignorance.
- Claude Code correlation (match cwd-slug against `~/.claude/projects/`) is not needed: the observer already finds transcripts by cwd, and the harness adapter's `find_transcript` handles the slug lossily without inverting it.

**Delivered.** 156 tests green (135 + 21 new). New: `theater adopt` command, `adopt` daemon method, `participants.unmanaged` daemon method, `normalize()` in `harness/__init__.py`, `known_binaries()` in `harness/__init__.py`, `pane_current_command` in the tmux `Pane` format. `_format_ls` takes an optional `unmanaged` list and renders it below participants.

**Exit criteria met:** a hand-started session can be adopted with `theater adopt` (no id, no model call); panes running a harness that never registered appear in `theater ls` as unmanaged.

---

## Phase 2 — Observation: harness adapters (3–4 days)

The highest-risk abstraction in the project. Both adapters are written in this phase, deliberately.

**The interface** — write this first, from the spike results, then implement twice:

```python
class Harness(Protocol):
    name: str

    def launch_cmd(self, *, prompt, cwd, approval, worktree) -> list[str]: ...
    def find_transcript(self, *, pane, spawned_after) -> Path | None: ...
    def parse(self, raw_lines: Iterable[str]) -> Iterator[Event]: ...
    def is_turn_end(self, event: Event) -> bool: ...
    def native_children(self, transcript: Path) -> list[NativeChild]: ...
    def format_injection(self, prompt: str) -> list[str]: ...   # tmux send-keys args  (phase 5b)
    def input_buffer_nonempty(self, pane_tail: str) -> bool: ...                     # phase 5b
```

`native_children()` covers the two-edge-kind lineage found in 0.2 — Claude Code's `isSidechain` records and Vibe's `meta.json.child_sessions[]`. The régie renders harness-internal children differently from Theater-spawned ones.

Two methods are only exercised by phase 5b and can be left `NotImplemented` until spike 0.3 clears.

**Known implementations from 0.2:**

```
                    Claude Code                          Vibe
find_transcript     ~/.claude/projects/<cwd-slug>/       ~/.vibe/logs/session/<dir>/
                    <sessionId>.jsonl                    messages.jsonl
is_turn_end         message.stop_reason == "end_turn"    assistant record with no
                                                         "tool_calls" key
native_children     records where isSidechain == true    meta.json.child_sessions[]
cwd                 per-record .cwd                      meta.json.environment
                                                         .working_directory
```

`Event` is normalized: `{ts, kind: user|assistant|tool_call|tool_result|error, text, tool_name}`.

**Build**
- `theater/harness/base.py`, `vibe.py`, `claude_code.py`
- `theater/daemon/observer.py` — async tail per participant, feeds the bus and derives status
- Status derivation: `idle` / `working` / `awaiting_input` / `dead`, from the event stream

**Rule:** if implementing `claude_code.py` requires changing the `Harness` signature, that is a success, not a setback — it is exactly the information the phase exists to produce. Record the change.

**Exit criteria:** `theater ls --watch` shows live status transitions for a Vibe session and a Claude Code session, driven purely by transcript tailing. `theater bus` streams normalized events from both.

### Delivered

116 tests green. New: `harness/{base,claude_code,vibe}.py`, `daemon/observer.py`, and the `theater bus` / `theater ls --watch` surfaces. `harness/launch.py` is gone, dissolved into the three harness modules.

**Deviations from the interface sketched above.** Producing these was the point of the phase.

| Planned | Built | Why |
|---|---|---|
| `launch_cmd(...) -> list[str]` | `plan_launch(...) -> LaunchPlan` | argv alone cannot carry the MCP wiring: Vibe needs `$VIBE_MCP_SERVERS` in the environment, Claude Code a `--mcp-config` file. The plan carries argv and env together |
| `find_transcript(*, pane, spawned_after)` | `find_transcript(*, cwd, session_id, after)` | nothing links a pane to a transcript — neither harness records a pane id anywhere. `cwd` is the only join key both write down. `after` is a floor for *spawned* participants only; an adopted session's transcript predates our first sight of it |
| `parse(raw_lines) -> Iterator[Event]` | `parse(line, index) -> list[Event]` | one record can hold an assistant message *and* several tool calls, so the mapping is one-to-many. Per-line also keeps byte offsets and partial-line handling in the observer, where the file actually is |
| `is_turn_end(event)` | `Event.turn_end` | turn-endness is a property of the raw record, not of the normalized event. Deciding it inside `parse`, while the record is still in hand, avoids a second and lossier inspection |
| status ∈ idle/working/awaiting_input/dead | idle / working only | not derivable — see below |

**`awaiting_input` cannot come from a transcript.** A harness blocked on a permission prompt writes nothing at all: the prompt is a screen state, not a record. Both adapters therefore emit only IDLE and WORKING, and a blocked agent reads as WORKING. The real signal is `capture-pane`, which belongs to phase 5b. Recorded rather than faked: a status that lies in exactly the situation you most need it is worse than one that admits its range.

**Gate — the cross-harness thesis holds.** Claude Code fit the interface without contortion. The asymmetries are real but stay inside the adapters:

```
                     Claude Code                     Vibe
turn end             stop_reason == "end_turn"       assistant record, no tool_calls key
timestamps           per record, ISO 8601            none, anywhere
content blocks       exactly one per record          message + N calls per record
tool_result names    absent (tool_use_id only)       present
native children      isSidechain records             meta.json.child_sessions[]
```

**Verified out of process against real transcripts**, not fixtures:

- A live Vibe session was located from its cwd in ~1 s, and its prose, tool calls, and tool results appeared on the bus as `agent.assistant` / `agent.tool_call` / `agent.tool_result`, with status moving IDLE → WORKING off the turn boundary.
- A real Claude Code session was located the same way and attached, skipping 371 existing records. All 1103 records of a 3 MB real transcript parse without raising, yielding 711 events whose 25 `turn_end`s match the 25 `end_turn` stop reasons counted independently.
- `theater bus -f` streams over the socket and flushes per batch.

**Three bugs the tests found, worth remembering:**

1. *Truncation is not detectable by size.* A transcript rewritten to the same length reads as "unchanged", after which the byte offset points into the middle of a different record and every later parse is garbage. The observer now fingerprints `(size, mtime_ns)` and re-reads from the top when a file changes without growing.
2. *An unobservable participant was silent.* `hello` accepts any harness string, and the observer skipped unknown ones with no diagnostic anywhere — the exact blind spot this design keeps trying to avoid. It now warns once per participant, naming the harnesses it can read.
3. *A followed feed piped anywhere produced nothing*, because Python block-buffers a non-tty stdout. Indistinguishable from a hang.

**Follow-up for phase 1b:** harness names are not normalized. A session that reports `claude_code` or `Claude` instead of `claude` registers happily and is then unobservable forever. Adoption is the phase that owns getting hand-started sessions correctly identified, so the fix belongs there — either an explicit alias map or a `--harness` value the daemon supplies rather than the agent claiming one.

---

## Phase 3 — The régie (3–4 days)

**Build**
- `theater/tmux/client.py` — typed wrappers over `tmux` subprocess calls
- `theater/tmux/panes.py` — `break-pane` / `join-pane` staging, zoom, grid mode
- `theater/regie/app.py` — Textual app; adopt `$TMUX` session or create one
- `theater/regie/tree.py` — lineage tree with live status
- `theater/regie/bus_view.py` — scrolling inter-agent traffic

Layout: régie left (~40 cols), stage right. Agents park in hidden windows. Selection swaps the stage occupant. Read-mostly: navigate, select, zoom, kill.

**Watch for:** geometry. The staged pane must be resized to the stage dimensions on every swap and on terminal resize, or the agent renders for the wrong width.

**Exit criteria:** first half of the acceptance test — three agents started by hand appear in the tree, selecting one puts it on the stage fully interactive, `<prefix>d` detaches and reattaches with everything intact.

---

## Phase 4 — Spawn, complete (1–2 days)

Phase 1a built the minimum spawner needed for identity. This completes it.

**Build**
- `spawn(harness, prompt, approval, worktree)` MCP tool and régie action
- `parent_id` wiring so spawned windows nest correctly in the lineage tree
- `git worktree add` per child; branch naming `theater/<child-id>`
- `approval` is required, no default; surfaced in the régie when a child blocks on a prompt

**Exit criteria:** an agent calls `spawn`, a new window appears in the tree as its child, the child works in its own worktree, and killing the parent from the régie offers to strike the subtree.

---

## Phase 5a — Jobs and await (2–3 days) — UNBLOCKED

The core orchestration loop, over spawned children only. No keystroke injection anywhere in this phase: a spawned child receives its prompt on argv, so `spawn` is itself a complete delivery mechanism.

**Build**
- `theater/daemon/jobs.py` — handles, state machine, per-target queue
- `await(handles, max_wait)` — bounded, returns `done | still_running | error(code)`
- Result extraction: the argv prompt carries a completion contract; the phase-2 observer detects turn end and captures the assistant text as the job result
- Error codes: `crashed`, `killed`, `budget_exceeded`, `timeout`

**Exit criteria:** a Vibe agent spawns a worker, awaits it, receives the result, and uses it. Fan-out of three spawned workers awaited together completes correctly. This is most of the acceptance test's second half.

---

## Phase 5b — Live delivery via `send` (1–2 days) — needs spike 0.3

Injection into an already-running session. The only part of the design that depends on `send-keys` being reliable.

**Build**
- Inbound delivery via `send-keys`, gated on target status from phase 2
- `theater/tmux/presence.py` — human detection; `human_present` error back to the caller
- Per-target queueing when the target is busy

**Exit criteria:** an agent `send`s to a hand-started peer mid-session, the prompt arrives intact, the reply resolves the handle. Typing into the target pane and re-sending produces `human_present` rather than a corrupted input line.

**If 0.3 is red:** ship without `send`. `spawn` + `await` is a complete orchestration story on its own, and External participants were already emit-only — this would extend the same limitation to Adopted ones.

---

## Phase 6 — Rails (1–2 days)

- Depth cap (default 3), enforced at `spawn`
- Cycle detection on the await graph, rejecting an await that would close a loop
- Per-tree budget with hard subtree stop
- Queue backpressure and per-participant `send` rate limit

**Exit criteria:** deliberately construct A→await B→await A and get a clean rejection, not a hang. Set a $0.10 budget and watch a subtree stop.

---

## Phase 7 — Restart and reconciliation (1–2 days)

- Daemon restart: rebuild the registry from tmux, reload jobs and bus from SQLite
- Reconcile disagreements between tmux reality and stored state; orphaned `running` jobs resolve to `crashed`
- Régie reconnect without losing the staged pane

**Exit criteria:** `kill -9` the daemon mid-job. It restarts, the tree is intact, the orphaned job reports `crashed` to its caller.

---

## Phase 8 — Acceptance and hardening (2 days)

Run the full acceptance test end to end:

> Two Vibe sessions started by hand plus one Claude Code session all appear in the tree with live status; one Vibe agent spawns a worker, awaits it, and uses the result; the whole thing survives a tmux detach and a daemon restart.

Then: transcript parser version pinning with loud failure, `theater doctor` for environment checks, README, packaging.

---

## Module layout

```
theater/
├── cli.py                 # theater ls | bus | daemon | doctor | regie
├── daemon/
│   ├── server.py          # unix socket, ndjson
│   ├── registry.py        # participants, tiers, lineage
│   ├── observer.py        # transcript tailing → events → status
│   ├── jobs.py            # handles, queue, await, failure codes
│   ├── bus.py             # event log
│   ├── rails.py           # depth, budget, cycles, rate limit
│   ├── lifecycle.py       # tmux hooks
│   └── store.py           # sqlite
├── mcp/
│   ├── server.py          # stdio server spawned by each agent
│   ├── tools.py           # the eight tools
│   └── client.py          # daemon socket client, auto-start
├── harness/
│   ├── base.py            # Harness protocol + Event
│   ├── vibe.py
│   └── claude_code.py
├── tmux/
│   ├── client.py          # subprocess wrappers
│   ├── panes.py           # staging, zoom, grid
│   └── presence.py        # human detection
└── regie/
    ├── app.py
    ├── tree.py
    └── bus_view.py
```

Python 3.12, `uv`, Textual. Mirrors Vibe's structure so its MCP and Textual code read as a reference.

---

## Critical path

**Main spine — fully unblocked today:**

```
0.2 ✓
  └─> 1a ──> 2 ──> 4 ──> 5a ──> 6 ──> 7 ──> 8
              └──> 3 ────────────────┘
```

**Spike-gated side branches — mergeable whenever their spike clears:**

```
0.1 ──> 1b   RESOLVED (negative). $TMUX_PANE cannot reach an MCP server;
             1b proceeds on register_pane and is no longer gated.
0.3 ──> 5b   (send to live panes)
0.5 ──> tunes the default for await(max_wait) in 5a
```

Neither side branch blocks the spine, and neither is on the path to the acceptance test except for its "started by hand" clause — which 1b supplies and which `theater spawn` can stand in for until then.

Phase 2 remains the bottleneck and the one to overstaff. Phases 3 and 4–6 parallelize after it.

## Gates

One real gate remains:

- **After phase 2.** If the Claude Code adapter cannot be written against the phase-2 interface without contortion, the cross-harness thesis is weaker than assumed. Better to learn it here than at phase 8. Early evidence from 0.2 is encouraging: the two harnesses answer `is_turn_end()` by entirely different means and the interface absorbed it.

The former phase-0 gate is retired. 0.1 and 0.3 each constrain a single feature with a working fallback, so a red result narrows the product rather than invalidating the design.

## Deliberately deferred

Not in v1, recorded so they aren't re-proposed as oversights:

- Remote / multi-machine participants
- Any harness beyond Vibe and Claude Code
- Auto-merge of child branches
- Rolling LLM summaries in `list_participants`
- A prompt composer in the régie
- Non-tmux operation
- Multi-user or multi-tenant trust
