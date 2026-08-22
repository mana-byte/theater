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
   │   rpc ── registry ── persistence (SQLite)    observation       │
   │      │          │           │                   │              │
   │      │        rails       jobs ◄────────────────┘ turn-end     │
   │      │                                                          │
   │   spawning ──► tmux new-window                                  │
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
MCP server does neither; it forwards.

---

## 3. Identity: three tiers

`theater/models.py` defines the tier ladder, and it is the central idea of the
system.

| Tier | How it got here | Identity | Addressable |
|---|---|---|---|
| `SPAWNED` | the daemon created the pane | by construction | yes |
| `ADOPTED` | pre-existing pane, self-registered | pane known; transcript untrusted until bound/proven | yes |
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

The participant id belongs to Theater; `session_id` belongs to the underlying
harness and is the opaque value accepted by `spawn_session(resume=...)`. The
observer fills it in only after finding the participant's transcript, so the
agent-facing `whoami`, `list_participants`, `spawn_session`, and `register_pane`
records always carry the field but may initially report it as null. A later
`whoami` or `list_participants` call reads the persisted value once discovery
has completed. Like the rest of the machine-wide participant list, session ids
follow Theater's single-user trust model; they are routing metadata, not
authorization tokens.

Transcript identity has its own provenance ladder:

- `heuristic` — cwd/time or newest matching transcript; useful for candidates,
  never enough to attribute text to a participant.
- `operator` — a same-UID operator ran `theater bind <id> <candidate>
  --confirm-id <id>` after inspecting `theater candidates <id>`.
- `proven` — the daemon matched process-local evidence, such as Codex process
  correlation, to the participant.
- `exact` — the daemon constructed or received exact evidence: spawn/resume
  session ids, Claude lifecycle receipts, or equivalent process receipts.

Spawned sessions are trusted by construction where the launch plan supplies
identity. Adopted Claude, Vibe, OpenCode, and unproven Codex sessions are not:
the observer may keep screen-only status live, but `send` and `read_transcript`
refuse with `transcript_untrusted`/correlation errors until provenance reaches
operator/proven/exact. This is deliberate recovery workflow, not a missing
autobind. A same-UID process that can run `theater` can bind, just as it can
kill; the stable-id confirmation protects against operator mistakes, not a
malicious local user.

Trusted provenance can later lose transcript identity without losing the pane.
Theater derives `transcript_identity_lost` when a trusted pin is positively gone
or no longer a file, or when a newer eligible same-harness/cwd/domain candidate appears
while the pin is inert and the screen is positively WORKING. Elapsed time alone
never triggers it. The old `transcript_location` stays pinned; heuristic
candidates are shown to the operator but never auto-adopted. While quarantined,
screen status continues, but transcript attribution, turn completion, `send`,
`read_transcript`, `recall_read`, and `resume` refuse with candidates/bind
recovery instructions. No participant state column stores this: the condition is
detected only by the observer, cached for the watcher lifecycle, and replayed
once from the audit stream after restart. Active loss audit rows are exempt from
bus age collection; a later bind, transfer-unbind, or trusted attach clears the
replay state and makes the older loss row retention-eligible again.
Generic source failures such as `EIO`, permissions, or an unavailable OpenCode
database retain the binding and use the ordinary observation-failure grace;
they are not identity evidence.

Resume is also provenance-gated. A session id may be resumed only when Theater
has a trusted owner row for that harness/session id and every trusted owner for
that id is dead. A live participant's session id is refused even when exact:
live work goes through `send`, while resume continues dead sessions without two
processes appending to the same harness session.

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

The daemon exposes 33 methods (`theater/daemon/rpc/`); the MCP server
exposes 14 tools to agents (`theater/mcp/server.py`), namespaced `theater_*`.
The two sets are not the same and should not be: `shutdown`, `adopt`, and
`bus.tail` are operator verbs, not agent verbs.

---

## 5. State

One SQLite file, `$THEATER_HOME/theater.db`, owned exclusively by the daemon —
every other process reaches it over the unix socket.

| Table | Holds |
|---|---|
| `participants` | the registry: id, harness, tier, pane, cwd, branch, parent, status |
| `jobs` | one row per unit of delegated work, keyed by handle |
| `bus` | append-only activity feed |
| `touch` | which paths each job changed, and the shas it moved them between |
| `meta` | small key/value durable state — currently the send-sequence counter |
| `budgets` | per-tree accounting — created, not yet used |

