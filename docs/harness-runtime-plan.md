# Harness runtime wiring: implementation plan (frozen Wave 1 contracts and storage)

This document persists the approved harness-runtime wiring plan ("Harness runtime
wiring: Codex pilot, ACP optional"), the accepted Wave 0 UI-first refinement,
and the concrete contract and storage definitions frozen by the Wave 1
`runtime-contracts` worker at base `c2aa1b8466142c26e2eae24c76a44cfa1bcb33ad`.
It is the reference for Wave 2 prompts: downstream workers receive the
definitions in this document verbatim.

## 1. Outcome and fixed decisions

Build reusable runtime wiring so harness plugins can provide structured control
and live observation without rebuilding daemon machinery. First delivery:
shared runtime infrastructure plus one complete Codex integration. Existing
plugins remain compatible. ACP is a possible plugin transport — not Theater's
internal protocol and not a required dependency.

Required behavior (unchanged from the approved plan):

- Run an unmodified Codex app-server backend with its native CLI UI attached to
  the same live session.
- Keep backend and UI alive across Theater daemon restarts; reconnect without
  starting another conversation.
- Automatically select native wiring for verified, supported new Codex spawns.
  Add explicit `wiring="legacy"` opt-out. Existing participants keep their
  selected wiring.
- Keep ordinary `send` idle-guarded. Add separate steering and queued-followup
  operations. Steering amends the current job; it never creates a replacement
  job handle. Change model/reasoning only while idle, where the installed native
  API supports it. Leave approval and clarification responses exclusively in
  the native UI. Interrupt cancels the active turn and every undelivered Theater
  followup. Daemon restart fails undelivered followups; it never replays them.
- Finish exactly correlated native jobs after persisting terminal evidence,
  without waiting for transcript flush.
- Keep ordinary `send` semantics unchanged for legacy wiring.

**Accepted limitation:** idle checks are guarded, not atomic against
simultaneous human input. Codex's `turn/start` can steer an already-active turn.
Serialize Theater controls and reject known-busy sessions; document the
remaining native-UI race. No input gateway; no use of Codex's persistent queue.

Boundaries: no CLI forks, mandatory ACP conversion, daemon-protocol
replacement, new headless participant tier, remote network service, or
approval UI in régie. The daemon remains the only SQLite writer and
participant-process/pane controller. `Participant.addressable` remains physical
pane truth. Daemon wire protocol stays version 1; new methods and optional
response fields are additive. Plugins already execute inside the daemon — no
ACP, MCP, or other socket between a plugin and the daemon.

### Layer ownership

| Layer | Owns |
|---|---|
| Harness plugin | Native protocol, capability detection, configuration mapping, session identity, event normalization |
| Shared runtime helpers | Bounded connections, request correlation, deadlines, reconnect primitives, wakeups |
| Daemon runtime manager | Backend lifecycle, persisted bindings, one runtime instance per participant |
| Daemon control service | Authorization, idle checks, job correlation, followup queue, delivery recovery |
| Existing observation/trajectory | Harness-neutral policy, canonical projection, durable reconciliation |
| CLI/MCP/régie | Thin client calls and presentation |

## 2. Frozen public contracts (Wave 1)

All runtime contracts live in `theater/harness/contracts/runtime.py` and are
re-exported through `theater.harness.contracts` and `theater.harness`. Contract
modules never import `theater.daemon`. Everything is immutable
(`@dataclass(frozen=True, slots=True)`) and harness-neutral, with explicit
enums for all vocabulary.

### 2.1 Manifest integration

```python
HarnessManifest.runtime: RuntimeManifest | None = None
```

`None` preserves existing behavior exactly: no runtime probe, no native
planning, legacy launch, durable-only observation. `ManifestValidation`
rejects a manifest whose `runtime` is not a `RuntimeManifest`, whose probe or
plan or factory is not callable, whose channel is not a `LiveChannelDeclaration`
of kind `ChannelKind.LIVE`, or whose live channel id duplicates an observation
channel id. An enrichment of kind `live` is rejected — a live channel is
declared by `HarnessManifest.runtime`, never encoded as a `CompositeSource`
enrichment, because current enrichments cannot drive authoritative
events/status. The compiled harness carries `runtime` through
(`theater/harness/manifests/compiler.py`). No mandatory abstract methods were
added to existing harnesses or sources; old-style manifests (no `runtime`
field) remain valid — proven by the fixture at
`tests/fixtures/plugins/oldstyle/manifest.py`.

### 2.2 Enums (failure and state vocabulary)

- `RuntimeWiring`: `AUTO`, `NATIVE`, `LEGACY` — wiring selection for spawn
  surfaces and persisted bindings. Default on spawn surfaces is `AUTO`;
  `LEGACY` is the explicit opt-out.
- `RuntimeLifecyclePhase`: `INTENDED`, `STARTED`, `BOUND`, `ATTACHED`, `ACTIVE`,
  `DETACHED`, `STOPPED`, `FAILED` — ordered by the UI-first refinement below.
  `INTENDED` is persisted before the backend starts; `BOUND` means exact native
  identity is persisted; `ATTACHED` means UI readiness verified; `ACTIVE` means
  the initial prompt was accepted.
- `SessionOpenMode`: `NEW`, `FORK`, `RECONNECT`.
- `DeliveryResult`: `ACCEPTED`, `REJECTED`, `UNKNOWN`. `UNKNOWN` means
  transmission or acceptance was uncertain: **no retry, no tmux fallback**; the
  operation remains eligible only for reconciliation.
- `ControlDeliveryPhase`: `RESERVED`, `QUEUED`, `DISPATCHED`, `SETTLED`.
  `RESERVED` is persisted before transmission. `QUEUED` is a Theater-owned
  followup waiting for an authoritative idle check. `DISPATCHED` is persisted
  *before* transmission begins, so an interrupted transmission stays
  potentially delivered. `SETTLED` records a terminal delivery result; job
  state is separate and never implied by phase.
- `ControlKind`: `SEND`, `STEER`, `QUEUE_FOLLOWUP`, `SETTINGS_UPDATE`,
  `INTERRUPT`.
- `ControlTransport`: `LEGACY_TMUX`, `NATIVE_RUNTIME`.
- `RuntimeCapability`: `SEND`, `STEER`, `QUEUE_FOLLOWUP`, `SETTINGS_UPDATE`,
  `INTERRUPT`.
- `CapabilityUnavailableReason`: `NOT_DETERMINED`,
  `UNSUPPORTED_NATIVE_VERSION`, `GATED_BY_BACKEND`, `SESSION_STATE`,
  `WIRING_MODE`, `THEATER_POLICY`. An unavailable capability always reports
  one of these; an unsupported optional capability stays explicitly disabled
  rather than optimistically enabled. (Wave 0 fixtures map: settings gated by
  experimentalApi → `GATED_BY_BACKEND`; UI attach before rollout →
  `SESSION_STATE`; native queue unused → `THEATER_POLICY`.)
- `ConnectionHealth`: `UNOPENED`, `CONNECTED`, `DEGRADED`, `DISCONNECTED`.
- `NativeTurnTerminal`: `COMPLETED`, `FAILED`, `INTERRUPTED` — before
  job-state mapping (interruption maps to `JobState.KILLED`).
- `ResultCompleteness`: `COMPLETE`, `PARTIAL`, `UNAVAILABLE`.
- `ResultProvenance`: `NATIVE_EVIDENCE`, `LIVE_STREAM`, `TRANSCRIPT`,
  `UNKNOWN`.
- `NativeInteractionKind`: `APPROVAL`, `CLARIFICATION` — pending human
  interactions only the native UI may answer.

### 2.3 Value contracts

| Contract | Fields / responsibility |
|---|---|
| `RuntimeSettings` | `model`, `reasoning_effort` — effective only as confirmed by the backend |
| `NativeHumanInteraction` | `kind`, optional `native_request_id`/`native_turn_id`/`native_item_id`, bounded `details`; the answer belongs to the human in the native UI |
| `RuntimeCapabilities` | `available: frozenset[RuntimeCapability]` plus `unavailable_reasons: Mapping[RuntimeCapability, CapabilityUnavailableReason]`. **Fails closed**: the default supports nothing, and an unavailable capability reports its explicit reason or `NOT_DETERMINED`; a capability in both sets is a construction error, so every capability has exactly one effective answer. `supports(cap)` / `reason_for(cap)` |
| `RuntimeSnapshot` | `participant_id`, `backend_generation`, exact `native_session_id`/`native_turn_id`, `pending_interaction`, `settings`, `capabilities`, `health`, bounded `health_diagnostics` |
| `ControlReceipt` | `operation_id`, `result: DeliveryResult`, optional `native_turn_id`, `native_request_id` (correlation fact only — never a durable idempotency guarantee), bounded `error_code`/`error` |
| `NativeTurnOutcome` | exact `native_session_id`/`native_turn_id`, `terminal`, optional `result` (bounded), `completeness`, `provenance`, `error_code`, `error`. Status broadcasts are not terminal evidence; a snapshot-derived result is at most partial |
| `RuntimePlan` | pure backend `LaunchPlan` plus the private local `endpoint` string |
| `RuntimeBinding` | `participant_id`, `backend_generation`, `wiring`, `lifecycle`, optional `endpoint`, verified `pid` (only after the daemon verified process identity), exact `native_session_id`, `protocol`/`protocol_version`/`native_version`, `compatibility_policy`, frozen `launch_policy` mapping. Carries no credentials |
| `LiveChannelDeclaration` | wraps a `ChannelDeclaration` that must be `ChannelKind.LIVE`, plus `drives_job_completion=True` and `durable_fallback=True` |
| `RuntimeCompatibility` | `supported`, `policy` (bounded identifier-like name of the tested compatibility policy, default `"unverified"`), `native_version`, `reason`. Automatic selection means Theater-verified compatibility, not presumed vendor stability |
| `RuntimeNotification` | observed `method`, frozen `params`, optional `request_id` for server requests. Theater records server requests and relies on the native resolution notification; it must never send a response |

### 2.4 Contexts, injection seams, and errors

- `RuntimeProbeContext(participant_id, binary, cwd)` — read-only facts for the
  compatibility probe.
- `RuntimeCompatibilityProbe(context) -> RuntimeCompatibility` (Protocol).
- `RuntimePlanningContext(participant_id, cwd, endpoint, config_path, approval,
  model, reasoning_effort)` — immutable facts for one pure planning call.
- `RuntimeBackendPlanner(context) -> RuntimePlan` (Protocol) — writes nothing.
- `RuntimeContext(participant_id, cwd, io, backend_generation, endpoint,
  config_path, approval, model, reasoning_effort, native_session_id)` —
  immutable facts plus the injected `RuntimeIO`. **Deliberately carries no
  Store and no Registry**; `io` is the only way out. `backend_generation` is
  required and binds the runtime instance and every identity/evidence it
  produces to one exact launch generation. (`RuntimePlanningContext`
  deliberately has no `backend_generation`: the pure planner builds a
  command line from configuration, and the generation is daemon-side launch
  bookkeeping persisted with the binding.)
- `RuntimeFactory(context) -> HarnessRuntime` (Protocol).
- `RuntimeManifest(probe, plan, factory, channel)`.

Injection seams (daemon-owned implementations, Wave 2A):

- `RuntimeIO` (ABC): `async connect(endpoint, *, timeout) -> RuntimeConnection`.
- `RuntimeConnection` (ABC): `async request(method, params, *, timeout)`,
  `async notify(method, params)`, `notifications()` (async iterator, bounded
  buffering, never silently discards identity or terminal evidence),
  `async aclose()` (closes the connection, never the backend).
- Typed errors: `RuntimeConnectionError` (base),
  `RuntimeConnectionClosed`, `RuntimeRequestTimeout`, `RuntimeRequestError`
  (carries remote `code` and `message`).

Shared I/O implementations are injected through these public contracts only.
Plugin code must not import daemon internals — enforced by
`tests/test_harness_runtime_contracts.py` via an AST import-boundary check.

### 2.5 `HarnessRuntime` (ABC)

One participant's live native runtime. The daemon creates exactly one
instance per participant and shares it between observation and controls; the
instance exposes exactly one live `Source`. History reads go through durable
readers and must never reach the controlling connection or launch a backend.

```python
open_session(*, mode, native_session_id=None) -> RuntimeBinding
    # NEW: backend's own thread creation. FORK: native fork semantics from
    # native_session_id. RECONNECT: attach to the exact existing session;
    # identity mismatch fails closed, never attaches by cwd resemblance.
frontend_plan(*, native_session_id) -> LaunchPlan
    # Native UI attachment only; the plan carries no initial prompt.
live_source() -> Source
snapshot() -> RuntimeSnapshot
send(*, operation_id, prompt) -> ControlReceipt
    # New native turn. If a simultaneous native-UI submission absorbs the
    # message into an already-active turn, report the actual returned turn;
    # never fabricate a second one. Never bind two Theater jobs to one turn.
steer(*, operation_id, native_turn_id, prompt) -> ControlReceipt
    # Amend exactly the named active turn; a stale-turn refusal stays a
    # refusal, never reinterpreted as send or queue.
interrupt(*, operation_id, native_turn_id=None) -> ControlReceipt
update_settings(*, operation_id, model=None, reasoning_effort=None) -> ControlReceipt
    # Idle-only, supplied fields only; uncertain application stays visibly
    # uncertain; never emulate unconfirmed application.
aclose() -> None
    # Disconnect Theater's connections only; never terminate the backend.
```

### 2.6 Live observation (`Source` / `Batch`)

The existing durable `observation.primary` contract is unchanged for legacy
plugins. Additions:

- `ChannelKind.LIVE` (`theater/harness/contracts/channels.py`) for live-channel
  declarations; a live channel is not a transcript and not a database.
- `Batch.terminal_evidence: Sequence[NativeTurnOutcome] = ()` — optional,
  default-empty; validated to be `NativeTurnOutcome` instances; existing event
  constructors remain valid. Live-channel terminal evidence drives exact job
  completion.
- A future `HybridSource` (Wave 3B, not implemented here) composes the runtime's
  live source with the existing durable reader. Ownership is validated per
  effective wiring mode.

## 3. Frozen storage (Wave 1)

Alembic migration `theater/daemon/migrations/versions/0029_runtime_contracts.py`
(only migration in this wave) adds three daemon-owned tables;
`theater/daemon/schema.py` defines them; repositories live under
`theater/daemon/persistence/repositories/`; `Store` exposes the facade in the
`# ---- native runtime wiring` section.

### 3.1 `participant_runtime_bindings`

Primary key `participant_id` (one binding per participant; the binding row is
the current generation's record, `backend_generation` is part of the row).

Columns: `harness`, `wiring`, `backend_generation`, `lifecycle_phase`,
`endpoint`, `backend_pid`, `backend_started_at`, `native_session_id`,
`protocol`, `protocol_version`, `native_version`, `compatibility_policy`,
`launch_policy` (JSON), `created_at`, `updated_at`. Index on
`native_session_id` for exact identity lookup. No credentials are stored;
`launch_policy` is a bounded JSON mapping of launch-policy facts needed for
recovery (approval/model selection facts, not secrets).

Repository `RuntimeBindingRepository` / `ParticipantRuntimeBinding` dataclass:
`upsert`, `record_launch_intent`, `mark_backend_started(pid, started_at)`,
`bind_identity(native_session_id, protocol facts)`, `set_lifecycle`,
`get`, `find_by_native_session`, `list_recoverable`, `delete`, plus
`encode_launch_policy`. Write methods accept `connection=None` for
transaction composition (reads use the autocommit connection). All
generation-scoped mutations — `mark_backend_started`, `bind_identity`,
`set_lifecycle` — are guarded by `WHERE participant_id AND
backend_generation = expected`, never assign the generation themselves, and
return `bool`: `False` means the persisted binding carries a different
generation and a stale callback must fail closed instead of overwriting the
current generation's pid/session/lifecycle. `upsert` validates the row through
the public `RuntimeBinding` contract, so malformed launch-policy JSON or
oversized identifier/policy values are rejected before persistence.

Store facade: `runtime_transaction()` (`engine.begin()`),
`upsert_runtime_binding`, `get_runtime_binding`,
`runtime_binding_by_native_session`, `runtime_bindings_for_recovery`,
`mark_runtime_backend_started`, `bind_runtime_identity`,
`set_runtime_lifecycle` (all generation-guarded; the guarded mutations
return `bool`), `delete_runtime_binding`.

### 3.2 `control_operations`

Primary key `operation_id`. Columns: `participant_id`, `job_handle` (nullable),
`kind`, `transport`, `delivery_phase`, `delivery_result`, `backend_generation`,
`native_session_id`, `native_turn_id`, `queue_sequence` (nullable),
`payload` (bounded JSON, nullable; rejected when its UTF-8 encoding exceeds
`CONTROL_OPERATION_PAYLOAD_MAX_BYTES` bytes), `error_code`, `error`,
`created_at`, `updated_at`. Indexes: `(participant_id, delivery_phase)`,
`job_handle`, `(participant_id, queue_sequence)`.

Repository `ControlOperationRepository` / `ControlOperation` dataclass:
`reserve` (insert-or-ignore on `operation_id` — idempotent reservation),
`mark_dispatched`, `settle`, `get`, `for_job`,
`queued_for_participant` (FIFO by `queue_sequence`),
`dispatched_for_participant`, `pending_count_for_participant`,
`active_running_for_target` (the active-job seam), `prune` (settled rows
only, bounded).

`active_running_for_target` returns a running job when it is native and
transmission began — its `SEND`/`QUEUE_FOLLOWUP`/`STEER` operation reached
`DISPATCHED`, or `SETTLED` with `ACCEPTED` or `UNKNOWN` delivery (an accepted
or possibly-delivered turn keeps the job running until terminal evidence
completes it) — or when it is legacy and has no operation row at all.
`RESERVED` and `QUEUED` operations, and `SETTLED`/`REJECTED` operations,
never make a job active, so a queued followup can never become the oldest
eligible active job by accident. Both predicates are correlated `EXISTS`
checks scoped to the job's target participant: operations with a NULL
`job_handle` (settings/interrupt follow no Theater job) match no job and
never poison the legacy anti-join, and another participant's operations
never reclassify this one.

Queue positions come from `MetadataRepository.allocate_send_seq` — the existing
persisted send-sequence allocator in `meta` (`send_seq`), never `MAX(...)`,
timestamps alone, or an in-memory counter. The counter persists independently
of GC-prunable rows.

Store facade: `reserve_control_operation`, `get_control_operation`,
`control_operations_for_job`, `queued_control_operations`,
`dispatched_control_operations`, `queued_control_operation_count`,
`mark_control_operation_dispatched`, `settle_control_operation`,
`active_running_jobs_for_target`, `allocate_control_queue_sequence`,
`prune_control_operations`. The all-running-job queries
(`running_jobs_for_target`, `oldest_running_job_for_target`, …) are untouched
for cancellation and lifecycle handling.

Job state stays `running` / `done` / `crashed` / `killed`; queue/delivery phase
is separate metadata. Native request IDs are not durable idempotency
guarantees; a persisted operation ID never justifies retrying a native
mutation.

### 3.3 `native_terminal_evidence`

Composite primary key `(participant_id, backend_generation,
native_session_id, native_turn_id)` — exactly the correlation key of a native
turn. Columns: `terminal`, `result` (bounded), `result_completeness`,
`result_provenance`, `error_code`, `error`, `recorded_at`. Index on
`participant_id`.

Repository `NativeTerminalEvidenceRepository` / `NativeTerminalEvidence`
dataclass: `record` (insert-or-ignore, **first write wins**, returns whether the
row was written; values are validated through the public `NativeTurnOutcome`
contract, so oversized results/errors or malformed identity are rejected
before persistence — evidence is never truncated), `get` (exact four-part
key), `for_participant`, `prune` (bounded, via composite-key batching).

Store facade: `record_native_terminal_evidence`, `get_native_terminal_evidence`,
`native_terminal_evidence_for_participant`, `prune_native_terminal_evidence`.

### 3.4 Transaction boundaries

`Store.runtime_transaction()` (`engine.begin()`) gives one explicit
transaction; repository **write** methods accept `connection=` to compose into
it (reads use the autocommit connection). The three frozen boundaries:

1. **Persist launch intent before backend start.** The binding row is upserted
   in lifecycle `INTENDED` (with wiring, generation, endpoint, launch policy)
   and committed *before* the backend process is spawned — atomically with
   the participant/spawn reservation when they share one transaction; a rolled
   back reservation leaves no visible intent.
2. **Persist exact identity before initial dispatch.** The exact native
   session identity (and verified process identity) is committed before the
   initial prompt is transmitted — the dispatch is correlated to a durable
   identity, never to a cwd guess.
3. **Persist terminal evidence before exposing completion.** This is an
   ordered pair of commits, not one transaction: the evidence commit must
   precede the job finish, and a crash between the two commits is the
   *intentional* recoverable crash point — it leaves durable evidence and a
   still-running job, and restart reconciliation finishes the same job once
   from that evidence without replaying the prompt.

Pruning APIs (`prune_control_operations`, `prune_native_terminal_evidence`)
are bounded (`RUNTIME_STORAGE_PRUNE_BATCH` default batch) and may only run
after recovery/retention obligations end (settled rows whose job is no longer
running; evidence whose participant/generation is beyond retention). Counters
live in `meta`, independent of pruned rows.

## 4. Lifecycle: accepted Wave 0 UI-first refinement

For **new native Codex spawns only** (fork/resume and all other approved
contracts unchanged), the accepted refinement (Launce decision, Wave 0 review)
orders startup as:

1. Persist launch intent (binding in `INTENDED`).
2. Start the detached backend (lifetime independent of daemon pipes/shutdown).
3. Initialize the Theater observer (connect, native handshake).
4. Launch the promptless stock native UI; the UI creates the thread.
5. Discover the exact `thread/started` notification; extract the exact native
   session identity.
6. Persist identity (`bind_runtime_identity`, lifecycle `BOUND`) and verify
   UI/event readiness (no blind fixed sleep; readiness comes from evidence).
7. Theater submits the initial prompt **exactly once** through the control
   service (lifecycle `ACTIVE`).
8. Subscribe with `thread/resume` once the returned turn materializes the
   rollout.

The frontend command contains no initial prompt; backend startup never
independently submits it. Promptless spawn completes after successful
attachment.

Guards:

- Identity binds to the **verified private backend and launch generation**,
  not cwd alone. `backend_pid` is only persisted after the daemon verified the
  process identity; a PID/identity mismatch fails closed — no attachment or
  signal to the wrong process.
- Explicit approval/model configuration remains on the backend; it is never
  configured only on the frontend, and no duplicate MCP clients are created.
- Status broadcasts are not terminal evidence; only exact native terminal
  evidence completes a job.
- Recovery must support (a) crashes before identity persistence — the backend
  may be alive with a thread the daemon cannot name: reconcile via recovery
  queries without prompt replay and without launching a second UI; and (b)
  first-turn completion before observer subscription — reconcile missed
  evidence (`dispatched_control_operations`, evidence-for-participant reads)
  without prompt replay or launching a second UI.
- Fork/resume behavior and every other approved contract remain unchanged.

## 5. Control semantics (unchanged decisions, for Wave 2C)

- **Ordinary send**: verify pane ownership and copy-mode human-presence
  protection; check fresh native state, pending interactions, active jobs, and
  queued work; reject known-busy targets (ordinary send cannot jump a queued
  followup); reserve the job and the durable operation before transmission;
  correlate the receipt and later events by native session/turn identity.
  Serialize Theater submissions per participant; never hold a daemon-wide lock
  during native I/O. The accepted human-input race applies at dispatch: record
  the actual returned turn; never bind two Theater jobs to one native turn.
- **Queued followup**: awaitable job immediately, separate `QUEUED` delivery
  phase; the queue lives entirely in Theater; dispatch FIFO one prompt at a
  time after an authoritative idle check; if already idle, dispatch on the next
  scheduling opportunity; no path touches, active-turn completion, or rescue
  attached to pending jobs; temporary busy/human presence leaves items queued;
  lost ownership, dead target, or definitive refusal finishes the item with an
  explicit error; an interrupted native turn (including interruption initiated
  in the native UI) cancels remaining queued items; a normally failed turn does
  not cancel later followups. Default bound: `CONTROL_QUEUE_MAX_PENDING` (32)
  pending followups per participant, enforced before creating another queue
  job. Reuse existing prompt-size limits.
- **Steering**: require an active native turn mapped to a running Theater job;
  send the exact `expectedTurnId`; store the amendment against that job
  preserving the original prompt and response-format contract; a stale-turn
  refusal stays a refusal; no synthetic job for a human-only turn.
- **Settings**: recheck idle immediately before dispatch; update only supplied
  model/reasoning fields; show effective values only after native
  confirmation/readback; uncertain application stays visibly uncertain. The
  accepted native-UI concurrency limitation applies to idle-guarded settings
  too.
- **Interrupt**: durably cancel all pending followups; prevent a queued
  dispatch from crossing the cancellation boundary unnoticed; request
  interruption of the exact current native turn; finish the active job only
  from authoritative terminal evidence. Map native interruption to
  `JobState.KILLED`.

Delivery failure vocabulary (§3.5 of the approved plan, restated): rejected
before submission is a safe failure (a declared legacy fallback may be
considered); confirmed native acceptance tracks the turn; uncertain
transmission/acceptance means no retry and no tmux fallback; backend gone
fails affected jobs and follows participant lifecycle policy; identity
mismatch fails closed. Default deadlines: 30 s startup, 10 s control
acknowledgement, 30 s immediate ambiguous-delivery reconciliation; unresolved
jobs finish `crashed` with `delivery_unknown` and an explicit warning that
native work may have been accepted. Late evidence never rewrites terminal job
state.

Daemon shutdown disconnects runtime clients but never terminates healthy
backends. Explicit participant kill or confirmed participant exit terminates
its verified backend.

## 6. Downstream ownership (Waves 2–3)

| Wave | Worker | Owns |
|---|---|---|
| 2A | `runtime-engine` | `RuntimeIO`/`RuntimeConnection` implementation: WebSocket-over-Unix JSON-RPC, bounded receive/request queues and deadlines, server-request routing without automatic approval responses, backend process ownership/private endpoints/reconnect, one runtime instance per participant. Test disconnects, reordered replies, unknown notifications, oversized frames, saturation, process-identity mismatch, close-without-kill. No composition-root edits. |
| 2B | `codex-native-runtime` | Codex plugin production code: compatibility probe and backend/frontend planning (`RuntimeManifest`), new/fork/reconnect session handling, native control mapping and capability detection, live `Source`, item/turn normalization, human-interaction facts, backend MCP/policy configuration, durable/live identity alignment. Preserve all accepted Codex parser and usage fixes. No other harness edits. |
| 2C | `control-state-machine` | Daemon control service, job integration, active-job queries: operation reservation and receipt handling, send/steer/queue/settings/interrupt semantics, queue ordering and cancellation, unknown-delivery recovery, exact job/turn mapping and terminal-evidence completion, exclusion of queued jobs from active touch/completion behavior. Use fake runtimes; no client-surface or observation-policy edits. |
| 3A | `runtime-lifecycle-integration` | Daemon composition, spawning, restart, teardown: exact UI-first startup ordering (§4), prompt-once behavior, backend survival, restart reconciliation, orphan diagnostics, backend-before-worktree teardown. |
| 3B | `live-observation-integration` | `HybridSource`, observer wakeups, completion routing, trajectory reconciliation: live/durable authority, durable-before-completion ordering, exact job/path attribution, deduplication, bounded cooperative processing. |

Public surfaces (RPC/CLI/MCP/régie, Wave 4) are out of scope for Waves 2–3 and
unchanged here; the daemon wire protocol stays v1.

## 7. Test fixtures owned by this wave (for downstream reuse)

- `tests/rig/fake_runtime.py` — a small in-memory `HarnessRuntime` fake:
  `FakeRuntime`, `FakeRuntimeState`, `FakeSource`, `FakeRuntimeConnection`,
  `FakeRuntimeIO`, plus `fake_runtime_manifest()`, `fake_runtime_context()`,
  and `completed_outcome()`. State is shared between runtime and IO;
  `aclose()` keeps `backend_alive=True` (disconnect never terminates). Wave 2C
  drives control semantics against this fake without any native backend.
- `tests/fixtures/plugins/oldstyle/manifest.py` — an old-style local plugin
  with no `runtime` field, proving legacy manifests load, compile, launch, and
  observe unchanged.
- `tests/test_harness_runtime_contracts.py` — manifest integration, contract
  bounds, fake-runtime behavior, old-style compatibility, and the
  import-boundary check (contract modules must not import `theater.daemon`).
- `tests/test_runtime_storage.py` — migration columns, the three transaction
  boundaries (including rollback visibility), generation-guarded binding
  mutations (a stale generation cannot touch the current one), the
  settled-delivery active-job matrix (accepted/unknown active; rejected,
  queued, reserved not; null-job_handle settings/interrupt operations poison
  nothing), queue FIFO via the persisted allocator, bounded prunes, declared
  bound enforcement (UTF-8 payload bytes, launch-policy JSON, evidence
  bounds), evidence first-write-wins and crash recovery.

## 8. Required regression matrix (from the approved plan)

| Scenario | Required result |
|---|---|
| Native terminal before transcript flush | Job finishes from persisted evidence |
| Crash after evidence commit, before job finish | Restart finishes the same job once |
| Lost acknowledgement after prompt acceptance | No resend, no tmux double-send |
| Daemon restart during active turn | Same backend/session; active job reconciles |
| Restart with pending followups | Pending jobs fail; none replay |
| Human turn while followup queued | Human turn cannot finish the queued job |
| Steer with stale turn ID | Refusal; no new prompt/job |
| Interrupt racing queue dispatch | No cancelled item silently starts afterward |
| Native UI interrupts | Pending Theater followups cancel |
| Delayed/repeated transcript records | No duplicate result, usage, or trajectory item |
| Delayed history after newer turn starts | Participant status does not regress |
| Native approval/clarification request | Only native UI answers |
| Backend identity/PID mismatch | No attachment or signal to wrong process |
| Slow backend or worktree cleanup | Other participants and régie remain usable |
| Legacy/local plugin with no runtime field | Existing launch, observation, send, interrupt unchanged |
| Unsupported native capability/version | Honest unavailable reason or pre-launch legacy selection |
| Short-lived history request | No backend launch or control connection creation |

Wave 2 workers run focused tests only and return commit, changed paths, checks,
decisions, and blockers. Automatic native selection stays disabled until the
Wave 5 release gate passes.
