# Theater — architecture

This document explains *why* Theater is shaped the way it is. The README covers
what the commands do; this covers the constraints that produced them, and the
alternatives that were rejected along the way.

Read `docs/init_idea_grilled.md` for the original design interrogation. This
document is the state of the code as of v1.1, and where the two disagree, the
code wins.

---

## 1. The one constraint that shapes everything

MCP has no server-initiated turn.

An MCP server cannot wake an agent up. It answers when the agent calls a tool
and is silent otherwise. There is no push, no interrupt, no way for the daemon
to hand a running Claude Code session a new instruction through the protocol it
is already speaking.

Every significant decision in Theater follows from working around that:

| Consequence | Why it follows |
|---|---|
| tmux is a hard dependency | `send-keys` into a pane is the only inbound channel that exists |
| participants are tiered | a participant without a pane is physically unreachable, not merely unprivileged |
| replies come back through `await`, not a callback | the reply must be the return value of a tool call the agent already made |
| the daemon reads transcripts off disk | an agent mid-tool-call makes no MCP calls, and that is exactly when you want to know what it is doing |

If MCP ever grows a server-initiated turn, the tier system and half of the tmux
layer become optional. Until then they are load-bearing.

---

## 2. Map

```
  tmux session
  ┌───────────────────────────────────────────────────────────────┐
  │  window: régie          window: agent-a       window: agent-b │
  │  ┌──────────────┐       ┌──────────────┐      ┌─────────────┐ │
  │  │ theater regie│       │ vibe         │      │ claude      │ │
  │  │  (textual)   │       │  + MCP stdio │      │ + MCP stdio │ │
  │  └──────┬───────┘       └──────┬───────┘      └──────┬──────┘ │
  └─────────┼──────────────────────┼─────────────────────┼────────┘
            │                      │                     │
            │  NDJSON over unix socket ($THEATER_HOME/daemon.sock)
            │                      │                     │
            ▼                      ▼                     ▼
  ┌───────────────────────────────────────────────────────────────┐
  │                          theater daemon                        │
  │                                                                │
  │   methods ── registry ── store (SQLite)      observer          │
  │      │          │           │                   │              │
  │      │        rails       jobs ◄────────────────┘ turn-end     │
  │      │                                                          │
  │   spawner ──► tmux new-window                                  │
  │   send    ──► tmux send-keys                                   │
  └───────────────────────────────────────────────────────────────┘
                              │  tails
                              ▼
              ~/.vibe/logs/... , ~/.claude/projects/...
                    (transcripts the harnesses already write)
```

Three kinds of process:

- **the daemon** — one per machine, holds all state, owns the socket
- **MCP servers** — one short-lived stdio process per agent, a thin client
- **the régie** — a Textual TUI, also just a client

The daemon is the only thing that writes to SQLite or shells out to tmux. The
MCP server does neither; it forwards. This is why the MCP layer is 358 lines
and testable without a daemon at all.

---

## 3. Identity: three tiers

`theater/models.py` defines the tier ladder, and it is the central idea of the
system.

| Tier | How it got here | Identity | Addressable |
|---|---|---|---|
| `SPAWNED` | the daemon created the pane | by construction | yes |
| `ADOPTED` | pre-existing pane, self-registered | self-reported, trusted | yes |
| `EXTERNAL` | no pane at all | self-reported | **never** |

`Participant.addressable` is not a permission flag. It is a physical statement:
without a pane there is nowhere to `send-keys`, so an External participant can
call out and can never be called. It emits into the bus and stays visible in
the tree, which is most of the value; it simply cannot receive.

### How a participant acquires an id

Three routes, in descending order of confidence:

1. **argv** — `theater mcp --id <id>`. The spawner mints the id *before* the
   pane exists, precisely so it can be baked into the child's MCP config.
2. **`$THEATER_ID`** — fallback for the same case.
3. **`hello` with no id** — the daemon mints one and files the caller by
   whether it reported a pane.