The store is **synchronous on purpose**. Every call is a local SQLite
statement measured in microseconds; wrapping them in a thread pool to satisfy
`async` aesthetics would add real complexity to buy nothing. The daemon's event
loop blocks on these calls and that is fine.

### Schema, and why Alembic (v1.3)

Tables are declared once in `daemon/schema.py` as SQLAlchemy **Core** metadata
— not the declarative ORM, because `models.py` holds plain dataclasses that
every layer passes around and mapping them would put `Mapped[...]` columns on
the domain layer to buy nothing. `Store` already hand-maps rows in `from_row`.

Up to v1.2 the schema was a `CREATE TABLE IF NOT EXISTS` script replayed at
every start, versioned by `PRAGMA user_version`. It had **no ALTER path**:
adding a column to `SCHEMA` was a silent no-op against any existing database,
and the version guard could not fire because the version had not changed. That
hazard is why the `jobs` table was created empty two phases before anything
wrote to it. Alembic exists here to close it, and `render_as_batch` is the
setting that does the work — SQLite cannot express most ALTERs, so Alembic
copies the table, moves the rows, and swaps the names.

The daemon runs `alembic upgrade head` while constructing a `Store`, on the
connection it already holds. A developer runs the CLI from the repo root:

```bash
uv run alembic revision --autogenerate -m "add a column"
uv run alembic check          # fails if schema.py and versions/ disagree
```

`tests/test_migrations.py` runs that same comparison in CI, plus a test that
the comparison is not vacuous. A v1.2 database is **stamped** at the baseline
revision rather than rebuilt: legacy files have exactly one possible shape,
stamping is therefore truthful, and it keeps the live pane-to-participant
mapping across the upgrade instead of making the daemon forget every running
pane.

### Retention (v2.1)

Four of those tables grow with use and none of them shrank. Measured on a real
machine over 4.26 days: `bus` was **94.2%** of a 32 MB file, growing 7.1 MB/day.
Jobs were 3.4%, participants 0.16%, touch 0.06%. The bus is the fire; everything
else is rounding. So retention is age-based and on by default (`[retention]`,
swept hourly by `_gc_loop`), because a database that is only bounded for users
who found the setting is not bounded.

The spans differ because the value of a row does. Bus events are a feed the
régie reads forward through a cursor, and nothing reads a week-old one — 7 days.
Finished jobs and their `touch` rows are what `recall` reaches back through —
15 days, past which the code has moved, the branches are merged and the harness
transcript is usually gone from disk anyway. `send.refused` is exempt from the
age sweep and capped by count instead: it is the only record that a send was
refused, and at ~3/day the cap is a century of headroom.

Three things make it safe:

- **Jobs are filtered on `finished_at`, never `created_at`.** A running job has
  `finished_at = NULL`, and `NULL < x` is never true in SQL, so no sweep can
  delete a job a caller is still awaiting.
- **The send-sequence counter lives in `meta`.** It used to be re-seeded from
  `MAX(jobs)` at every start; once old jobs can be deleted that regresses the
  counter and re-mints handles the pruned jobs already used, silently corrupting
  recall. Persisting it is the prerequisite that made job deletion shippable.
- **Deletes are batched** (`batch = 5000`). The store is synchronous on the
  event loop — see above, and this is where that stops being free. One
  unbatched `DELETE` of 32,217 rows blocks every status poll and every await
  for its duration; batched, the worst measured stall was 35 ms.

Dead participants are deleted only when nothing references them — no job as
target or caller, no surviving participant as parent. That gate means they
become collectable as a consequence of the job sweep rather than on a timer of
their own, and it is why `recall` never joins to a row that is gone.

`VACUUM` is not run in the background at any interval. It rewrites the whole
file under an exclusive lock, and an hourly lock over a growing file is a worse
problem than a large file; it is `theater gc --vacuum`, on purpose and by hand.
Ordinary deletion leaves the file the same size — SQLite reuses the freed pages
— so both the CLI and this paragraph say so, because the alternative is a user
concluding GC is broken.

The bus is an activity feed, not an archive. Event text is clipped at
`MAX_TEXT = 2000` chars, because a single tool result is routinely 25 KB and
keeping it whole would put megabytes of file contents into SQLite for something
the TUI renders as one line. What the harness itself wrote stays the full record
— which is exactly what `read_transcript` reaches for, through the same `Source`
the observer uses, when a caller needs the untruncated text.

## 6. Observation

