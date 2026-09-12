# Theater — architecture

This document explains *why* Theater is shaped the way it is. The README covers
what the commands do; this covers the constraints that produced them, and the
alternatives that were rejected along the way.

This document tracks the current state of the code, and where the two
disagree, the code wins.

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
            │  NDJSON over unix socket ($THEATER_HOME/var/run/daemon.sock)
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
(`DEFAULT_INHERITED_ENV_VARS` in the SDK's `mcp/client/stdio.py`). Environment
variables set on the tmux
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

Participant names are deliberately live-only, recyclable aliases: they make
current interaction readable without becoming historical identifiers.
Descriptions are different metadata. They are bounded, persisted with the
participant row, retained after death for résumé discovery, and inherited by a
resumed successor unless the caller explicitly replaces or clears them.

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

Pane identity is scoped to a tmux server epoch. The daemon persists tmux's
server identity beside each pane-backed participant and reconciles it together
with a non-empty pane inventory. A changed identity ends participants from the
previous epoch as `tmux_restart` without retiring their worktrees, crashes their
active jobs as `tmux_restarted`, and records one shared incident. Empty or failed
inventories remain inconclusive. The retained incident metadata lets the
`theater-recover-tmux` skill plan explicit, conservative resume attempts.

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

Requests may carry an optional top-level `_meta` object for W3C trace-context
propagation (see [§13](#13-observability)). This is an additive extension: old
daemons already ignore unknown top-level keys, new daemons accept requests
without `_meta`, and no protocol version bump is needed. Trace metadata lives
in `_meta`, never inside `params` — handlers must not see or reject it.
Receivers ignore unknown or malformed `_meta` keys.

The daemon exposes 45 methods (`theater/daemon/rpc/`); the MCP tools
number 22 (`theater/mcp/server.py`), registered under bare
names — namespacing is by server name, not a tool prefix. Agents in fact see
two servers: `theater` carries everything except `await_sessions`, which lives
on a separate `theater_wait` stdio server so a cancelled long wait can never
delay a control call. The two sets are not the same and should not be:
`shutdown`, `adopt`, and `bus.tail` are operator verbs, not agent verbs.

Skills use the same boundary. `skills.list` and `skills.load` discover and
validate packages in the daemon; the MCP tools only forward. Listing returns
bounded metadata and diagnostics without instruction bodies. Loading accepts
one canonical name and returns that validated `SKILL.md` verbatim. This keeps
filesystem access out of per-agent MCP processes and prevents broad skill
discovery from consuming agent context.

Packages live at `theater/skills/builtin/<name>/SKILL.md` or
`$THEATER_HOME/skills/<name>/SKILL.md`. A package contains no executable code or
auxiliary files. Enabled MCP-server manifests may explicitly register packages
at `skills/<name>/SKILL.md` inside their own directory. Those files are loaded
once with the plugin and merged into the same global namespace. Bundled and
user names remain authoritative; conflicting plugin registrations are rejected
without disabling their sidecars. Invalid declared plugin skill packages make
that plugin invalid.

Built-ins can be refused individually through the `[skills] disabled` list in
`config.toml` — a built-in-only denylist. User and plugin skills are
unaffected; every built-in is validated *before* the denylist filters, so a
broken Theater installation stays fatal rather than being hidden by the
setting; names Theater does not bundle are tolerated. The daemon threads the
list into `skills.list` and `skills.load` alike, so a disabled built-in is
neither listed nor loadable.

---

## 5. State

One SQLite file, `$THEATER_HOME/var/state/theater.db`, owned exclusively by the daemon —
every other process reaches it over the unix socket.

The home root contains the user-managed `config.toml`, `plugins/`, and `skills/` entries.
Theater-managed state, runtime files, logs, and participant data live under `var/`. Each
participant owns one `var/participants/<id>/` subtree, while plugin-global state lives under
`var/state/plugins/<name>/`.

### Clean-break home migration

The home-layout change does not migrate runtime state:

1. Stop Theater, including MCP sidecars and the daemon.
2. Stage `config.toml` and the complete `skills/` tree outside `$THEATER_HOME`.
3. Archive or discard the old home, then create or reinstall the fresh layout.
4. Restore `config.toml` and `skills/`, removing obsolete plugin `state_path` settings.
5. Reinstall or relink user plugins under `$THEATER_HOME/plugins/`, then start Theater.

The database, participants, transcripts, logs, usage history, and plugin state start empty. An
archived database is for reference only and must not be restored into the new home.

| Table | Holds |
|---|---|
| `participants` | the registry: id, harness, tier, pane, cwd, branch, parent, status |
| `jobs` | one row per unit of delegated work, keyed by handle |
| `bus` | append-only activity feed |
| `touch` | which paths each job changed, and the shas it moved them between |
| `meta` | small key/value durable state — currently the send-sequence counter |
| `budgets` | per-tree accounting — created, not yet used |
| `tree_kv` | sibling scratchpad: small coordination facts scoped to a spawn tree and repo |
| `participant_artifacts` | validated participant-owned files, recorded for GC |
| `participant_mcp_plugins` | one row per participant-scoped plugin sidecar: credential verifier, grants, path |
| `named_worktrees` | named shared worktrees Theater created, keyed by repo and name |
| `usage` | per-participant model usage samples, for `theater stats` and cost estimation |
| `participant_runtime_bindings` | native wiring per participant: wiring, backend generation, lifecycle phase, endpoint, verified pid + start identity, native session id, protocol facts |
| `control_operations` | durable control state: operation id, kind, transport, delivery phase, native turn identity, queue position |
| `native_terminal_evidence` | exact terminal evidence keyed by generation/session/turn — finishes a job exactly once |

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
- **Abandoned running jobs are reaped separately.** A job whose daemon died
  keeps `finished_at = NULL` forever; `stale_running_days` (7) marks such a
  row `crashed` with `error_code = "abandoned"`, so immortality has an
  explicit backstop instead of being the default.
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

Generated launch files and observation trees follow the participant retention
gate. Their validated Theater-owned paths are recorded before launch files are
written, then removed off the event loop when the participant row becomes
collectable. Failed removals retain their ownership rows for the next sweep;
legacy artifacts are discovered only inside dedicated Theater roots and are
rechecked against current participant rows before deletion. Receipt and native
channel secrets are stricter: death removes them immediately, with GC retaining
the crash-recovery cleanup path.

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

The observer (`theater/daemon/observation/service.py`) tails
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
the launch path and the observe path share no object. The reducer passes a frozen
`ParticipantObservationContext` to `observer.open_source_context()`. Shipped adapters consume that
typed context directly; the dispatcher still adapts it to `open_source_for()` or `open_source()`
for existing third-party plugins. `observer.open_source()`
returns a `Source` (`theater/harness/contracts/source.py`, re-exported by
`harness/source.py`); the default, inherited from
`TranscriptObserver`, is `TranscriptSource`, the file tailing that used to live
inline in the observer. An observer that replaces it returns `Batch(events,
progressed, status, attached, waiting)` from `read()` and the reducer's policy
runs unchanged on top. The OpenCode adapter is the one shipped adapter that does, and
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

A source has one more job, and it is not the observer's: `history_page` reads a
bounded history window without advancing the live cursor, and is what
`read_transcript` calls. `read()` is a tail and cannot answer that question — by
the time an agent asks for older text, the batch carrying it is long gone. File
and database sources page from their own storage cursors; the generic pager
owns the MCP response budget and continuation chunks. The legacy
`history(last_n)` projection remains for internal consumers, but is not the
agent-facing transcript surface.

**Why not let a plugin bring its own observer?** Because job 2 would then be
written once per harness, and the settling logic, the rescue and the
relocation timers are exactly the code that took a dozen bug fixes to get
right. The seam is deliberately placed below the policy, not around it.

### Attaching

The observer always attaches at **EOF** and records how many records it
skipped. A session that has been running for an hour before adoption does not
replay an hour of history onto the bus. `skipped_records` appears in the
`agent.transcript` bus event so the gap is explicit rather than silent.

Spawned Claude sessions get launch-local `SessionStart` and `PreCompact` hooks
that call `theater transcript-receipt` (the generic entry point) with a
private token file. The token is valid for the lifetime of the live
participant, not for a wall-clock TTL; death and GC delete the token and
token file. `SessionStart` covers cold starts and post-compaction
locations, while `PreCompact` records the old transcript before the
harness rotates. Live Claude sessions shipped in v3.2.0 have
`settings.json` files on disk that invoke `theater claude-receipt` by that
exact name; the old CLI command remains as a payload-transparent alias of
`transcript-receipt` so those sessions keep working. Both command names use
the harness-neutral `transcript.receipt` RPC, and only the plugin interprets
the native payload.

Spawned OpenCode sessions use the same authenticated receipt transport. Their launch-local plugin
publishes each root `session.created` ID, retries transient delivery failures, and retries again on
later session events. The database source waits for the matching row before committing
`opencode://<session-id>`. A later root receipt stages a session switch without falling back to
another same-cwd row. The plugin also records a bounded, atomically replaced catalog of MCP tool
identities proved by OpenCode's merged configuration and execution hooks. The database source uses
that catalog only to classify its own durable records; it is metadata, not a second fact stream or
the generic hook channel.

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

### Status and presence — independent signals and failure policies

This is the subtlest part of the system and the source of most v1 bugs.

| Signal | Drives | Failure policy | Source |
|---|---|---|---|
| transcript growth | `IDLE` / `WORKING` | source of truth, no heuristic | disk file |
| `capture-pane` screen | `AWAITING_INPUT` | accept false negatives | rendered screen |
| `pane_in_mode` | blocks legacy key injection | preserve copy mode; query failures refuse delivery | tmux fact |
| terminal focus + selected input pane | mutation guards, `await` holds, presence UI | UNKNOWN protects like PRESENT | presence provider |

Their failure policies differ because the cost of being wrong differs per consumer:

- A wrong `AWAITING_INPUT` misleads a human reading the régie for a fraction of
  a second, until the next transcript growth corrects it.
- A wrong "no human present" injects keystrokes into a pane a human is using.
  Presence is therefore fail-closed: an input-capable attached client's focused
  terminal and selected input pane protect the participant. Mouse position,
  visible cursors, and régie selection are not focus signals. Pane changes,
  terminal blur (including Alt-Tab), and detach release protection. Copy mode
  separately blocks unsafe legacy key injection without affecting native controls
  or presence-aware awaits. Missing or errored facts report UNKNOWN and protect.
  Consumers refresh at admission and wait on revisions, because presence is
  reported asynchronously and a stale absence is not a fresh one.

An earlier version scraped the pane's input buffer to detect a human typing. It
was removed: it cannot distinguish agent output from unsubmitted human input,
and the last line of an agent pane is almost always non-empty text. It blocked
legitimate sends constantly. Copy mode now answers only whether legacy key injection
is safe; it is not evidence of human focus.

The daemon maintains server-wide `focus-events on`; terminal reporting must work,
and already attached clients may need reattachment. Any input-capable human client
protects its selected pane; read-only and control clients do not. Only a genuinely
independent `active-pane` client whose input pane cannot be observed protects the
whole displayed window. Ordinary pane selection protects only the selected pane.
A shared bounded monitor refreshes facts after hook wakes and periodically,
invalidating stale facts. A received hook immediately invalidates cached facts
and fences out an in-flight observation that began before that wake; only a new
read can restore absence, without discarding verified focus-transition history.
Hooks preserve existing user entries, and shutdown leaves focus events enabled.

The tmux layer owns OS facts and hook plumbing. `daemon/presence` separates its
shared contract, pure classification, monitor lifecycle, and provider access.
Controls refresh presence before reading native execution state, then recheck
cached protection without yielding before reservation; neither fact may become
stale while awaiting the other. Await coordination stays in `daemon/awaiting`,
and RPC/MCP/régie only project daemon-owned decisions.

Agents cannot mutate protected participants through CLI or MCP. Existing FIFO
followups pause until protection releases and normal execution guards also permit
dispatch. Entering a pane does not interrupt work already in progress. Focus reports
are asynchronous, so an already transmitted request cannot be retracted atomically.

`jobs.await` decides presence gating exactly once, at admission: the first snapshot
after the admission refresh. A target protected at admission (present or unknown —
fail-closed) is gated: it waits for both an observed unprotected snapshot and a
terminal job state, in either order. The observed departure clears the gate
permanently for that await; later re-entry does not restore it. `await_reason` names
the condition observed last — `presence_released` when departure was last,
`job_terminal` when completion was last — and `job_terminal` wins when both first
become visible in the same evaluation. A target unprotected at admission is never
gated: human presence arriving later is irrelevant, terminal state alone qualifies it,
and a later departure does not release a still-running job. An existing job handle
wins resolution; a registered id without a job waits only for presence, returning
`already_absent` immediately when unprotected at admission, without creating a job
or returning job-only fields. Wait-any keeps input order, marks other entries
`pending`, and uses one overall deadline (150 seconds default, 300 maximum).
`timeout` grants no permission to mutate. Régie displays presence beside activity:
`◉` means present and `◌` means unknown; both protect the participant.

### Three independent quiet timers

```
RELOCATE_TIMEOUT      = 5.0    # Vibe rotates its session dir per turn
AWAITING_INPUT_TIMEOUT = 1.5   # no transcript growth before checking the screen
RESCUE_TIMEOUT         = 60.0  # quiet long enough that a turn end was missed
SCREEN_INTERVAL        = 1.0   # no-transcript harnesses: the screen is the only turn-end evidence
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
four gates:

```
addressable?  →  no  →  not_addressable
human at the pane?  →  yes  →  human_present
participant working?  →  yes  →  busy
already running a send?  →  yes  →  busy
```

A direct parent may interrupt, wait for `IDLE`, and retry `send`.

Then the job is created with handle `<target_id>#<seq>`.

### Completion

The caller does not get a callback. It gets a handle, and calls
`await_sessions(handles, max_wait=150)`.

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
the child's turn; an agent that wants what the child said or did reads bounded
transcript pages via `read_transcript`, continuing only with Theater's returned
cursor when needed.

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

Both need `caller_id`, which `await_sessions` passes. Until v1.5 it
did not, and both were unreachable from MCP — the guard existed and never ran.

The budget rail **rejects the next spawn and nothing else**. It does not kill
anything already running. An earlier `hard_stop_tree` was deleted in v1.1
because it killed nothing — it walked the tree and did no work.

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
| `open_source_context` | observer | open from typed participant identity/location facts |

The three transcript methods are abstract on `TranscriptObserver` and absent
from `HarnessObserver`, which is the point of the split. `OpenCodeHarness` used
to implement all four observing methods purely to return nothing, because its
output is a shared SQLite database and none of those questions has an answer for
it — four stubs to say "not applicable" meant the interface was describing one
particular way of observing rather than observation itself.

Every adapter is a named package loaded under one manifest contract. The five
that ship — `claude`, `codex`, `opencode`, `pi`, `vibe` — live entirely under
`theater/harness/builtin/plugins/<harness>/`, each with `manifest.py` exporting
one immutable `MANIFEST`. Local packages use the same shape at
`$THEATER_HOME/plugins/<name>/manifest.py`; the directory name is canonical,
relative sibling imports are isolated, and a local package overrides a shipped
one. Disabled names are skipped before import. A legacy top-level local `.py`
is never executed and is retained only as an actionable migration diagnostic.
`harness/plugins.py` remains a generic compatibility facade for the loader.
There is no built-in tier. The only asymmetry is what happens when one will not
import: a shipped plugin failing is fatal (the install is broken and hiding it
makes the bug report unreadable), a local one is skipped with a warning and
listed by `theater harnesses`.

Manifests are immutable values with explicit typed callbacks; there is no
opaque callback bag or private built-in extension API. A primary transcript or
database source remains the durable authority for identity, completion, and
history. `CompositeSource` may add bounded hook or native-OTel trajectory
enrichment only when declared signal ownership and exact native-key correlation
make it safe. Optional channel failure is bounded health, never a reason to
break durable observation. Hooks use authenticated ingress and launch-local
installation only; native OTel uses the separate loopback/authenticated inbound
channel at `harness/channels/otel/`. Neither may rewrite global hooks or take
over a user's exporter.

Interactive control also stays manifest-driven. `ControlManifest` declares a
bounded `InterruptPlan`; the daemon owns lineage, status, pane-identity, and
human-presence checks, while each plugin owns only the native key sequence its
TUI understands. Interruption never sets participant status directly—the
observer remains authoritative.

This inbound harness OTel channel is distinct from `theater/observability/`,
which exports Theater's own daemon, CLI, and régie telemetry. All five shipped
plugins declare richer hooks and native OTel explicitly but currently mark them
unavailable; their durable transcript/database sources remain authoritative
until safety and live-evidence gates pass.

That is a v1.4 decision, and the reason is that a plugin was previously the
second of two mechanisms — the other being a TOML block that could describe a
harness without a parser. A config schema can only express the shallow half of
an adapter, so the deep half was untested-by-construction: nothing that shipped
used the extension point. Now the shipped adapters exercise it on every run.

The five are genuinely different, which is the interface's real test. Claude
Code writes one JSONL per project directory. Vibe rotates its session directory
per turn, which is the entire reason `RELOCATE_TIMEOUT` exists. Codex writes
date-sharded rollout files and marks a turn end with an explicit
`task_complete` — or `turn_aborted`, which counts too, since an abandoned turn
still has to release a waiting caller. opencode keeps its session in a SQLite
database rather than transcript files. Pi writes JSONL sessions and rotates
them deliberately on `/new`.

`parse` takes `clip_text: bool` rather than always clipping, because the same
parser serves two consumers with opposite needs: the bus wants a one-line
summary, `read_transcript` wants the bytes as written. `clipper()` picks the
treatment once instead of each parser redefining it inline — a v1.1 fix that
uncovered a Claude Code path clipping unconditionally despite `clip_text=False`.

Runtime wiring is a separate, additive manifest surface. A harness may also
declare `HarnessManifest.runtime` — a `RuntimeManifest` with a read-only
compatibility probe, a per-participant runtime factory, and one declared live
channel. A detached host adds a pure backend planner; a frontend host adds a
passive extension overlay for the ordinary launch. The field is `None` for
every harness that predates it, and `None` preserves legacy behaviour exactly.
The Codex pilot is the first user; see [§14](#14-native-runtime-wiring-the-codex-pilot).

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

Trajectory is a logical right-hand surface, not another tmux pane. The shared
`theater/trajectory/` package defines bounded, harness-neutral records and wire
values. Harness plugins alone translate native transcript or database facts
into that contract. The daemon's `trajectory/` package then owns ingestion,
identity, causal links, pricing, aggregation, warm streams, and RPC responses.
Plugins expose native MCP calls as normalized `mcp_server` / `mcp_tool` facts;
daemon projection alone classifies calls to Theater's server into the Theater lane.

The régie's `trajectory/` package is presentation-only. Its controller owns
snapshot and follow clients; state owns the bounded participant window; the
Textual-free projection owns derived ordering, search, and pagination caches;
analysis and inspection build display models; render helpers and widgets draw
them. `ParticipantTrajectoryState.ledger_page` remains the canonical runtime
page index. Projection refresh writes its clamped index and selection back to
that state while retaining only the derived page. This keeps native parsing in
plugins, canonical policy in the daemon, and widget mutation at the UI edge.

---

## 11. Module map

```
theater/
├── cli/                entry point, parser, render, command modules
├── config/             loading, models, validation, describe
├── constants/          immutable values split by domain, including trajectory and régie
├── observability/      one package, one process-level lifecycle (see §13)
│   ├── catalog.py        immutable operation/attribute specs (frozen, slotted)
│   ├── engine.py         timing context, prose rendering, log extras, metric bridge
│   ├── metrics.py        metric specs, histogram/counter registries, cached gauges
│   ├── tracing.py        span lifecycle, explicit W3C inject/extract
│   ├── signals.py        direct structured-log and completed-span transport
│   ├── logging.py        owned handlers, rotation, stderr-generation pruning
│   └── runtime.py        process-level composition and RuntimeHandle shutdown
├── timing.py           compatibility facade — re-exports observability engine
├── trajectory/         bounded harness-neutral domain and wire values
│   ├── records.py        canonical records, details, links, usage, failures
│   ├── requests.py       request-level aggregation
│   ├── tools.py          paired tool-operation aggregation
│   └── causality.py · grouping.py · timing.py · overview.py · page.py
├── models.py           Tier, Status, Participant, Job, error codes
├── client.py           DaemonClient, autostarts the daemon
├── protocol.py         NDJSON framing, PROTOCOL_VERSION = 1
├── paths.py            $THEATER_HOME layout
├── formatting.py       shared CLI/régie rendering, no rich/textual
├── proc.py             process facts from ps / proc / lsof
├── names.py            live-only participant name aliases (recyclable masks)
├── provenance.py       transcript-provenance predicates (trusted vs untrusted)
├── transcript_identity.py  shared transcript identity and location canonicalisation
├── resume_floor.py     persisted pre-launch stream-position fact for resume
├── plugin_client.py    typed async client for participant-scoped plugin sidecars
├── plugins/            shared plugin catalog, loading, and diagnostics (both kinds)
├── mcp_plugins/        MCP-server plugin contracts, registry, runner, validation
├── pricing/            token cost estimation from usage records
├── daemon/
│   ├── observation/    watch loop, identity, completion, status policy
│   ├── persistence/    SQLite repositories and store (sync on purpose)
│   │   ├── database.py · repositories/ (participants, jobs, bus, metadata,
│   │   │   receipts, channels, scratchpad, statistics, usage, worktrees,
│   │   │   artifacts, mcp_plugins, runtime_bindings, control_operations,
│   │   │   native_evidence)
│   ├── rpc/            @method handlers, including trajectory snapshot/follow/close/locate/search
│   ├── runtime/        socket dispatch, lifecycle, maintenance loops
│   ├── controls/       durable control state machine: send, steer, queue, settings, interrupt
│   ├── harness_runtime/  WebSocket-over-Unix transport, detached backends, runtime manager
│   ├── spawning/       launch planning, resume, service
│   ├── worktrees/      repository, unique and named worktree implementations
│   ├── trajectory/     canonical projection, ingestion, cache, aggregation, responses
│   │   ├── runtime.py    composition facade over stream, panel, and mutations
│   │   ├── telemetry/    bounded agent logs, spans, metrics, and deduplication
│   │   └── history_ingest.py · live_ingest.py · bus_ingest.py
│   ├── observer.py · store.py · methods.py · spawner.py · worktree.py
│   │                   compatibility facades for established import paths
│   ├── server.py       lifecycle only: socket, pidfile, wiring
│   ├── registry.py     tier assignment, pane eviction, lineage
│   ├── jobs.py         JobManager, asyncio.Event per handle
│   ├── artifacts.py    validated participant-owned files and cleanup
│   ├── gc.py           retention sweep: bus, jobs+touch, participants, artifacts
│   ├── rails.py        depth, cycle, and budget guards
│   ├── recall.py · recall_read.py   path-touch history and segment reader
│   ├── schema.py       table metadata, the one place columns are declared
│   └── migrations/     alembic env + versions/
├── harness/
│   ├── contracts/      immutable manifests, typed callbacks, harness/source/observation facts
│   ├── manifests/      manifest compiler, validation, and reusable strategies
│   ├── loading/        named-directory discovery and isolated package imports
│   ├── channels/       CompositeSource, HybridSource, bounded hooks, and inbound native OTel
│   ├── normalization/  bounded cross-harness value and timestamp conversion
│   ├── registry/       lookup, install, capabilities, claims
│   ├── transcript/     source, observer, attachment, bounded history reader
│   ├── builtin/plugins/  claude/, codex/, opencode/, pi/, vibe/ package manifests and native code
│   ├── base.py · observation.py · source.py   compatibility facades
│   └── plugins.py      generic loader compatibility facade
├── skills/             declarative SKILL.md validation, discovery, immutable registry
│   └── builtin/        theater-configure · theater-debate · theater-orchestrate · theater-recover-tmux
├── mcp/                theater + theater_wait server composition, session, toolsets
├── tmux/               client, command, panes, buffers, presence, delivery, facts, options
└── regie/              Textual app composition, palette, bus view
    ├── controllers/    session, navigation, polling, staging, surface, animation, usage
    ├── animations/     reusable animation state and frame helpers
    ├── dashboard/      unstaged welcome content and widgets
    ├── render/         tree layout, glyphs, routing
    ├── widgets/        chrome, leaf, tree, usage breakdown and footer
    ├── trajectory/     participant trajectory presentation
    │   ├── controller.py · state.py · models.py · projection.py · messages.py ·
    │   │   view.py · search.py · navigation.py · enums.py
    │   ├── analysis/     cached diagnostic models
    │   ├── inspection/   bounded detail projection and links
    │   ├── render/       pure ordering, pagination, row, and timeline projection
    │   └── widgets/      Textual timeline, ledger, inspector, filters, and footer
    └── tree.py         compatibility facade re-exporting tree render modules
```

The modular refactor decomposed the daemon's monolithic observer, methods,
store, and server into packages (`observation/`, `rpc/`, `persistence/`,
`runtime/`, `spawning/`, `worktrees/`, `trajectory/`), split the harness
contracts, transcript mechanics, normalization, and built-in adapters into sub-packages, moved MCP
tool bodies into
`toolsets/`, and broke the régie into controllers, projections, render helpers,
and widgets. Compatibility facades remain only for established import paths.

---

## 12. Known gaps

- **`budgets` table is unused.** Token and cost accounting is written but never
  read; the budget rail counts participants instead.
- **tmux behaviour is tested only where tmux exists.** `tests/test_tmux_rig.py`
  drives a real private tmux server and asserts on exact pty bytes, but it is
  marked `tmux` and self-skips when tmux is absent — a sandbox without tmux
  silently loses that coverage (see AGENTS.md).
- **`AWAITING_INPUT` is a display hint.** Never let it gate a control decision.
- **Human presence is focus-derived and fail-closed.** Terminal focus and the
  selected input pane protect; mouse position does not. Unknown facts protect,
  while copy mode independently blocks unsafe legacy input. Reporting is asynchronous.
- **Codex's first run in a directory is a trust dialog.** It waits on a
  keypress no transcript records, so a spawn there reads as WORKING until a
  human answers it. Run `codex` by hand once per directory. Detecting the
  dialog would mean matching its rendered text, which is the fragile thing
  `is_idle_screen` is already deliberately conservative about.
- **The native-UI idle race is accepted, not solved.** Theater serializes its
  controls per participant and rejects known-busy targets, but a human typing
  in the native UI at the same instant can absorb a Theater send into that
  human-started turn. Theater records the actual returned turn and never
  binds two jobs to it; the race itself is a documented limitation of the
  native protocol — see §14.

`docs/v2_ideas.md` covers where this goes next.

---

## 13. Observability

Theater ships an opt-in observability stack: structured stdlib logging, optional
OpenTelemetry export (traces, metrics, logs), and W3C trace-context propagation
across the NDJSON transport. All of it lives in one package,
`theater/observability/`, with one process-level lifecycle.

### Package ownership and dependency rules

```
theater/observability/
├── __init__.py   small public API, no SDK import
├── catalog.py    immutable operation/attribute specifications (frozen, slotted)
├── engine.py     timing context, exact prose rendering, log extras, metric bridge
├── metrics.py    histogram registry, views, cached gauges, GaugeSampler
├── tracing.py    span lifecycle, explicit W3C inject/extract
├── signals.py    direct structured-log and completed-span transport
├── logging.py    owned handlers, crash capture, rotation, stderr pruning
└── runtime.py    process-level composition and RuntimeHandle shutdown
```

`theater/timing.py` is a compatibility facade that re-exports the engine and
preserves existing calls, so call sites that import `timing` continue to work
unchanged.

Dependency direction is strictly layered: `constants/observability.py` imports
no feature package; `catalog.py` imports only dataclasses, enums, and constants;
`metrics.py`, `tracing.py`, `signals.py`, and `engine.py` may import `catalog.py`;
`runtime.py` composes logging, metrics, tracing, and direct signals. Lower modules
never import `runtime`.
Domain objects (`Registry`, `JobManager`, repositories) never import OpenTelemetry.
Only setup helpers in `runtime.py` import SDK/exporter modules, and only after
configuration says export is enabled.

### Process logging roles

The daemon has two log files that must never share an inode:

- **`var/logs/daemon/daemon.log`** — the routine human-readable log. A `RotatingFileHandler`
  attaches directly to the `theater` logger with the existing formatter
  (`%(asctime)s %(levelname)-7s %(name)s %(message)s`). Rotation is **always
  active**, regardless of whether OTLP export is enabled — this is the fix for
  the 25 MB/9 h unbounded growth that motivated the whole design. Default 10 MB
  per file, 3 backups. `theater.propagate = False` prevents duplicate root
  output. A direct foreground daemon (`theater daemon`) also attaches a stderr
  handler with the same formatter; an autostarted daemon does not mirror routine
  logs into raw stderr.
- **`var/logs/daemon/stderr/<token>.log`** — raw crash output. When `DaemonClient`
  autostarts a daemon, the parent generates 12 lowercase hex chars with
  `secrets.token_hex(6)`, creates a mode-0600 file in that directory, and
  passes the same open fd as the child's stdout and stderr. The child does not
  reopen or redirect stderr — it inherited the correct descriptor. Token
  exists only for safe cleanup, pruning, and error reporting.

A `RotatingFileHandler` renames its file on rollover; an inherited raw fd would
continue writing to the renamed inode, which is why the two must never be the
same file.

Generation files are pruned by mtime immediately after the winner acquires the
lock, before config and handler setup. Retention count includes the current
generation; the current path is pinned regardless of mtime. Only `LockHeld`
(the singleton race loser) deletes its own generation — every other failure
keeps it, whether before or after lock acquisition.

Each régie writes a rotating `var/logs/regie/pane-<id>.log`, with a
`var/logs/regie/pid-<pid>.log` fallback when no pane identity is available. Per-pane
files prevent two régie processes from rotating the same inode. Startup keeps
the current and live-pane generations, plus a small bounded number of newest
inactive generations; each base file and its `.1`, `.2`, … backups count as one
generation. Its event-loop lag monitor and unhandled Textual exceptions use the
same shared logging and OTel pipeline.
MCP attaches only the OTel `LoggingHandler`; stdout remains protocol-only and
there is no local MCP log file.

`logging.basicConfig` was removed from the daemon start path; `runtime.configure()`
delegates all owned handler work to `observability/logging.py`.

### Opt-in OTLP export

Export is off by default (`otlp_enabled = false`). When off, Theater starts no
exporter thread and makes no network call; daemon log rotation still runs.

Accepted observer batches can emit agent metrics, logs, and spans independently of
any open trajectory viewer; the bounded Régie cache is never a telemetry source.
Generic signal transport lives in `observability`; feature projection lives in
`daemon/trajectory/telemetry`. `agent_metrics` projects request, TTFT, tool-duration,
token, cost, and failure metrics. Token and cost increments emit only after the usage
repository accepts the corresponding idempotency key. Model and tool labels, plus
emitted-record deduplication state, have hard process bounds.

`agent_logs` emits each accepted highest record revision, metadata only by default.
`agent_log_content` is opt-in because bounded content can include prompts, responses,
paths, and tool payloads. `agent_spans` emits only honest absolute intervals; a retained
call and later result may form one interval, but duration-only data never fabricates a
timestamp. Request/tool relationships use links, and only interval-compatible nested
tools use parentage. Signal failures are contained and never affect observation.

The agent metric catalog contains request duration and TTFT, tool duration, durable
token and cost totals, failure totals, and terminal request/tool-call totals. Metric
labels have explicit cardinality bounds. Logs and traces retain bounded exact model and
tool identities. Projection keeps a bounded, content-free record snapshot per
participant/source epoch so records split across observer batches can still pair. The
deduplication guarantee is process- and retention-bounded; a daemon restart or LRU
eviction may export an old operation again.

Enable it by installing the optional dependency and setting the config key:

```sh
pip install -e '.[observability]'
```

```toml
[observability]
otlp_enabled = true
```

Missing optional packages with `otlp_enabled = true` is fatal with an
actionable error: `install theater[observability] or disable
observability.otlp_enabled`.

#### Protocol and endpoint semantics

`otlp_protocol` is `grpc` or `http`. `otlp_endpoint` is a collector **base**
endpoint, not a signal-specific URL.

| Protocol | Default endpoint | Signal URLs |
|---|---|---|
| gRPC | `http://localhost:4317` | passed unchanged to all three exporters |
| HTTP | `http://localhost:4318` | `/v1/traces`, `/v1/metrics`, `/v1/logs` appended |

Do not silently pair gRPC with port 4318. A configured endpoint must be an
absolute `http` or `https` URL with a host and no query or fragment; a path
prefix is allowed and receives the HTTP signal suffixes. Blank configured
endpoints are rejected.

Only the tracer provider is published globally — MCP SDK middleware obtains its
tracer through the global API. Meter and logger providers stay private to
Theater. `configure()` returns an idempotently closable `RuntimeHandle`; a
module guard rejects a second configuration attempt in the same process,
including after shutdown. Global tracer providers cannot be reset safely.

Resource attributes on every owned provider: `service.name` (default `theater`),
`service.version` (installed distribution version, fallback `unknown`), and
`theater.process.role` (`daemon`, `mcp`, or `regie`).

### Trace chain and additive NDJSON `_meta`

Theater carries W3C trace context from MCP to the daemon over the existing
NDJSON transport by adding an optional top-level `_meta` object to requests.
This is additive — no `PROTOCOL_VERSION` bump — because old daemons already
ignore unknown top-level keys and new daemons accept requests without `_meta`.
Trace metadata never goes inside `params`, where handlers could see or reject
it.

An explicit `TraceContextTextMapPropagator` is used rather than the global
composite propagator; Theater propagates only `traceparent` and `tracestate`,
never baggage. Injection returns an empty mapping when no valid current span
context exists; callers omit `_meta` when empty. Extraction requires a
non-empty mapping, catches malformed-carrier errors, and returns `None` unless
the extracted span context is valid.

The expected trace chain when a client MCP supports SEP-414 and supplies valid
context:

```
client MCP span
  MCP SDK SERVER span        (built-in OpenTelemetryMiddleware, untouched)
    Theater daemon RPC CLIENT span
      daemon RPC SERVER span
        internal Theater spans
```

Theater does not write custom MCP tracing middleware. It reuses MCP SDK 2.0's
default `OpenTelemetryMiddleware` as-is. External client support is an
integration fact to verify, not a guaranteed property of every harness.

`RPC_CLIENT` (trace-only, `TraceKind.CLIENT`) wraps the `DaemonClient.call()`
path: started after connection and request ID allocation, kept open through
response validation and remote-error conversion. `RPC_SERVER`
(`TraceKind.SERVER`) wraps dispatch; `RPC_AWAIT` uses a separate spec for
`jobs.await`. Dispatch owns the RPC histogram — no second daemon-side RPC span
or metric exists.

### Daemon-only SQLite gauge sampling

Three observable gauges provide runtime metrics: `theater.participants.live`,
`theater.participants.addressable`, and `theater.jobs.active`. They are backed
only by cached integers — never by live queries.

`GaugeSampler` runs on the daemon event loop because Store's SQLite connection
is loop-thread-only. It updates a lock-protected cache at each sample interval.
OTel exporter callbacks run on exporter threads and read **only that cache**;
they never query SQLite or call domain services. This keeps export failure
from blocking observation and keeps SQLite access on the thread that owns it.

The sampler starts only after reconciliation and only when an active Theater
metric bridge exists (which implies successful OTLP setup). A directly
constructed test or embedded daemon may carry an enabled config without
process bootstrap, so the gate is the bridge, not the config flag. Until a
source has produced a value, its callback emits no observation rather than a
false zero. Per-gauge read failures are caught and logged independently so one
broken query does not suppress others. `stop()` is awaited before `Store.close()`.

### Accepted limitation: raw stderr generation

Raw stderr generation files are intentionally not rotated while a daemon is
alive. They normally contain only interpreter or native crash output and
accidental direct writes; routine Python logs go to the bounded rotating
`var/logs/daemon/daemon.log`. Per-generation retention (3 files total, including current) bounds
old files, not one pathological current generation. Rotating an arbitrary
inherited file descriptor requires a pipe or fd-reopen protocol; phase 1
deliberately avoids that complexity. The observed growth source — 25 MB/9 h of
routine logs — is moved to the bounded rotating `var/logs/daemon/daemon.log`.

---

## 14. Native runtime wiring: the Codex pilot

The harness seam in §9 starts and watches CLIs through a pane. This section
documents the additive second wiring: a harness plugin that can speak a
native control protocol declares it, and the daemon drives the agent through
a private per-participant connection or a passive frontend extension while
the stock native CLI UI stays attached to the same live session. Codex uses
the detached control host; OpenCode uses passive frontend status observation.
Pi uses its public extension for status and confirmed thinking updates. Claude
adds correlated command-hook tool observations. Their prompt, FIFO followup,
and interrupt routes remain on the ordinary pane path; Vibe stays legacy.

**MCP's constraint still holds.** MCP remains outbound-only (§1): a server
still cannot wake an agent, and nothing here changes that. Native runtime
control is a separate path — daemon → private per-participant socket or a
passive extension → daemon-owned Unix listener — parallel to tmux
`send-keys`, not a change to MCP or a new participant tier. The daemon keeps
sole ownership of SQLite and pane mutation.

### Ownership

| Layer | Owns |
|---|---|
| harness plugin (`RuntimeManifest`) | native protocol, capability detection, configuration mapping, session identity, event normalization |
| shared runtime helpers (`daemon/harness_runtime/`) | bounded native transport, frontend listener, and detached backend ownership |
| daemon runtime manager (`HarnessRuntimeManager`) | one `HarnessRuntime` instance per participant, backend generations, close-without-kill |
| daemon control service (`ControlService`) | authorization, idle checks, job correlation, the followup queue, delivery recovery |
| observation / trajectory | unchanged harness-neutral policy, with exact live evidence routed through the same reducer |

`HarnessManifest.runtime` is `RuntimeManifest | None`, and `None` is every
harness that predates runtime wiring: a local plugin overriding a shipped one
without a `runtime` field is legacy by construction. The manifest is pure
declaration — a read-only compatibility probe, a runtime factory, and a
single declared live channel. Detached hosts add a backend planner; frontend
hosts add a passive installer. The factory receives an immutable
`RuntimeContext`: participant/configuration facts plus injected I/O,
deliberately no Store and no Registry, so a plugin cannot re-implement daemon
policy per harness. History reads reach neither a runtime nor a backend — the
manager's `get` is creation-free.

The frozen `HarnessRuntime` operations:

```python
open_session(...)       # new, fork, or reconnect the exact native session
frontend_plan(...)      # native UI attachment only, never a prompt
snapshot(...)
send(...)
steer(...)
interrupt(...)
update_settings(...)
aclose()                # disconnect only; never terminates the backend
```

`aclose()` is the load-bearing one: a daemon restart disconnects Theater's
connections and leaves the backend and its UI running, so the next daemon
reconnects to the same conversation instead of starting another one.

### Topology and startup order

One isolated backend per participant, one UI attached to its thread:

```text
Backend: codex app-server --listen unix://<private-socket>
UI:      codex --remote unix://<private-socket> resume <thread-id>
```

The private endpoint carries WebSocket frames with an HTTP Upgrade handshake
— the native protocol, not Theater NDJSON. The participant's pane shows the
stock native CLI UI, not a Theater render. MCP configuration, approval,
model, reasoning, and the working directory are applied to the *backend* that
runs the agent, never only to the frontend.

The UI-first spawn order (frozen in `RuntimeLifecyclePhase`):

1. Validate spawn policy, requested wiring, native compatibility
   (probe), and resume identity.
2. Reserve participant, worktree, spawn job, backend generation, and
   private artifacts; persist the binding in `INTENDED` with its wiring,
   generation, endpoint, and launch-policy facts before any process exists.
3. Launch the detached backend and persist the verified pid plus its
   strong numeric start identity (`STARTED`).
4. Create the one runtime instance; `frontend_plan(native_session_id=None)`
   completes the observer handshake *before* the pane exists, so the eager
   `thread/start` a fresh UI emits can never race past the observer. The
   plan carries no prompt — the backend never submits one independently
   either.
5. Launch the UI pane.
6. `open_session(mode=NEW)` waits for the exact UI-created session from
   the `thread/started` broadcast — the working directory is a confirmation
   predicate, never a discovery source. `FORK` opens the predecessor's exact
   session first (`thread/fork`) and then plans the UI against the returned
   id.
7. Persist the exact native session identity (`BOUND`), then `ATTACHED`,
   from observed readiness evidence — no blind fixed sleep.
8. If a prompt was requested, submit it exactly once through the control
   service, reusing the spawn job — no second job exists (`ACTIVE`).

A promptless spawn completes after step 7. The whole pre-dispatch sequence is
bounded by a 30-second deadline. A failure before dispatch cleans up only
verified participant-owned resources — the backend first, then the pane, then
the binding; a failure after ambiguous dispatch preserves everything and
exposes the uncertain outcome. Nothing is ever resent or relaunched across
that boundary.

### Detached backend survival and reconnect

The backend runs in its own session (`start_new_session=True`) with stdout
and stderr in participant-owned log files — never in daemon pipes — so its
lifetime never depends on the daemon. Daemon shutdown runs
`HarnessRuntimeManager.aclose()`: every runtime disconnects, nothing
terminates. Only explicit participant kill, or a confirmed exit, reaches
`teardown` — the single path that signals a backend — and it verifies the
process identity (pid plus the persisted start time and observed process
name) before signalling, so a recycled pid is never signalled. A worktree
retires only after its participant's backend is proven stopped.

At daemon startup, `reconcile_runtime_bindings` runs before ordinary
observation assumes a backend is missing, in this order:

1. Fail never-dispatched queued/reserved work with `daemon_restarted` —
   never replay it.
2. Adopt the persisted backend only when pid and start identity verify
   against the live process; a mismatch means the backend is gone, never a
   signal to whatever recycled the pid.
3. Reconnect (`SessionOpenMode.RECONNECT`) the exact persisted native
   session via `thread/resume`; an identity mismatch fails closed — Theater
   never attaches by working-directory resemblance.
4. Consume stored terminal evidence: finish the same job exactly once.
5. Reconcile ambiguous delivery without retry.

A daemon that died before the backend's identity was persisted, or before
the session identity was bound, cannot safely name what is running: an orphan
diagnostic is exposed on the bus, no second UI is launched for it, and
nothing it cannot positively identify is ever signalled. A dead backend fails
affected jobs with `backend_gone` and follows ordinary participant lifecycle
policy.

Codex resume suppresses full turn hydration and reads a separate bounded
initial page. One owned history task then processes 16 summaries per page,
yielding after each 64-page pass while retaining its cursor. Backpressure
bounds pending evidence; transient failures retry without replaying prompts.
The pass boundary never abandons an older accepted turn.

### Capability and fallback rules

`RuntimeCapabilities` fails closed: the default supports nothing, and every
unavailable capability carries an explicit `CapabilityUnavailableReason` — an
honest refusal, never an optimistic default. A capability refused at
execution is final: the control is refused with the recorded reason, never
retried, never degraded to another transport. Settings are separately
capability-gated because Codex's `thread/settings/update` is experimental;
Theater does not emulate them by storing settings that might apply to an
unrelated future turn.

`wiring="auto" | "native" | "legacy"` is an additive spawn parameter on the
CLI (`--wiring`), the spawn RPC, and MCP `spawn_session`. Approval remains
per-spawn with no default anywhere and no connection to wiring.

- `legacy` is the explicit opt-out: the pane path, unchanged.
- `native` and `auto` are preferences: they select a compatible runtime when
  available and otherwise retain the ordinary launch. `auto` also respects
  the daemon's rollout gate.

**Automatic native selection is enabled** — the Wave 5 release gate passed at
the verified integrated base. `NATIVE_AUTO_SELECTION_ENABLED = True` in
`daemon/runtime/wiring.py` is the rollout constant: `auto`, the spawn default,
selects native only for Theater-verified-compatible *new* Codex spawns on the
pinned verified stock release. Automatic selection means *Theater-verified*
compatibility, never presumed vendor stability — the Codex policy is
`codex-appserver-0.154-verified`, so codex-cli 0.154.0 compatibility is the
verified boundary, proven end to end by the Wave 0 proof and its fixtures and
re-checked by the app-server handshake on every connection. Unknown or
unsupported versions retain legacy under `auto` and `native`. Explicit
`wiring="legacy"` remains the
per-spawn opt-out, and harnesses — including local overrides — without a
runtime manifest are legacy by construction. Existing participants stay
pinned to their persisted wiring: rollback flips the constant, sends future
`auto` spawns to legacy, and never rewires a live participant. The UI-first
promptless frontend, prompt-once dispatch, per-spawn approval with no default
anywhere, native-UI approval ownership, the guarded idle race, and MCP's
no-server-initiated-turn constraint are all unchanged by the rollout.

A natively-wired participant whose runtime is disconnected fails closed for
capabilities selected for the native transport: those controls are never
retried automatically. Manifest-declared legacy fallback capabilities keep
their pane route and its existing guards. A legacy participant keeps its
legacy transport: no runtime or binding. Shared presence guards apply to both
transports.

Frontend hosts use independent LIVE channel credentials, with the secret in
a participant-owned private file. The native UI connects to the daemon's
Unix listener; neither connection loss nor daemon restart replaces its
conversation. The bounded duplex broker correlates each request once and
never replays a request after timeout or disconnection. A frontend observer
with `drives_job_completion=False` enriches status while durable transcript
evidence still owns completion. Trusted identity is checked again after
composed observation awaits. The launch policy pins the host and every
control route so a plugin update cannot reroute an existing participant.

### Controls: the durable state machine

`ControlService` owns every Theater-originated control — ordinary send,
steer, queued followups, settings, interrupt. Physical facts (pane ownership,
focus-derived human presence, legacy-only copy-mode refusal, prompt limits, model
allowlists, legacy tmux delivery) arrive through injected `ControlGates`; native delivery goes
through one `HarnessRuntime` per participant. Exactly one `asyncio.Lock` per
participant: participant B's controls proceed while participant A's runtime
call blocks, and nothing global is ever held across native I/O.

Every control is a durable operation before transmission:

```text
RESERVED    persisted before transmission
QUEUED      a Theater-owned followup waiting for an authoritative idle check;
            its position comes from the persisted send-sequence allocator
DISPATCHED  persisted *before* transmission begins — an interrupted
            transmission stays potentially delivered
SETTLED     the terminal delivery result
```

Job state stays `running`/`done`/`crashed`/`killed` and is never implied by a
delivery phase. A native request id is a correlation fact, never a durable
idempotency guarantee, and a persisted operation id never justifies retrying
a native mutation.

The public surfaces — the wire protocol stays version 1, and the four new
methods are additive:

| RPC | CLI | MCP | Semantics |
|---|---|---|---|
| `participant.steer` | `theater steer` | `steer_session` | amend exactly the current job's active turn |
| `participant.queue_followup` | `theater queue` | `queue_followup` | awaitable send job with a queued delivery phase |
| `participant.settings.update` | `theater settings` | `update_session_settings` | idle-only model/reasoning change |
| `participant.controls` | `theater controls` | `get_session_controls` | capabilities, health, settings, active turn, queued handles |
| `participant.interrupt` | `theater interrupt` | `interrupt_session` | cancel the active turn *and* every undelivered followup |

- **Ordinary send** keeps the legacy gates and adds the authoritative
  runtime-snapshot idle check: pending native interactions, an active turn,
  active jobs, and queued followups all refuse. A queued followup can never
  be jumped by an ordinary send. If a simultaneous native-UI submission
  absorbs the prompt into a human-started turn, the runtime reports the
  actual returned turn and Theater records it — never a fabricated second
  turn, and never two Theater jobs on one native turn; a conflicting binding
  fails closed.
- **The followup queue** lives entirely in Theater — Codex's `thread/queue/*`
  is never used. Bound: 32 pending followups per participant, enforced before
  the job is created. A queued job carries no touch accumulator; it attaches
  when the item dispatches, so a pending followup can never receive path
  touches, results, or rescue attention by accident. Dispatch is FIFO, one
  prompt at a time, after an authoritative idle check, and it revalidates the
  original caller's ownership. Temporary conditions (busy, human present,
  pending interaction) defer the item; definitive ones (lost ownership, dead
  target, refusal, a backend relaunch across the reserved generation) finish
  it with an explicit error — never a replay into the new backend.
- **Steer** requires an active native turn mapped to a running Theater job
  and sends its exact `expectedTurnId`. It amends that job — no new handle,
  the original prompt and response-format contract preserved. A stale-turn
  refusal stays a refusal; a human-started turn owns no Theater job, so
  steering refuses rather than creating a synthetic one.
- **Settings** are idle-only, supplied-fields-only, and readback-confirmed:
  effective values are reported only after the native backend confirms them,
  and an uncertain application stays visibly uncertain. Approval and sandbox
  policy are immutable here — the service has no parameter that could carry
  them.
- **Interrupt** cancels the queue durably first, under the same
  per-participant lock as dispatch so no cancelled item can start
  afterwards, then requests interruption of the exact active turn
  (`turn/interrupt`). The active job finishes only from terminal evidence,
  mapped to `killed` with an `interrupted` error code; the participant stays
  alive. An interrupt while already idle still clears the queue. A human
  pressing interrupt in the native UI has the same queue effect through
  `handle_native_ui_interrupt` — the human already interrupted; Theater
  cancels what it owns.

**The accepted race.** Idle checks are guarded, not atomic against
simultaneous human input in the native UI. Theater serializes its own
controls per participant and rejects known-busy targets, but it cannot
prevent a human typing at the same instant — and Codex's `turn/start` can
steer an already-active turn. There is deliberately no input gateway and no
use of the native persistent queue; the recorded actual turn is the honest
outcome (see §12).

### Terminal evidence: durable before visible

A live channel is never a transcript surrogate. `HybridSource` composes the
durable reader — which keeps attachment, identity, resume floors, history,
and the persisted checkpoint cursor exactly as before — with the runtime's
single live `Source`, which owns current-turn deltas, status while healthy,
and exact terminal evidence. `Batch.terminal_evidence` is optional and
default-empty: legacy durable sources never populate it, existing event
constructors remain valid, and the batch is bounded at 512 outcomes.

The completion rule: **evidence commits before the job finish becomes
visible.** The observer routes live terminal evidence synchronously through
`ControlService.record_terminal_evidence`, which persists the
`NativeTurnOutcome` — keyed by participant, backend generation, native
session, and native turn — *before* finishing exactly its mapped job. The
crash window between the evidence commit and the job finish is the
intentional recoverable point: restart closes it exactly once from the
stored row (`finish_jobs_from_pending_evidence`), never from an
oldest-running heuristic, never by replaying the prompt. Evidence is
first-write-wins: repeated or delayed evidence cannot rewrite a terminal
state, and the queue-cancellation side effect of an interrupted turn runs
only for the first processing. A turn that maps to no Theater job (a human
turn) completes nothing; an ambiguous mapping fails closed.

Evidence persists its live/history origin and optional native completion
time. Historical interruptions cancel only causally related queued work:
native time establishes ordering; exact queue-predecessor identity resolves
missing or same-second timestamps. Ingestion time is never interruption
time, and queued predecessors never become delivered-turn job bindings.

A delayed durable record enriches history but cannot reopen a turn the live
channel reported terminal, and cannot regress the status a healthy live
channel last reported. Heuristic completion — rescue, identity loss, source
errors — is suppressed for live-wired jobs: only exact evidence finishes
them. The Codex durable parser remains canonical for billing; native
cumulative usage totals are never projected into per-response usage.

`Source.terminal_evidence_snapshot()` is the cancellation-safe handoff: the
bounded (512) set of outcomes consumed from an upstream live source but not
yet acknowledged as durably routed. The observer drains it when a composed
read is cancelled before its batch reaches the watch loop, so cancellation
can never drop the sole copy of evidence that can never be produced again;
rollback likewise retains it for replay. Legacy and durable-only sources
inherit the empty snapshot.

### Restart and unknown delivery

At restart, `RESERVED` operations never began transmission: they settle
`rejected` and their still-running jobs finish `crashed` with
`daemon_restarted` — safe to send again. A jobless `DISPATCHED` operation
settles `unknown`; job-bearing dispatched work is left untouched for exact
reconciliation. Nothing is replayed automatically; the caller decides.

An uncertain delivery — a lost acknowledgement, or a receipt naming another
operation — stays running: no resend, no tmux fallback. Reconciliation reads
only exact native facts: stored terminal evidence, or the authoritative
snapshot while the same turn is still live on the same backend and session.
Thirty seconds past the transmission, the affected job finishes `crashed`
with `delivery_unknown` and an explicit warning that native work may have
been accepted and may still be running. Late evidence must not rewrite
terminal job state.

### What did not change

- The three quiet timers (`RELOCATE`, `AWAITING_INPUT`, `RESCUE`) are
  untouched, and the reducer's policy is still the only job 2. A live
  source is just a `Source`: the seam (§6) holds on the new wiring too.
- Observability keeps its one process-level lifecycle (§13). A
  `WakeupSignal` — cleared before each read, with the ordinary poll interval
  as fallback — makes live delivery prompt without a task per message; a
  live stream does not remove the régie's polling.
- The daemon is still the sole SQLite and tmux writer, and the sole signaler
  of participant processes. MCP servers and the régie still only forward
  RPCs; the régie's steer/queue/settings/interrupt actions run as background
  tasks on per-operation connections, so a slow control never blocks
  polling or another participant's action.