Route 1 exists because of a specific bug in the middle of the stack: the MCP
SDK replaces the inherited environment with a six-variable allowlist
(`mcp/client/stdio/__init__.py:127`). Environment variables set on the tmux
window do not reach the MCP server process. `$TMUX_PANE` is lost the same way,
which is why `register_pane` exists as an adoption fallback — the agent reads
its own pane id with its shell tool and hands it over, because it can see what
its MCP server cannot.

---

## 4. Transport

Newline-delimited JSON over a unix socket. Deliberately **not** JSON-RPC.

```
-> {"id": 1, "method": "participants.list", "params": {}}
<- {"id": 1, "ok": true, "result": [...]}
<- {"id": 1, "ok": false, "error": {"code": "not_found", "message": "..."}}
```

`PROTOCOL_VERSION = 1` in `theater/protocol.py`. JSON-RPC was rejected because
nothing here needs batching, notifications, or a standard error registry, and
the framing above is auditable by eye with `nc`. Errors carry a `code` that
maps to a `TheaterError` subclass, so a client can branch on `busy` versus
`human_present` without parsing prose.

The daemon exposes 16 methods (`theater/daemon/methods.py`); the MCP server
exposes 7 tools to agents (`theater/mcp/server.py`), namespaced `theater_*`.
The two sets are not the same and should not be: `shutdown`, `adopt`, and
`bus.tail` are operator verbs, not agent verbs.

---

## 5. State

One SQLite file, `$THEATER_HOME/theater.db`, `SCHEMA_VERSION = 1`.

| Table | Holds |
|---|---|
| `participants` | the registry: id, harness, tier, pane, cwd, branch, parent, status |
| `jobs` | one row per unit of delegated work, keyed by handle |
| `bus` | append-only activity feed |
| `budgets` | per-tree accounting — created, not yet used |

The store is **synchronous on purpose**. Every call is a local SQLite
statement measured in microseconds; wrapping them in a thread pool to satisfy
`async` aesthetics would add real complexity to buy nothing. The daemon's event
loop blocks on these calls and that is fine.

The bus is an activity feed, not an archive. Event text is clipped at
`MAX_TEXT = 2000` chars, because a single tool result is routinely 25 KB and
keeping it whole would put megabytes of file contents into SQLite for something
the TUI renders as one line. The transcript on disk stays the full record —
which is exactly what `read_transcript` reaches for when a caller needs the
untruncated text.

---

## 6. Observation

The observer (`theater/daemon/observer.py`, the largest module at 528 lines)
tails the transcript files the harnesses already write.

**Why not have agents self-report?** Two reasons, and the second is decisive:

1. An agent mid-tool-call makes no MCP calls. That is precisely the window in
   which you want to know it is alive and working.
2. Adopted sessions that predate Theater would be invisible. The whole promise
   of adoption is that you can point Theater at a session already running.

The observer therefore never asks. It reads.

### Attaching

The observer always attaches at **EOF** and records how many records it
skipped. A session that has been running for an hour before adoption does not
replay an hour of history onto the bus. `skipped_records` appears in the
`agent.transcript` bus event so the gap is explicit rather than silent.

### Status derivation — three signals, three failure policies

This is the subtlest part of the system and the source of most v1 bugs.

| Signal | Drives | Failure policy | Source |
|---|---|---|---|
| transcript growth | `IDLE` / `WORKING` | source of truth, no heuristic | disk file |
| `capture-pane` screen | `AWAITING_INPUT` | accept false negatives | rendered screen |
| `pane_in_mode` | blocks `send-keys` | accept false negatives, **never** false positives | tmux fact |

They share the phrase "accept false negatives" and mean different things by it,
because the cost of being wrong differs per consumer:

- A wrong `AWAITING_INPUT` misleads a human reading the régie for a fraction of
  a second, until the next transcript growth corrects it.
- A wrong "no human present" injects keystrokes into a pane a human is using.
  That is unrecoverable, so `tmux/presence.py` uses only `pane_in_mode` — copy
  mode, a tmux fact with no heuristic in it.

An earlier version scraped the pane's input buffer to detect a human typing. It
was removed: it cannot distinguish agent output from unsubmitted human input,
and the last line of an agent pane is almost always non-empty text. It blocked
legitimate sends constantly. Copy mode is narrow, but it is never wrong.