The observer (`theater/daemon/observation/service.py`, ~790 lines) tails
the transcript files the harnesses already write.
`theater/daemon/observer.py` is a compatibility facade that re-exports the
observation package.

**Why not have agents self-report?** Two reasons, and the second is decisive:

1. An agent mid-tool-call makes no MCP calls. That is precisely the window in
   which you want to know it is alive and working.
2. Adopted sessions that predate Theater would be invisible. The whole promise
   of adoption is that you can point Theater at a session already running.

The observer therefore never asks. It reads.

### Two jobs, one seam

Watching a session is two jobs that look like one:

```
job 1: get the text        job 2: decide what it means
────────────────────────   ──────────────────────────────
find the file              IDLE / WORKING transitions
open it, tail it           settling before IDLE is believed
follow a rotation          60s job rescue
skip to EOF on attach      dead detection, awaiting-input
```

Job 1 is harness-shaped. Vibe rotates its session directory mid-turn; Claude
appends to one file; opencode writes no transcript at all and keeps every
session in one shared SQLite database, where there is no byte offset to hold
onto. Job 2 is harness-agnostic policy, and it is where every observation bug in
this project has been.

So job 1 is a replaceable seam and job 2 is not. Job 1 belongs to a
`HarnessObserver` (`theater/harness/contracts/observation.py`, re-exported by
the `harness/observation.py` facade) which every harness carries
as `harness.observer`, and which the reducer holds *instead of* the harness — so
the launch path and the observe path share no object. `observer.open_source()`
returns a `Source` (`theater/harness/contracts/source.py`, re-exported by
`harness/source.py`); the default, inherited from
`TranscriptObserver`, is `TranscriptSource`, the file tailing that used to live
inline in the observer. An observer that replaces it returns `Batch(events,
progressed, status, attached, waiting)` from `read()` and the reducer's policy
runs unchanged on top. `opencode.py` is the one shipped adapter that does, and
its source keys off `event.seq` in the database instead of a file position.

Three fields in that contract are load-bearing:

- `progressed` is not "produced events". A bookkeeping record advances the file
  with zero events; if that read as silence the 60s rescue would fire
  mid-turn.
- `status` lets a source that knows the agent's real state say so, instead of
  having it inferred from the last event. That is the channel for a harness
  with an authoritative status column.
- `waiting=True` means "nothing to read from yet" — no session file, no row.
  The observer sleeps and runs no timers, because a quiet timer against a
  source that has never spoken measures nothing.

A source has one more job, and it is not the observer's: `history(last_n)`
returns the session from the beginning, unclipped, and is what `read_transcript`
calls. `read()` is a tail and cannot answer that question — by the time an agent
asks for the full text of a reply, the batch carrying it is long gone. The
default implementation re-reads the file with clipping off; a database source
writes its own query. A custom source that skips it does not error, it just
returns nothing, which is the one feature a source can silently lose.

**Why not let a plugin bring its own observer?** Because job 2 would then be
written once per harness, and the settling logic, the rescue and the
relocation timers are exactly the code that took a dozen bug fixes to get
right. The seam is deliberately placed below the policy, not around it.

### Attaching

The observer always attaches at **EOF** and records how many records it
skipped. A session that has been running for an hour before adoption does not
replay an hour of history onto the bus. `skipped_records` appears in the
`agent.transcript` bus event so the gap is explicit rather than silent.

Spawned sessions get launch-local `SessionStart` and `PreCompact` hooks
that call `theater transcript-receipt` (the generic entry point) with a
private token file. The token is valid for the lifetime of the live
participant, not for a wall-clock TTL; death and GC delete the token and
token file. `SessionStart` covers cold starts and post-compaction
locations, while `PreCompact` records the old transcript before the
harness rotates. Live Claude sessions shipped in v3.2.0 have
`settings.json` files on disk that invoke `theater claude-receipt` by that
exact name; the old command name and `claude.receipt` RPC are kept as
forwarding aliases so those sessions keep working.

Vibe cold spawns get a Theater-owned isolated transcript save directory with a
signed marker naming the original participant. Resumes may re-enter that domain
only through a trusted dead predecessor row whose session lineage matches the
marker. This keeps repeat Vibe resumes out of the user's shared history without
allowing an unrelated trusted row to claim the marker.