### Two independent quiet timers

```
RELOCATE_TIMEOUT      = 5.0    # Vibe rotates its session dir per turn
AWAITING_INPUT_TIMEOUT = 10.0  # quiet long enough to be a prompt, not a pause
POLL_INTERVAL          = 0.25
SEARCH_INTERVAL        = 2.0
SYNC_INTERVAL          = 1.0
```

These were one timer in v1. Sharing them made `AWAITING_INPUT` unreachable:
relocation fired first, every time. They measure different things and must stay
separate — this is a scar, not a preference.

---

## 7. Delegation: the job lifecycle

Two verbs create work, and they differ in how the prompt is delivered.

**`spawn_session`** — prompt goes on the child's **argv**. This path does not
depend on keystroke injection working at all, which is why it is the reliable
one.

```
mint id  →  worktree (opt)  →  write MCP config  →  tmux new-window  →  attach pane
```

The order is not arbitrary. The id must exist before the config is written
because the config contains `theater mcp --id <id>`; the pane id is only known
after `new-window` returns it. Nothing is ever inferred. If `new-window` fails
the participant is marked dead immediately rather than left as a `STARTING`
ghost the régie would draw forever.

**`send`** — prompt is typed into an existing pane with `send-keys`, after
three gates:

```
addressable?  →  no  →  not_addressable
human at the pane?  →  yes  →  human_present
already running a send?  →  yes  →  busy
```

Then the job is created with handle `<target_id>#<seq>`.

### Completion

The caller does not get a callback. It gets a handle, and calls
`await_sessions(handles, max_wait=60)`.

```
caller                    daemon                    target
  │  send ──────────────────►│
  │◄──────────── handle ─────│── send-keys ──────────►│
  │                          │                        │ (works)
  │  await(handle) ─────────►│                        │
  │      (blocked)           │◄── transcript grows ───│
  │                          │    turn_end detected   │
  │◄──── result ─────────────│  jobs.finish()         │
```

`await_jobs` creates an `asyncio.Event` per handle. The observer detects
`turn_end` in the transcript and calls `jobs.finish()` with the assistant's
final text as the result, which sets the event and wakes the caller. **This
blocks the caller's MCP request only** — the daemon and every other participant
keep running. That is the whole trick: the reply arrives as the return value of
a tool call the agent already made, so no inbound-reply channel is needed.

Job states are `running`, `done`, `crashed`, `killed`. `timeout` is
deliberately **not** a state: it is what `await` returns when the caller stops
waiting, not something that happens to the job. A job still running at the
ceiling comes back as `running` and the caller decides whether to re-await.

The in-memory events do not survive a daemon restart, and that is correct — a
restarted daemon has no observer attached yet, so an in-flight await would have
to re-poll regardless. `await_jobs` recreates a missing event rather than
failing.

---

## 8. Safety rails

Three, in `theater/daemon/rails.py`, all checked before a spawn:

| Rail | Default | Behaviour |
|---|---|---|
| depth cap | `DEFAULT_DEPTH_CAP = 3` | reject spawns deeper than 3 levels |
| cycle check | — | reject if the target is an ancestor of the caller |
| tree budget | `DEFAULT_BUDGET = 20` | reject the next spawn once the tree hits 20 participants |

The cycle check works because **the spawn tree is the await tree**. A child
awaiting its own ancestor is a deadlock by construction, and it is cheap to
refuse.

The budget rail **rejects the next spawn and nothing else**. It does not kill
anything already running. An earlier `hard_stop_tree` was deleted in v1.1
because it killed nothing — it walked the tree and did no work. Note that
`docs/implementation_plan.md:374` still describes the old intent; that file is
a historical planning record and has been left unedited.

---

## 9. Harness abstraction

`theater/harness/base.py` defines an ABC with six methods. To add a harness you
implement:

| Method | Answers |
|---|---|
| `plan_launch` | what argv, env, and config files start this thing |
| `find_transcript` | where does it write its transcript |
| `session_id` | what does it call this session |
| `parse` | turn one transcript line into `Event`s |
| `native_children` | does it spawn its own subagents we should show |
| `is_idle_screen` | does this rendered screen mean "waiting for a human" |