Harness-specific resume validation lives behind the
`Harness.resume_launch_overlay` hook. Core selects the trusted dead
predecessor and pre-filters the trusted matching set; the hook decides
whether the predecessor's transcript domain is safe to reuse and returns
the env/domain overrides core merges into the launch plan. The base
implementation is conditionally fail-closed: a domainless predecessor
returns an empty overlay, while a predecessor with a domain is refused
unless the plugin implements the hook.

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

### Three independent quiet timers

```
RELOCATE_TIMEOUT      = 5.0    # Vibe rotates its session dir per turn
AWAITING_INPUT_TIMEOUT = 10.0  # quiet long enough to be a prompt, not a pause
RESCUE_TIMEOUT         = 60.0  # quiet long enough that a turn end was missed
POLL_INTERVAL          = 0.25
SEARCH_INTERVAL        = 2.0
SYNC_INTERVAL          = 1.0
```

The first two were one timer in v1. Sharing them made `AWAITING_INPUT`
unreachable: relocation fired first, every time. They measure different things
and must stay separate — this is a scar, not a preference.

The third is the same scar seen coming. A rescue reading the screen check's
clock would never fire at all, because that check throttles itself by pushing
its own clock forward every time it runs. Its job: after a minute of silence
over a screen that looks idle, finish any job still running against the
participant, with the last thing it said as the result and `error_code =
"turn_end_unseen"`. That covers a turn boundary the parser never saw — an
aborted turn, a badly timed rotation — which otherwise leaves the agent that
sent the prompt waiting on a promise nothing can resolve.

It stays `DONE` rather than failing, because the caller has a usable answer and
failing the job would block it on the very thing being rescued. Narrow on
purpose: no pane means no rescue, and an unreadable capture decides nothing.
Sixty seconds because firing early hands out a half-written answer, which is
worse than a slow one.

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
the participant is marked dead immediately rather than left as a
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

A multi-handle await blocks until **any** requested handle reaches a terminal
state, not until all of them do. If any handle is already terminal when the
call arrives, it returns immediately. The reply carries one current-state entry
per requested handle, so the caller processes the terminal entries and re-awaits
the still-running ones. This lets a caller fan out and react to the first
handle to become terminal without waiting on the slowest.

Job states are `running`, `done`, `crashed`, `killed`. `timeout` is
deliberately **not** a state: it is what `await` returns when the caller stops
waiting, not something that happens to the job. A job still running at the
ceiling comes back as `running` and the caller decides whether to re-await.

The agent-facing reply drops `prompt` and `result` from each entry. The prompt
is what the caller already sent, and `result` was only ever a 2000-char clip of
the child's turn; an agent that wants what the child said or did reads the
transcript directly via `read_transcript`, which returns it whole.

The in-memory events do not survive a daemon restart, and that is correct — a
restarted daemon has no observer attached yet, so an in-flight await would have
to re-poll regardless. `await_jobs` recreates a missing event rather than
failing.

---

## 8. Safety rails

In `theater/daemon/rails.py`. The depth and budget rails are checked before a
spawn; the two cycle rails before an await.

| Rail | Default | Behaviour |
|---|---|---|
| depth cap | `DEFAULT_DEPTH_CAP = 3` | reject spawns deeper than 3 levels |
| lineage cycle | — | reject if the target is an ancestor of the caller |
| wait cycle | — | reject if the target is already blocked on the caller |
| tree budget | `DEFAULT_BUDGET = 20` | reject the next spawn once the tree hits 20 participants |
| await ceiling | `MAX_AWAIT = 300s` | clamp `max_wait`, whatever the caller asks for |

The lineage check works because **the spawn tree was the await tree**. A child
awaiting its own ancestor is a deadlock by construction, and it is cheap to
refuse. `send` broke that equivalence — any participant can now prompt any
other — so it is an approximation, not a proof: it catches a descendant about
to block on an ancestor whose own await has not started yet, and misses two
peers entirely.

The wait check closes that gap by reading the awaits actually in flight
(`JobManager.wait_graph`, in memory, an edge per blocked call). Adding
caller -> target is refused when target can already reach caller. Two peers
awaiting each other share no ancestry, so this is the only rail that sees
them.

Both need `caller_id`, which `theater_await_sessions` passes. Until v1.5 it
did not, and both were unreachable from MCP — the guard existed and never ran.

The budget rail **rejects the next spawn and nothing else**. It does not kill
anything already running. An earlier `hard_stop_tree` was deleted in v1.1
because it killed nothing — it walked the tree and did no work. Note that
`docs/implementation_plan.md:374` still describes the old intent; that file is
a historical planning record and has been left unedited.

---

## 9. Harness abstraction

Two objects since v1.6. `theater/harness/contracts/harness.py` (re-exported by
the `harness/base.py` facade) defines `Harness`, which knows how to *start* a
CLI; `theater/harness/contracts/observation.py` (re-exported by
`harness/observation.py`) defines `HarnessObserver`, which knows how to *watch*
one. A harness constructs its observer and carries it as `harness.observer`.

| Method | On | Answers |
|---|---|---|
| `plan_launch` | harness | what argv, env, and config files start this thing |
| `find_transcript` | observer | where does it write its transcript |
| `session_id` | observer | what does it call this session |
| `parse` | observer | turn one transcript line into `Event`s |
| `native_children` | observer | does it spawn its own subagents we should show |
| `is_idle_screen` | observer | does this rendered screen mean "waiting for a human" |
| `open_source` | observer | where to read from, when it is not a transcript file |

The three transcript methods are abstract on `TranscriptObserver` and absent
from `HarnessObserver`, which is the point of the split. `OpenCodeHarness` used
to implement all four observing methods purely to return nothing, because its
output is a shared SQLite database and none of those questions has an answer for
it — four stubs to say "not applicable" meant the interface was describing one
particular way of observing rather than observation itself.

Every adapter is a plugin file, loaded by `harness/plugins.py` under one
contract. The four that ship — `claude`, `codex`, `opencode`, `vibe` — live in
`builtin/plugins/` and are read by the same scanner as anything in
`$THEATER_HOME/harnesses/`. There is no built-in tier. The only asymmetry is
what happens when one will not import: a shipped plugin failing is fatal (the
install is broken and hiding it makes the bug report unreadable), a local one
is skipped with a warning and listed by `theater harnesses`.

That is a v1.4 decision, and the reason is that a plugin was previously the
second of two mechanisms — the other being a TOML block that could describe a
harness without a parser. A config schema can only express the shallow half of
an adapter, so the deep half was untested-by-construction: nothing that shipped
used the extension point. Now the shipped adapters exercise it on every run.

The three are genuinely different, which is the interface's real test. Claude
Code writes one JSONL per project directory. Vibe rotates its session directory
per turn, which is the entire reason `RELOCATE_TIMEOUT` exists. Codex writes
date-sharded rollout files and marks a turn end with an explicit
`task_complete` — or `turn_aborted`, which counts too, since an abandoned turn
still has to release a waiting caller.

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
and the two never drift on how a tier or status is spelled. The lineage rails
(`├── │`) are the one thing the régie draws that the CLI does not: they need
sibling and ancestor position, which the shared depth-only walk cannot express,
so `regie/render/routing.py` keeps its own traversal (re-exported by the
`regie/tree.py` facade).

Two tmux courtesies belong to the régie rather than the daemon, because they
are properties of *being on screen*: it enables the session's `mouse` option
for as long as it runs, and on exit it unstages, so a staged agent never ends
up sharing a window with a dead TUI. Both are restored in `action_quit`, not
`on_unmount` — by unmount the event loop is closing and an awaited tmux call
can be cancelled halfway.

`regie/palette.py` adds a `Spawn <harness>` entry per registered harness to
Textual's ctrl+p palette. It goes through the same `spawn` RPC as the CLI, with
no prompt and no parent, so the régie gains no privileged path to the daemon.

---

## 11. Module map