Two implementations ship: `claude_code.py` and `vibe.py`. They are genuinely
different — Claude Code writes one JSONL per project directory; Vibe rotates
its session directory per turn, which is the entire reason `RELOCATE_TIMEOUT`
exists.

`parse` takes `clip_text: bool` rather than always clipping, because the same
parser serves two consumers with opposite needs: the bus wants a one-line
summary, `read_transcript` wants the bytes as written. `clipper()` picks the
treatment once instead of each parser redefining it inline — a v1.1 fix that
uncovered a Claude Code path clipping unconditionally despite `clip_text=False`.

---

## 10. The régie

A Textual app (`theater/regie/`) that is *just another client* — it holds no
state the daemon does not have, and killing it affects nothing.

The staging model uses tmux's `break-pane` / `join-pane`: agents park in their
own hidden windows, and staging one moves its pane onto the stage window
without killing anything. Selecting a different agent moves the previous one
back. Nothing is ever restarted to be displayed, which matters — a staged agent
is fully interactive and keeps its scrollback.

`theater/formatting.py` holds the rendering both the CLI and the régie need and
imports neither `rich` nor `textual`, so the plain CLI stays dependency-light
and the two never drift on how a tier or status is spelled.

---

## 11. Module map

```
theater/
├── cli.py 395            argparse; daemon mcp ls spawn bus kill adopt regie stop
├── client.py 130         DaemonClient, autostarts the daemon
├── protocol.py 46        NDJSON framing, PROTOCOL_VERSION = 1
├── models.py 170         Tier, Status, Participant, Job, error codes
├── paths.py 35           $THEATER_HOME layout
├── formatting.py 109     shared CLI/régie rendering, no rich/textual
├── daemon/
│   ├── observer.py 528   transcript tailing, status, job completion
│   ├── server.py 324     lifecycle only: socket, pidfile, reaper, wiring
│   ├── methods.py 317    16 RPC handlers
│   ├── registry.py 233   tier assignment, pane eviction, lineage
│   ├── store.py 230      SQLite, synchronous on purpose
│   ├── jobs.py 181       JobManager, asyncio.Event per handle
│   ├── worktree.py 158   git worktree per child, branch theater/<child-id>
│   ├── spawner.py 145    LaunchPlan → tmux window
│   ├── rails.py 144      depth / cycle / budget
│   ├── harness_detect.py 85
│   └── lineage.py 73     ancestor_ids, depth_of, root_of, subtree_ids
├── harness/  base.py 273 · claude_code.py 355 · vibe.py 272
├── mcp/      tools.py 206 · server.py 152
├── tmux/     client.py 227 · panes.py 167 · presence.py 56
└── regie/    app.py 384 · tree.py 99 · bus_view.py 37
```

Roughly 5,700 lines, 292 tests.

The v1.1 refactor split `daemon/server.py` (lifecycle vs. methods vs. harness
detection), broke the `store ↔ jobs` import cycle by moving `Job` into
`models.py`, gave the observer's watch loop an explicit `TranscriptCursor`, and
consolidated four hand-rolled tree walks into `lineage.py`. It found three real
bugs in the process, which is the argument for having done it.

---

## 12. Known gaps

- **`Participant.pid` is always `None`.** `registry.attach_pane` nulled it from
  a keyword nobody passed. Now an explicit gap rather than a silent one.
- **`budgets` table is unused.** Token and cost accounting is written but never
  read; the budget rail counts participants instead.
- **tmux behaviour is not covered by tests.** tmux is unavailable in the
  development sandbox, so `tmux/panes.py` asserts argv in tests and the real
  behaviour is verified by hand. Modules that do this say so in their
  docstring.
- **`AWAITING_INPUT` is a display hint.** Never let it gate a control decision;
  that is what `pane_in_mode` is for.
- **Human presence is narrow.** Copy mode only. An agent-aware prompt matcher
  would be better but requires knowing each harness's prompt format, which is
  not stable across versions.

`docs/v2_ideas.md` covers where this goes next.