```
theater/
├── cli/                __init__.py 133 (entry point + main) · parser.py 287 · render.py 117
│                       errors.py · commands/ (bus, identity, introspection, maintenance,
│                       participants, process)
├── config/             load.py 95 · models.py 148 · validation.py · describe.py
├── constants/          __init__.py + cli, core, daemon, harness, limits, observation,
│                       regie, tmux, worktree
├── models.py 325       Tier, Status, Participant, Job, error codes
├── client.py 234       DaemonClient, autostarts the daemon
├── protocol.py 114     NDJSON framing, PROTOCOL_VERSION = 1
├── paths.py 71         $THEATER_HOME layout
├── formatting.py 158   shared CLI/régie rendering, no rich/textual
├── proc.py 222         process facts from ps / proc / lsof
├── daemon/
│   ├── observation/    service.py 788 (watch loop, observation orchestration root)
│   │   ├── reducer.py  398 — QuietClock, three quiet timers, status policy
│   │   ├── identity.py 209 · completion.py 186 · failures.py 230
│   │   ├── screen.py 47 · turns.py 128 · attachment.py 349
│   ├── persistence/    store.py 410 (SQLite over SQLAlchemy Core, sync on purpose)
│   │   ├── database.py 84 · repositories/ (participants, jobs, bus, metadata,
│   │   │   receipts, scratchpad, statistics, usage, worktrees)
│   ├── rpc/            33 @method handlers across admin, jobs, participants, recall,
│   │                   scratchpad, sending, spawning, transcripts, usage
│   ├── runtime/        lifecycle.py 193 · socket.py 111 · maintenance.py 135
│   ├── spawning/       service.py 443 · planning.py 150 · resume.py 148 · models.py 56
│   ├── worktrees/      repository.py 133 · unique.py 233 · named.py 289 · paths.py 99
│   ├── observer.py 141       compatibility facade re-exporting observation/
│   ├── store.py 16          compatibility facade re-exporting persistence/
│   ├── methods.py 101       compatibility facade re-exporting rpc/
│   ├── server.py 205        lifecycle only: socket, pidfile, wiring
│   ├── spawner.py 25        compatibility facade re-exporting spawning/
│   ├── worktree.py 69       compatibility facade re-exporting worktrees/
│   ├── registry.py 391     tier assignment, pane eviction, lineage
│   ├── jobs.py 438          JobManager, asyncio.Event per handle
│   ├── gc.py 484            the retention sweep: bus, jobs+touch, participants
│   ├── rails.py 258         depth / cycle / budget
│   ├── recall.py 392 / recall_read.py 495  path-touch history + segment reader (v2)
│   ├── harness_detect.py 223 · lineage.py 73 · lock.py 206 · blob.py 48
│   ├── schema.py 178  table metadata, the one place columns are declared
│   └── migrations/     alembic env + versions/
├── harness/
│   ├── contracts/      harness.py 166 · source.py 285 · observation.py 332
│   │                   launch.py 86 · events.py 133
│   ├── registry/       lookup.py 108 · install.py 116 · capabilities.py 90 · claims.py 163
│   ├── transcript/     source.py 595 (file-tailing, the observer's job-1 seam)
│   │                   observer.py 249 · attachment.py 52
│   ├── builtin/plugins/  opencode.py 706 (a database, not a file)
│   │                      codex.py 414 · claude.py 367 · vibe.py 284
│   ├── base.py 63          compatibility facade re-exporting contracts
│   ├── observation.py 38   compatibility facade re-exporting contracts + transcript
│   ├── source.py 43        compatibility facade re-exporting contracts + transcript
│   └── plugins.py 183     the plugin loader
├── mcp/      server.py 526 (14 agent tools, composition surface) · session.py 53
│   ├── toolsets/       delegation.py 310 · participants.py 78 · recall.py 59 · transcripts.py 34
│   └── tools.py 42     compatibility facade re-exporting toolsets + session
├── tmux/     client.py 86 · command.py 121 · panes.py 311 · presence.py 53
│            delivery.py 60 · facts.py 123 · options.py 90
└── regie/    app.py 1203 (Textual app, composition surface) · palette.py 257 · bus_view.py 84
    ├── controllers/    session.py 234 · navigation.py 124 · polling.py 203
    │                   staging.py 242 · animation.py 356 · usage.py 212
    ├── render/        layout.py 211 · glyphs.py 262 · routing.py 300
    ├── widgets/       chrome.py 36 · leaf.py 186 · tree.py 268
    │                   usage_breakdown.py 185 · usage_footer.py 408
    └── tree.py 121    compatibility facade re-exporting render modules
```

Roughly 31,500 lines, 77 test modules (~39,500 test lines).

The modular refactor decomposed the daemon's monolithic observer, methods,
store, and server into packages (`observation/`, `rpc/`, `persistence/`,
`runtime/`, `spawning/`, `worktrees/`), split the harness contracts and
transcript into sub-packages, moved MCP tool bodies into `toolsets/`, and broke
the régie into `controllers/`, `render/`, and `widgets/`. The old module paths
survive as compatibility facades that re-export the new packages, so existing
imports continue to work unchanged.

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
- **Codex's first run in a directory is a trust dialog.** It waits on a
  keypress no transcript records, so a spawn there reads as WORKING until a
  human answers it. Run `codex` by hand once per directory. Detecting the
  dialog would mean matching its rendered text, which is the fragile thing
  `is_idle_screen` is already deliberately conservative about.

`docs/v2_ideas.md` covers where this goes next.
