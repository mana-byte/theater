# RC10 execution plan: Theater waves and worker assignments

This is the orchestration companion to
[`rc10-implementation-plan.md`](rc10-implementation-plan.md). That document defines
what RC10 must do; this document defines how to implement and integrate it with
Theater workers. Both are planning documents. Creating this file does not launch
workers, implement RC10, or authorize publishing a release.

The implementation uses only these worker profiles:

| Profile | Theater harness | Exact model selection | Reasoning |
|---|---|---|---|
| **S** | `codex` | `gpt-5.6-sol` | `xhigh` |
| **G** | `pi` | `mistral/zai-glm-5-3` | `high` |

These spellings and efforts were accepted by the running daemon's model catalog
on 2026-09-17. Revalidate before execution. Catalog acceptance does not prove that
a provider account is authenticated or that the underlying CLI honored the model;
verify startup/session evidence too. Do not omit model/effort or substitute another
profile if one is unavailable.

The source baseline inspected for both plans is
`82986c9bbfcffe4cd198097abe1f73bfa96240e5`. The actual run must record its own clean,
reviewed starting commit after including the approved documents and reconciling
any intervening user changes.

Navigation:

- [1. Team and operating rules](#1-team-and-operating-rules)
- [2. Keep the coordinating Theater independent](#2-keep-the-coordinating-theater-independent)
- [3. Branches, ownership, and handoffs](#3-branches-ownership-and-handoffs)
- [4. Wave schedule](#4-wave-schedule)
- [5. Detailed wave assignments](#5-detailed-wave-assignments)
- [6. Theater tool procedure and prompt templates](#6-theater-tool-procedure-and-prompt-templates)
- [7. Integration, failures, and corrections](#7-integration-failures-and-corrections)
- [8. Final review, acceptance, and shutdown](#8-final-review-acceptance-and-shutdown)

## 1. Team and operating rules

### 1.1 The coordinator owns the result

The coordinator remains responsible for planning, interface decisions, file
ownership, worker messages, commit review, integration, complete validation and
user communication. The coordinator's current harness/model need not change;
every delegated worker and reviewer must come from the two-profile pool above.

Use the repository's
[`theater-orchestrate` skill](../theater/skills/builtin/theater-orchestrate/SKILL.md)
as the execution workflow. Use Theater MCP for discovery, spawning, sending,
waiting, transcript inspection and participant lifecycle. Do not mix in native
Codex/Pi subagents or another orchestration system.

Start with **two implementation lanes at most**: one S worker and one G worker.
Only run both when their assigned files and prerequisite interfaces are disjoint.
Serial waves deliberately leave a lane idle. Do not manufacture parallel work by
asking both workers to explore or redesign the same subsystem.

Use S for transaction boundaries, durable state machines, identity, native
runtime integration, presence and recovery. Use G for bounded public clients,
contract consumers, catalog/read adapters, scratchpad, UI extraction, packaging
and independent test infrastructure. These are task assignments, not claims
about model pricing or guaranteed relative performance.

### 1.2 Worker invariants

Every implementation assignment includes all of the following:

- One bounded task, current base SHA, exact owned paths and prohibited files.
- Exact profile, `approval="yolo"`, and an isolated Theater worktree.
- Applicable sections of the technical specification and frozen interface names.
- Focused validation commands; workers never run the full repository suite.
- A requirement to commit code/test changes and leave the worktree clean.
- A compact result containing commit, changed paths, checks and blockers.
- No repository-wide formatting, dependency upgrades, schema changes or API
  changes outside assigned ownership.
- No nested workers, user messaging, PR publication, release, or worker cleanup.

Explicit `yolo` is an execution setting for unattended authorized work. It does
not expand file ownership, permit unrelated destructive actions, or override the
requirement to protect the coordinating daemon and user sessions.

### 1.3 Reuse sessions without reusing stale branches

Prefer reusing the S and G implementation sessions between compatible waves to
avoid accumulating idle terminals. Reuse is allowed only after all prior work is
accepted/integrated, the worker is idle, its worktree is clean, and its HEAD is an
ancestor of the next integration checkpoint.

Send a complete new assignment through Theater, including the instruction to
fast-forward its own worktree to the exact new checkpoint and verify HEAD before
editing. Do not depend on remembered interface details. Never reset or rebase a
dirty worker branch to reuse it. If fast-forward is impossible, inspect and
preserve the unaccounted commits before deciding whether a new session is needed.

Fresh independent review sessions are mandatory in wave 14; implementation lanes
must not review their own integrated work as the final independent check.

## 2. Keep the coordinating Theater independent

### 2.1 Two separate runtime environments

Theater is both the tool running the workers and the software being changed.
Treat those as distinct installations for the entire refactor.

| Environment | Purpose | Required isolation |
|---|---|---|
| **Control installation** | Existing RC9 daemon, coordinator MCP, worker MCP/runtime wiring and real worker terminals | Immutable code/interpreter, existing control home and pinned tmux server |
| **Candidate installation** | RC10 unit/integration tests, migration tests, fake provider, bridge and packaging smoke tests | Per-run test homes, separate interpreter/builds, isolated tmux servers and process identities |

Do not upgrade, restart, hot-reload or repoint the control installation to an
integration branch while its participants are active. RC10's drained-upgrade
guard would correctly reject this database, and an accidental bypass could
strand the workers or destroy their orchestration state.

A long-lived RC9 daemon is not safe merely because it started before edits:
Python lazy imports, subprocess entry points and injected worker MCP commands
may read changed files later. Verify that their executables and module paths
come from a frozen wheel/install or an immutable RC9 checkout, never a mutable
worker or integration directory.

If the current control plane imports mutable repository code, resolve that
before implementation. Existing live workers must not be silently migrated or
restarted. Record the obstruction and establish a stable control environment
with the user when needed; the documentation task itself performs none of this.

### 2.2 Isolation rules for every worker prompt

Preserve the worker session's inherited control environment for its Theater MCP
connection. Override environment only for candidate test subprocesses. Never
globally replace the worker's `THEATER_HOME` or change its MCP configuration to
point at the candidate daemon.

Candidate subprocesses require:

- A unique, short `THEATER_HOME`, preferably a fixture-owned directory beneath
  `/tmp`, respecting macOS Unix-socket path limits.
- An explicitly isolated tmux socket/root. Clear inherited `TMUX` and `TMUX_PANE`
  for that subprocess; `TMUX_TMPDIR` alone does not override inherited `TMUX`.
- Removal/replacement of inherited participant correlation and hook/runtime
  credentials, including `THEATER_ID`, where the subprocess is a test participant.
- Explicit candidate executables/module paths; no accidental control-daemon
  autostart through a default CLI invocation.
- Cleanup limited to test-created process/server identities whose ownership is
  recorded. Never use a bare `tmux kill-server` against the inherited environment.

The existing [`tests/conftest.py`](../tests/conftest.py) and
[`tests/tmux_guard.py`](../tests/tmux_guard.py) already protect pytest paths.
Their fixtures run after import/collection; preserve them and add subprocess
containment where a new raw client, provider or wheel smoke test bypasses them.
Never import code that opens a daemon or terminal as a module side effect.

Give each worker independent candidate homes and environments. No shared test
SQLite database, provider identity, socket path, tmux server or writable virtual
environment between parallel workers. Shared package caches are acceptable.

### 2.3 RC9 semantics remain in force for the workers

Until the implementation run ends, the **control** daemon still has RC9 behavior:

- Scratchpad is scoped by orchestration tree and canonical Git repository.
- Unique-worktree creation from a linked checkout has the inspected path/base
  hazard; spawn from the canonical repository root with explicit base commit.
- Killing a unique-worktree worker can delete its worktree and branch.
- A coordinator successor does not automatically own the predecessor's children.
- Public RC10 methods, provider selection and global scratchpad are unavailable
  to the control installation.

Do not use the new RC10 semantics to justify control-plane cleanup or recovery.
Candidate feature tests use the candidate daemon explicitly.

## 3. Branches, ownership, and handoffs

### 3.1 Starting and checkpoint branches

During execution, the coordinator prepares one integration branch, for example
`rc10/integration`, in an isolated integration checkout. It preserves all existing
user changes and commits the approved plan documents before workers need them.
This is a future implementation step, not a commit made by this documentation task.

Each wave begins from an immutable full SHA, denoted `B00`, `B01`, and so on.
Store actual SHAs in the execution ledger; never pass those labels literally to
Git or Theater. All parallel workers in a wave start from the same SHA.

For a new worker use:

- `cwd`: canonical `/Users/manaiki.laut/Desktop/theater`, re-resolved at run time.
- `worktree`: `true`, never a shared named worktree.
- `base_branch`: the exact checkpoint SHA, even if the integration branch moves.

The canonical checkout may remain on the baseline; the explicit SHA selects the
correct tree. Record Theater's returned participant ID, worktree path, branch,
spawn-job handle and eventual harness session ID. Use IDs for later targeting.

### 3.2 Integration policy

Use deliberate merge commits for accepted worker commits, rather than routinely
cherry-picking them. This preserves the accepted worker commit as an ancestor of
the integration branch and allows clean worker branches to fast-forward for
reuse. Do not merge a branch name that has advanced since review; merge the exact
reviewed commit SHA.

Before integration, check actual diff, tests, changed paths and interface
compliance. After both branches and coordinator-owned wiring are integrated,
run the wave gate. Only a passing gate creates the next checkpoint.

If integration exposes a cross-worker defect, repair the integrated result
through the original worker when its context/branch and correction allowance are
still suitable, otherwise locally as coordinator. Do not spawn a third fixing
worker or start the next wave against a half-integrated branch.

### 3.3 Shared files have one owner at a time

By default, the coordinator owns composition and high-conflict files:

- `theater/daemon/server.py`, `theater/daemon/runtime/wiring.py`.
- `theater/daemon/persistence/store.py` and repository export registries.
- Public/private RPC registration modules and package `__init__.py` exports.
- Root `pyproject.toml`, `uv.lock`, shared test configuration and `tests/conftest.py`.
- `AGENTS.md` and plan updates.

A task card below may explicitly transfer a named file for that wave. Record the
transfer before spawning and take it back at the gate. For example, wave 2 gives
S ownership of `schema.py`, database/migration foundations and domain fields;
wave 13 gives G ownership of packaging/CI files.

Workers needing an unowned file return a precise requested interface/change to
the coordinator; they do not opportunistically edit it. The coordinator either
makes the small composition change or changes the ownership schedule. Avoid
having workers generate patch files/reports in the repository as handoffs.

New test filenames follow task-specific prefixes in these cards. Test-only work
is substantive when it exercises a real boundary or failure mode; do not delegate
a prose-only report task as an extra lane.

### 3.4 Interface checkpoint

Wave 1 freezes the vocabulary and signatures that concurrent workers consume:

| Interface | Required agreement |
|---|---|
| Actor/ownership | Operator client identity, optional participant initiator, immutable lineage, current-owner revision |
| Write unit | Transaction connection, journal append and after-commit cache/waiter behavior |
| Public transport | Handshake/version/role, envelopes, errors, idempotency key, limits |
| Provider channel | Callback schemas, generations, report revisions, lease and terminal identity tuple |
| Operation result | Public state, detailed phase, linked control operation/job, uncertainty/barrier |
| Workspace usage | Ownership, exact path/base, reservation-to-participant handoff and deletion fence |
| Events | Transaction groups, entity revisions, stream/cursor behavior and resnapshot errors |

The technical specification controls the required semantics. Wave 1 chooses
concrete Python symbols/layout and schema identifiers within those boundaries.
Provide exact snippets from the committed interfaces in both affected worker
prompts. Any later change increments the recorded interface revision, updates
schemas/tests, and pauses dependent assignments until they receive the revision.

### 3.5 Execution ledger and scratchpad

The coordinator maintains a durable run ledger outside worker-owned source files.
At minimum record run ID, control installation identity, integration branch,
checkpoint SHAs, task ownership, participants, worktrees, job handles, worker
commits, correction counts, gate results and unresolved blockers. Do not store
tokens, launch environments or credentials.

In the task cards, paths are repository-relative. Expand abbreviated `daemon/`
and `persistence/` references to `theater/daemon/` and
`theater/daemon/persistence/` in the actual worker prompt. Test basenames refer
to `tests/` unless the card explicitly places them under `packages/regie/tests/`.
Never send a worker an unresolved path abbreviation as its ownership boundary.

Use Theater scratchpad for a compact mirror under `rc10-<run-id>`: current wave,
base SHA, interface revision, ownership map and accepted commits. The coordinator
is the only writer of scheduling/ownership entries. Scratchpad is neither a lock
nor the durable execution record.

The coordinator must be registered from the canonical Git repository for RC9
scratchpad sharing. If its participant cwd is outside Git, deliver complete
handoffs directly and keep the durable ledger; do not assume changing the shell
cwd changes its registered scope, and do not let scratchpad failure block code
that already has a complete prompt.

## 4. Wave schedule

`C` denotes coordinator work. S/G task IDs identify assignments, not fixed
participant IDs. A reused participant can complete several sequential task IDs.

| Wave | Purpose | S lane | G lane | Spec work packages |
|---|---|---|---|---|
| 00 | Safe orchestration/test baseline | C preflight; no S implementation | G00 isolation helpers | Prerequisite |
| 01 | Freeze shared public/domain contracts | S01 schemas and interfaces | G01 raw consumer fixtures after S01 | W01 |
| 02 | Make durable state transactional | S02 storage, migrations, records | Idle | W02, W03 |
| 03 | Connect public clients safely | S03 server negotiation/dispatch | G03 SDK RPC and raw client | W04 |
| 04 | Durable work and callback client | S04 operations/idempotency | G04 provider SDK channel | W05, part W06 |
| 05 | Register and recover terminal providers | S05 provider service/fencing | G05 standalone fake provider | W06 |
| 06 | Workspace and scratchpad semantics | S06 workspaces/usage/cleanup | G06 global scratchpad | W08 foundations |
| 07 | Launch/adopt through the provider | S07 launch/native/adoption | G07 catalogs and diagnostics API | Part W07, W04 |
| 08 | Control admission and observation API | S08a controls, then S08b presence | G08 transcript/recall/job read adapters | Remaining W07, W04 |
| 09 | Recover live state and read the journal | S09 recovery/control transfer | G09 snapshot/follow server | W09, part W10 |
| 10 | Publish every projection change | S10 writer/event coverage | G10 SDK state-follow controller | Remaining W10 |
| 11 | Independent Régie and real provider | S11 tmux bridge implementation | G11 Régie extraction/UI migration | W11, part W12 |
| 12 | Complete CLI/config and remove coupling | S12 boundary removal/regressions | G12 CLI/config/private adapters | Remaining W12 |
| 13 | Reproducible release candidate | S13 fault/integration tests | G13 packaging/CI/migration docs | W13 |
| 14 | Validate and independently review | Fresh reviewer RS14 | Fresh reviewer RG14 | Final acceptance |
| 15 | Corrections, final validation and handoff | Original worker only if appropriate | Original worker only if appropriate | Coordinator closure |

This ordering refines the technical document's work packages. For example,
workspace services precede provider-backed spawn so launch usage does not need
to be retrofitted later. Event hooks exist from wave 2; the complete reader and
writer coverage arrive in waves 9–10.

`Bnn` is the accepted output checkpoint of wave `nn`; the next wave starts from
it. Record intermediate SHAs for ordered subassignments, such as S01 before G01,
without pretending that the whole wave passed. Waves 14–15 form one final review
and correction loop; the review handoff is not a feature-acceptance checkpoint.

## 5. Detailed wave assignments

### Wave 00 — Establish an implementation environment that cannot kill its coordinator

**Purpose.** Prove control/candidate isolation and preserve the starting state
before any daemon or persistence change.

**Coordinator entry work.** Inspect repository status/instructions, include the
approved documents in the implementation baseline, validate the worker roster,
record actual installed runtime paths, control home/socket/server and current
participants. Confirm the control code remains immutable throughout the run.
Run initial relevant baseline checks in the isolated candidate environment and
record pre-existing failures instead of making workers rediscover them.

**G00 — Candidate subprocess isolation.**

- Own new `tests/rc10_support/` isolation utilities and
  `tests/test_rc10_isolation.py`. Read existing conftest/tmux guards; changing
  shared conftest requires coordinator approval of the file transfer.
- Implement helpers that allocate short test homes, isolated tmux roots and
  candidate subprocess environments without modifying the worker's control env.
- Ensure cleanup retains reachability when a test-owned process cannot be proven
  stopped. Keep ownership checks explicit; no inherited-server cleanup.
- Add focused tests proving an inherited control `THEATER_HOME`, `THEATER_ID`,
  `TMUX` and `TMUX_PANE` cannot leak into candidate fixtures.
- Validate the new isolation tests and existing `test_tmux_guard.py`. No harness
  worker is launched by this test package itself.

**Gate/checkpoint B00.** Coordinator inspects the helpers and runs their focused
checks. Candidate test processes cannot reach control state or terminals. Both
profiles are viable and the runtime/code isolation is recorded. Do not advance
if the daemon/MCP entry points still resolve into a directory about to be edited.

### Wave 01 — Freeze the seams before parallel implementation

**Purpose.** Make the wire vocabulary and shared Python contracts concrete so
later workers implement the same architecture.

**Coordinator preparation.** Make the agreed schema-validator dependency and
lockfile update in the integration checkout before S01's schema tests need it.
Publish that exact starting SHA. This is the sole dependency owner until a later
explicit packaging assignment.

**S01 — Contract and domain interfaces.**

- Own new `theater/frontend/schemas/`, `dto/`, `errors.py`, `capabilities.py`,
  and focused `tests/test_rc10_schema_catalog.py`.
- Define the full method catalog from spec §8.4, role/handshake schemas,
  provider callbacks, operation/job DTOs and event transaction envelopes.
- Define narrow shared interfaces for write units, terminal identity, actor,
  operation linkage and workspace usage in their intended domain packages. They
  may be protocol/value declarations at this stage, not alternate executors.
- Keep DTO imports free of daemon, Textual, private client/config and tmux.
- Include compatibility/unknown-value representation and concrete error codes;
  do not defer these decisions separately to the SDK and server workers.
- Validate schema resource references and representative requests, responses,
  callbacks and events. Preserve all approved semantics.

**Coordinator intermediate gate.** Review S01 against spec §§7–9, integrate the
contract commit and publish its exact symbols/schema revision. Only then start G01.

**G01 — Independent raw contract consumer.**

- Own `tests/rc10_support/raw_client.py`, raw JSON fixtures under
  `tests/fixtures/rc10/`, and `tests/test_rc10_raw_framing.py`.
- Build a stdlib-only socket client/decoder from the frozen contract. It must not
  import Theater, its SDK or installed schema libraries in the client process.
- Exercise framing, explicit errors and unknown response fields using a tiny
  fixture server; real-daemon calls arrive in wave 3.
- Report schema ambiguities to the coordinator instead of changing S01's files.
- Validate only raw-framing fixtures/tests and relevant schema examples.

**Gate/checkpoint B01.** The contract and independent consumer agree on bytes,
field names, actor/identity tuples, limits and error semantics. No placeholder
method catalog or conflicting operation state vocabulary remains.

### Wave 02 — Transactional storage and guarded migration

**Purpose.** Supply durable foundations before any new public mutation can escape
as a non-atomic write.

**S02 — Persistence foundation.**

- Own `theater/daemon/schema.py`, `persistence/database.py`, new
  `persistence/transactions.py`, required new repository modules and RC10 Alembic
  revisions. Explicitly include required `theater/models.py` domain fields and
  migration environment changes for this wave.
- Implement the RC9 drained guard before schema mutation from daemon startup and
  direct online Alembic upgrade. Preserve old history and discard scoped
  scratchpad only through the specified migration.
- Add provider/binding/owner/workspace/usage/operation/idempotency/journal/global
  scratchpad records, constraints and durable generation/sequence allocation.
- Introduce write units, connection propagation and after-commit hooks/cache
  staging. Reuse existing control/runtime/evidence records and link them to
  public operations; no competing completion machinery.
- Add focused migration, atomic rollback, uniqueness, sequence non-reuse and
  restart tests. Cover live external participants and running jobs as drain blockers.
- Keep legacy private storage behavior working while new services are unwired.

**Coordinator work.** Own Store composition/repository exports unless explicitly
transferred. Use the validator dependency/lock change already integrated in
wave 1; workers do not independently rewrite lockfiles.

**G lane.** Idle. The shared schema/write-unit boundary is too central to split
between simultaneous authors.

**Gate/checkpoint B02.** Fresh and drained RC9 migrations pass; live RC9 upgrade
refuses without mutation; a rolled-back state transition produces neither a
journal row nor a changed cache; sequence/generation counters survive GC/reopen.
Required existing store/control persistence tests pass.

### Wave 03 — Public dispatcher and ordinary SDK

**Purpose.** Establish one real public socket path without exposing private RPCs.

**S03 — Server transport boundary.**

- Own `theater/daemon/frontend/handshake.py`, `router.py`, `validation.py`,
  contract/health handlers, and `theater/daemon/runtime/socket.py` for this wave.
- Implement peer UID checks, permanent public/private classification, role and
  capability negotiation, request validation, limits and structured errors.
- Keep private clients on their existing dispatch path. Public methods map to a
  curated registry, never arbitrary private-method forwarding.
- Implement schema discovery and initial safe read endpoints against real storage.
- Own focused `test_rc10_handshake.py` and `test_rc10_public_dispatch.py`; extend
  socket/message-size tests only for this boundary.

**G03 — SDK ordinary connections.**

- Own `theater/frontend/client.py`, `transport.py`, domain client method wrappers
  and `tests/test_rc10_sdk.py`, `test_rc10_raw_client.py`.
- Implement connect-only lifecycle, typed envelopes/errors, one in-flight request
  per connection, wait/interactive connection separation and unknown-value decoding.
- Drive the actual daemon with the independent raw client from G01 for handshake,
  discovery and the implemented read endpoint. No private imports/autostart.
- Use frozen schemas rather than inspecting server private implementation to
  reconstruct undocumented behavior.

**Coordinator integration order.** S03, G03, then composition/exports and any
cross-process fixture wiring. Neither worker edits the other's registry/client.

**Gate/checkpoint B03.** A separate stdlib process talks to the candidate daemon;
wrong majors/capabilities/roles and public-private crossover are rejected;
existing private client smoke tests still pass.

### Wave 04 — Durable mutation execution and provider callback client

**Purpose.** Separate accepted work from request-handler lifetime, and prepare
the provider side of reverse requests.

**S04 — Operations and idempotency service.**

- Own new `theater/daemon/operations/`, operation public handlers and focused
  `test_rc10_operations.py` / `test_rc10_idempotency.py`.
- Implement validated-payload digesting, atomic key claim, original-handle return,
  payload conflict, settlement retention and indefinite unsettled pinning.
- Implement durable workflow phases, linked control-operation/job references,
  request disconnect semantics, bounded operation await and evidence-only reconcile.
- Exercise a controlled side-effect fixture: response loss must return the same
  accepted handle; handler cancellation must not undo accepted work.
- Expose transaction boundaries/hooks needed by spawner/controls without yet
  rewriting their native policy in this assignment.

**G04 — Provider SDK channel.**

- Own `theater/frontend/provider.py` and focused
  `tests/test_rc10_provider_client.py`.
- Implement handshake/channel lifecycle, callback reader/writer, bounded pending
  requests, exact correlation and unknown/late response handling.
- Keep read processing alive while an asynchronous callback handler waits.
  Reject/drop queued work from a lost generation before side effects start.
- Test independent terminal concurrency, same-terminal serialization and frame
  limits with a fixture daemon; no production tmux implementation yet.

**Gate/checkpoint B04.** Operation retry/cancel behavior is durable, and the
provider client cannot deadlock by waiting for a response on a blocked RPC
connection. Existing harness frontend callback tests retain their old framing
and bounds; terminal-provider changes do not silently alter that protocol.

### Wave 05 — A real provider service without tmux assumptions

**Purpose.** Prove the reverse transport and durable provider identity before
connecting it to production spawn/control policy.

**S05 — Provider registry, connection and binding services.**

- Own new `theater/daemon/terminals/registry.py`, `connections.py`, `bindings.py`,
  `service.py`, provider public handlers and focused `test_rc10_providers.py` /
  `test_rc10_provider_fencing.py`.
- Implement operator registration/credential verification, unique selectors,
  generation acquisition, callback authentication and current-generation reports.
- Implement heartbeat/lease expiration, immediate disconnect invalidation,
  `reconciling` state and verified terminal inventory/binding restoration.
- Enforce pending-request bounds and per-terminal mutation serialization without
  blocking provider inventory/heartbeat progress.
- Bind each mutation to an operation, provider generation and terminal
  incarnation; mark possible execution uncertain after acknowledgment loss.
- Add deterministic clock tests for expiry, competing live owners, stale frames,
  reused terminal IDs and current-provider historical receipt reconciliation.

**G05 — Standalone non-tmux provider fixture.**

- Own `tests/rc10_support/provider_process.py` and
  `tests/test_rc10_provider_process.py`.
- Implement a separate-process fixture speaking only the public wire contract.
  It must not import daemon internals or call tmux. Use fixture-owned child
  processes/terminal stand-ins with explicit identities and receipts.
- Support controlled create/inspect/deliver/interrupt/terminate outcomes, incomplete
  inventory, disconnect before/after side effects, delayed replies and occupant
  replacement. Failure injection must be deterministic.
- Record executions in fixture-owned evidence so tests can prove no duplicate
  side effect occurred; never equate a daemon self-report with execution proof.
- Exercise the SDK provider protocol and raw operator client together after S05
  integration. Spawn policy and real agent jobs are not yet this fixture's scope.

**Gate/checkpoint B05.** Two different fixture terminals can progress concurrently;
one terminal remains serialized; disconnect revokes routes; generation/occupant
fences hold; no uncertain callback is replayed. Provider lifetime is independent
of a request/ordinary RPC connection.

### Wave 06 — Durable workspace usage and machine-wide scratchpad

**Purpose.** Prepare the filesystem/data semantics that provider-backed launch
will rely on. These domains can be developed in parallel with separate ownership.

**S06 — Workspace lifecycle and exact Git operations.**

- Own `theater/daemon/worktrees/`, workspace repository/service extensions,
  `daemon/frontend/handlers/workspaces.py`, `tests/test_rc10_workspaces.py` and
  relevant additions to `tests/test_worktree.py`.
- Normalize borrowed, frontend-owned, unique and named workspace requests.
  Capture initiating checkout HEAD independently of canonical storage root.
- Implement exact path/branch/base recording and atomic reservation/live usage
  transitions. No usage release on provider lease expiry.
- Implement external deletion prepare/confirm/cancel fences and Theater-owned
  cleanup with distinct dirty/branch force semantics and partial results.
- Preserve named-join and retained-branch reuse restrictions. Verify ownership
  again before deletion; do not use cwd/branch prefixes as authority.
- Test linked checkout with a different HEAD from the main checkout, overlapping
  usage/deletion requests, dirty work, branch retention and ambiguous Git results.

**G06 — Global scratchpad service.**

- Own `persistence/repositories/scratchpad.py`, `daemon/rpc/scratchpad.py`,
  `daemon/frontend/handlers/scratchpad.py`, a focused scratchpad service module
  if needed, and `tests/test_rc10_scratchpad.py`.
- Remove tree/Git/participant prerequisites while preserving quota/key/string/
  paging behavior and optional actor audit metadata.
- Implement expiry from last write, idempotent-write replay without TTL refresh,
  namespace discovery and quota accounting excluding expired entries.
- Expose shared private/public behavior through one service. Do not retain a
  private tree-scoped store or add CAS/locking semantics.
- Test independent callers and outside-Git access, write races near quota,
  generated keys, read-only TTL behavior and namespace cleanup.

**Coordinator work.** Own `gc.py`, shared config wiring and schema adjustments
requested by either lane. Integrate scratchpad expiry sweeps and workspace
retention into one coherent GC change. Guard running/uncertain usages and keep
batching/VACUUM rules intact.

**Gate/checkpoint B06.** Shared workspace services refuse unsafe cleanup and keep
usage; linked-checkout creation uses the captured commit and correct root; public
and private scratchpad calls agree on global visibility and TTL. No test deletes
a control-plane worktree.

### Wave 07 — Launch and adoption through the selected provider

**Purpose.** Deliver the first orchestration vertical slice: reserved identity,
workspace usage, provider terminal creation and existing harness startup.

**S07 — Spawn/adoption vertical slice.**

- Own `daemon/spawning/{models,service,planning,native,frontend}.py`,
  `daemon/registry.py`, spawn/adoption portions of `daemon/rpc/participants.py`,
  `daemon/rpc/spawning.py`, and new participant launch/adoption public handlers.
- Add provider selection and persist the exact selected provider/launch facts.
  Validate availability before external side effects; no parent inheritance or
  failover to a different provider.
- Reserve participant/operation/job/workspace identities before callback
  dispatch. Correlate early MCP/hook registration with that reservation rather
  than creating a second external participant.
- Replace both ordinary pane creation and native stock-UI attachment with
  `terminal.create`; keep detached backend and per-harness listener ownership
  inside Theater and retain existing startup order.
- Implement explicit fresh adoption and attachment to an external participant
  with occupant/incarnation/native/transcript evidence gates.
- Implement the minimum generic route/identity admission needed by launch via
  the frozen control-gate interface. Coordinate any shared control-gate edit;
  do not insert a bypass to make launch tests pass. Full controls follow in wave 8.
- Preserve safe pre-execution native fallback and uncertainty behavior. Automatic
  process/workspace cleanup must never run after possibly executed creation.
- Own `test_rc10_provider_spawn.py`, `test_rc10_provider_adoption.py` and focused
  changes to existing frontend/native spawn tests.

**G07 — Catalog and diagnostic public adapters.**

- Own new public handler modules for catalogs, skills, usage/statistics and bus,
  plus dedicated `test_rc10_catalog_api.py` / `test_rc10_diagnostics_api.py`.
- Map installed harness/model/skill facts and existing diagnostics into public
  DTOs. Report installed versus launchable and provider-dependent reasons.
- Reuse read services; do not touch S07's private `rpc/spawning.py` or infer
  harness state by importing adapters inside Régie.
- Add bounded request/response validation and tests through the real public
  dispatcher, including missing/incompatible selected provider.

**Coordinator integration.** S07 first, G07 second, then registry/projection and
server wiring. Inspect every old tmux creation call site, not only the obvious
legacy spawner. Do not advertise unfinished native launch capabilities.

**Gate/checkpoint B07.** A root and child use provider-created terminals with
reserved identities and held workspace usage. Early hello, explicit adoption,
provider absence and lost create acknowledgment behave as specified. Existing
Codex/OpenCode/Pi startup regressions still exercise the supported topology.

### Wave 08 — Controls, presence, and observation surface

**Purpose.** Remove generic pane dependence while preserving the safety gates
and exposing the remaining observation/job API needed by an independent client.

**S08a — Control routing and operation linkage.**

- Own `daemon/controls/{routing,gates,service}.py`,
  `daemon/runtime/control_gates.py`, private send/control/interruption handlers,
  private participant mutation handlers and the public control/participant
  mutation handlers. G08's read handlers remain separately owned.
- Generalize the legacy terminal route to the bound provider; keep native
  capabilities and adapter-pinned fallbacks distinct.
- Use the existing control-operation queue, receipt and execution-barrier logic;
  link durable public handles without creating duplicate jobs or send sequences.
- Preserve ordinary-send permission, protected-control ownership, busy rules,
  copy-mode distinctions and supported settings/allowlists.
- Expose participant metadata/status mutations through the shared actor-aware
  service and implement termination across native backend and provider terminal
  ownership. Confirm required exits before reporting success or releasing usage;
  do not delete the retained workspace.
- Revalidate identity/policy at dispatch and keep input uncertainty from releasing
  queued follow-ups incorrectly.
- Add focused `test_rc10_provider_controls.py`; extend existing control/FIFO/
  queue tests only for changed behavior.

**S08b — Presence and lifecycle evidence, after S08a integration.**

- Reuse the S session with a new bounded assignment/base checkpoint.
- Own `daemon/presence/`, applicable observation screen/lifecycle adapters,
  `daemon/awaiting.py` where needed, and `test_rc10_provider_presence.py`.
- Replace daemon tmux inventory with provider evidence/revision refresh while
  preserving the `PresenceProvider` contract for control and await consumers.
- Missing/disconnected/stale evidence is unknown. Refresh before native delivery
  and require the provider to recheck before physical terminal mutation.
- Distinguish authoritative exit from partial inventory or lease loss. Keep
  jobs/resources held while execution remains uncertain.
- Test present/absent/unknown, focus changes immediately before delivery, native
  route with unavailable presence, and unchanged admission-time await semantics.

**G08 — Transcript, recall, participant/job observation APIs.**

- May run alongside S08a; no edits in S08b's presence/awaiting modules.
- Own public transcript/recall and participant/job read handlers, corresponding
  focused tests, and extraction of transcript read/bind domain helpers from
  `daemon/rpc/transcripts.py` when needed.
- Preserve transcript cursor budgets, trusted identity/collision checks,
  explicit operator binding, recall semantics and structured job results.
- Add operator-safe list/get/await projections without fabricated participant
  rows. Call the shared awaiting/presence service through its agreed interface.
- Keep trajectory public wrappers tied to the existing separate trajectory
  service and cursors; do not merge them into the orchestration journal.

**Coordinator gate sequence.** Integrate S08a and G08, run their combined focused
checks, then advance S08b. Keep protected routes disabled if their presence gate
is not connected; no temporary assumption of human absence.

**Gate/checkpoint B08.** Send/steer/queue/interrupt/settings use declared routes;
unknown human presence blocks protected actions; native control has no generic
tmux-pane precondition; transcript/job APIs work for operator clients; existing
control/native/await regression anchors pass.

### Wave 09 — Recover live operations and serve consistent snapshots

**Purpose.** Reconstruct state after crashes and expose a coherent read model
without losing work, ownership or resource usage.

**S09 — Restart, provider reclaim and control transfer.**

- Own provider recovery, `daemon/runtime/recovery.py`, relevant lifecycle/
  maintenance changes, a focused ownership service and control-transfer handler.
  Queue-cancellation edits in `controls/service.py` belong to S for this wave.
- Implement the spec's restart order: load durable state/barriers, restore native
  listeners/runtime identity, reconcile providers, then admit undispatched work.
- Apply every row of the crash-point matrix. Reconcile receipts/inventory from
  exact generation/operation identities; never repeat uncertain input/launch.
- Transfer a bounded explicit participant set atomically with expected owner
  revisions, cycle checks and cancellation of undispatched follow-ups.
- Preserve historical parent/authorship and already-dispatched work. Coordinator
  resume alone must not transfer authority.
- Own `test_rc10_recovery.py` and `test_rc10_control_transfer.py`; use failure hooks
  and real database reopen, not only same-process exception tests.

**G09 — Journal reader, snapshots and follow API.**

- Own new `daemon/events/` reader/snapshot/follow modules,
  `daemon/frontend/handlers/state.py`, public projection code assigned for this
  wave, and `test_rc10_state_stream.py`.
- Read event/write-unit interfaces from wave 2; do not edit S09's writers.
- Materialize an immutable bounded active projection and ending cursor in one
  read transaction. Cache pages with the specified expiry and byte limits.
- Implement complete transaction-group follow, timeout/empty batches, retention
  gaps, stream-identity mismatch and explicit resnapshot behavior.
- Close lost-wakeup races and support cursor resume after daemon restart.
- Test against controlled committed state transitions, including slow readers,
  duplicate delivery, expired pages and database replacement.

**Coordinator work.** Resolve any overlapping maintenance/GC wiring locally and
keep retention from pruning recoverable operations/held usage. Verify that
snapshot capability/presence summaries refer to committed projection revisions.

**Gate/checkpoint B09.** Restart preserves accepted work and resource ownership;
provider reclaim fences identities; explicit transfer cancels the intended queue
items; snapshot/follow is consistent for the currently instrumented transitions.

### Wave 10 — Complete event coverage and client synchronization

**Purpose.** Ensure an independent frontend sees every relevant change,
regardless of whether it originated in a public API call.

**S10 — Writer audit and event publication.**

- Own domain writer changes across registry, private RPCs, hooks, observation,
  control settlement, lifecycle/recovery and GC, plus
  `tests/test_rc10_event_coverage.py`.
- Trace all state projections to their actual writers. Route each transition
  through the write unit; add typed events/tombstones in the same transaction.
- Stage registry/job cache updates after commit and preserve notification behavior.
  Do not synthesize events later by watching the diagnostic bus.
- Group ownership transfers and related multi-record transitions atomically;
  avoid publishing unchanged heartbeat/poll traffic.
- Test private CLI/MCP/hook paths, natural completion, recovery and GC against the
  public stream. Include rollback and counter reuse after full event pruning.

**G10 — SDK snapshot/follow and operation waiting.**

- Own SDK state-follow/projection helpers and operation-wait client support,
  plus `tests/test_rc10_sdk_follow.py`.
- Implement complete snapshot assembly, atomic replacement, transaction-group
  application, cursor/revision deduplication, reconnect and resnapshot.
- Preserve unknown event kinds/fields safely. Separate trajectory and bus streams.
- Verify request cancellation only stops waits; operation handles survive
  reconnect and continue to identify accepted work.
- Compare a followed projection with a fresh snapshot at the matching revision
  using public APIs only.

**Gate/checkpoint B10.** Snapshot plus contiguous events equals fresh state for
private/public/observer/GC transitions. SDK consumers do not need polling or
private imports to repair missing orchestration information. The public API
surface inventory has a concrete implementation/test status for every method.

### Wave 11 — Extract Régie and implement the persistent tmux bridge

**Purpose.** Exercise the public contract with the real frontend and terminal
provider, while keeping daemon authority and native startup behavior intact.

**S11 — Régie tmux bridge.**

- Own `packages/regie/src/regie/bridge/`, `packages/regie/src/regie/tmux/`, and
  bridge/terminal tests under `packages/regie/tests/test_bridge_*` and
  `test_tmux_*`.
- Move/adapt terminal execution, inventory, focus/presence, safe input framing,
  interruption and termination from Theater's tmux layer into the provider.
- Implement durable bridge identity, private credentials/process lock, pinned
  tmux server, callback lifecycle, readiness, bounded reconnect and reconciliation.
- Recheck generation/occupant/presence before a queued side effect; expose
  operation receipts without claiming transport acceptance is job completion.
- Provide narrow presentation primitives needed by G11's staging controller;
  freeze their signatures before either worker builds against them.
- Keep a bridge running after TUI exit, and keep terminals alive after bridge
  stop. Reclaim exact incarnations without respawning them.
- Test under an isolated real tmux server, including server restart, pane ID
  reuse, focus changes and a lost create/send acknowledgment.
- Read old `theater/tmux/` as the source. Do not delete it in parallel with the
  extraction; retirement of remaining imports is a coordinated wave 12 step.

**G11 — Régie package and UI client migration.**

- Own `packages/regie/src/regie/` **except** `bridge/` and `tmux/`, the move of
  `theater/regie/`, presentation constants/helpers, and UI-specific tests under
  `packages/regie/tests/` excluding S11's test prefixes.
- Move controllers, widgets, trajectory presentation and resources to imports
  rooted at `regie`. Replace every private Theater import using spec §12.1.
- Implement snapshot/follow initialization, stale state on disconnect and
  resnapshot on gaps. Keep trajectory and diagnostic views on their own APIs.
- Convert launch/control/kill actions to idempotent operation handles and explicit
  pending/uncertain results. TUI teardown cancels waits, never accepted work.
- Use public catalog/capability data. Show provider-dependent actions and refuse
  staging for non-tmux bindings while keeping them visible in the tree.
- Preserve staging/session pane identity and existing UI behavior through S11's
  agreed presentation interface. No direct terminal lifecycle mutations in TUI.
- Move settings ownership to a standalone Régie config module. Final CLI command
  routing and user-facing config migration are finished in wave 12.
- Run moved focused UI/trajectory/controller tests and a static private-import
  check. Do not repair missing API data by importing Theater internals.

**Coordinator work.** Own initial `packages/regie/pyproject.toml`, root workspace
setup and export registration during this wave, so neither lane changes shared
packaging while both need it. Define and distribute bridge/presentation/config
constructor signatures before spawning. Integrate S11 and G11, then run the real
daemon–bridge–TUI smoke path.

**Gate/checkpoint B11.** Régie launches from its package against the public API;
its bridge owns tmux; TUI exit and bridge stop have distinct non-destructive
lifetimes; participant pane identity survives staging; no Régie production code
imports Theater outside `theater.frontend`.

### Wave 12 — CLI/config break and deletion of obsolete coupling

**Purpose.** Make the extracted products the only supported execution path and
remove old code that could silently restore daemon-owned tmux behavior.

This wave has two ordered assignments. G12 is integrated before S12 removes
compatibility remnants; do not race a CLI migration against deletion of imports
it still uses.

**G12 — CLI, configuration and private management adapters.**

- Own relevant `theater/cli/` commands/parser, `theater/config/`,
  `packages/regie/src/regie/cli.py` and config/paths modules, private provider/
  workspace/control-transfer management RPC adapters, and focused CLI/config tests.
- Make bare `theater` show help/migration guidance. Make old UI invocation fail
  with `regie` instructions without importing Textual or launching an alias.
- Implement `regie` startup ownership and `regie bridge start/status/stop` against
  S11's bridge process API. SDK remains connect-only.
- Reject old Theater `[regie]` config with exact destination instructions; preserve
  the moved block's supported names and do not automatically rewrite files.
- Add Theater provider/workspace/control-transfer CLI management and provider
  override on existing spawn CLI/MCP requests, retaining private transport.
- Extend the MCP spawn signature/help in the existing delegation surface for the
  provider override. Do not migrate agent MCP to public RPCs or implement the
  future operator MCP adapter.
- Test compatible/incompatible existing daemon handling, standalone bridge startup,
  stop/status without accidental autostart, and private/public semantic agreement.

**Coordinator intermediate gate.** Integrate G12 and export/register its private
management adapters. Verify entry points before authorizing removals in S12.

**S12 — Remove old coupling and prove the boundary.**

- Own retirement of obsolete `theater/tmux/` and residual `theater/regie/`
  implementation paths once all production consumers have moved; own focused
  import/physical-execution boundary tests and affected legacy test relocation.
- Audit all daemon orchestration code for terminal creation, injection,
  destruction, presence inventory and tmux-server reconciliation calls. Remove
  active direct execution paths; keep native backend process ownership intact.
- Remove generic pane/tier addressability and no-pane-means-absence fallbacks.
  Keep any temporary private wire `tier` alias as origin data only.
- Remove old automatic worktree retirement and tree-scoped scratchpad routes;
  check observer, kill, teardown, maintenance and GC callers as well as RPCs.
- Verify public adapters do not call private dispatch registries and that the
  public SDK does not import private config/client/runtime helpers.
- Move test imports/fixtures deliberately without weakening the tmux containment
  guard. Shared conftest edits transfer exclusively to S for this assignment.
- Run focused boundary, control/presence/GC regressions. Return any remaining
  cross-package API defect to the coordinator instead of widening the SDK facade.

**Gate/checkpoint B12.** The new entry points/config work; no daemon terminal
execution or Régie private imports remain; no accidental old UI alias exists;
private CLI/MCP/hooks retain their transport and intentional RC10 semantics.

### Wave 13 — Packaging and adversarial integration tests

**Purpose.** Produce a reproducible candidate and expose cross-process failures
before independent review. This is a planned implementation/test wave, not a
replacement for coordinator validation.

**S13 — Crash and cross-process acceptance tests.**

- Own dedicated `tests/test_rc10_fault_matrix.py`,
  `test_rc10_end_to_end.py`, `test_rc10_live_recovery.py` and narrowly required
  failure-injection seams agreed with the coordinator.
- Exercise commit/dispatch/receipt/settlement crash points from the technical
  spec, reopening the database and restarting candidate processes.
- Cover root/child launch, external adoption, identity replacement, provider
  disconnection/reclaim, ownership transfer, workspace retention/deletion and
  public snapshot convergence in the same candidate architecture.
- Prove no duplicate side effects using provider/native evidence. Assert
  uncertain input barriers and held resource usage survive restart and GC.
- Retain Codex/OpenCode/Pi runtime regression scenarios with their existing startup
  topology. No new terminal-free mode or production Superset adapter.
- Run only these focused integration/fault tests and selected native regression
  anchors. Submit discovered product defects as blockers for the original owner
  or coordinator; do not make unrelated packaging/UI changes.

**G13 — Packaging, CI and implementation-coupled migration documentation.**

- Own root and Régie `pyproject.toml`, `uv.lock`, `.github/workflows/{ci,release}.yml`,
  packaging smoke tests and required README/architecture/harness/config examples.
  This is the explicit transfer of shared packaging files for this wave.
- Build both wheels and sdists; declare exact Theater dependency, include schemas,
  harness/UI assets, and remove Textual from Theater runtime dependencies.
- Preserve test discovery/coverage after moving Régie tests; include supported
  Python versions and Linux/macOS-sensitive peer identity/recovery coverage.
- Add isolated wheel/sdist smoke tests for Theater without Régie/Textual and Régie
  with its matching Theater, using candidate-only homes and tmux roots.
- Ship the guarded drained-upgrade instructions, manual config move, scratchpad
  discard and worktree retention/cleanup behavior. Do not claim that RC9 worker
  cleanup already has RC10 retention semantics.
- Update the future release workflow to collect all four artifacts; do not push
  tags, publish packages, create a release or migrate the control installation.
- Run packaging/metadata/install checks and focused config/CLI tests, not the full
  suite. S13 retains ownership of fault tests and production recovery changes.

**Coordinator integration order.** S13 tests/failure seams, then G13 packaging;
regenerate/run only coordinator-owned final checks after the combined commit.
Update `AGENTS.md` ownership statements to describe daemon authority and provider
execution accurately.

**Gate/checkpoint B13.** The candidate installs independently from artifacts,
passes the focused fault matrix and has accurate upgrade instructions. Every
required check has either real evidence or an explicit unresolved environment
blocker; unavailable platform coverage is not recorded as passed.

### Wave 14 — Coordinator validation and independent reviews

**Purpose.** Evaluate the integrated candidate rather than trusting cumulative
worker reports.

**Coordinator before reviewers.** Create a reviewable checkpoint commit, run full
lint/format-check/typecheck/pytest/coverage and migration/artifact checks in the
integration environment, and record exact commands/commit/results. Resolve known
failures before asking reviewers to assess a candidate that is already broken.
Do not run multiple full suites simultaneously in worker worktrees.

Spawn two fresh reviewers at that exact checkpoint, each with `worktree=true`
and `approval="yolo"` but a **read-only assignment**. They may inspect and run
focused tests, but cannot edit files, reformat, commit fixes or invoke cleanup.

**RS14 — Independent correctness/recovery review, profile S.**

- Review the full baseline-to-candidate diff with priority on write-unit atomicity,
  idempotency, execution barriers, provider generation/occupant fencing, native
  startup/control behavior and fail-closed presence.
- Inspect operation-versus-job meaning, coordinator ownership transfer, dispatch
  races and every destructive workspace path.
- Reproduce suspected failures with focused tests in the isolated candidate
  environment; do not run the full suite or modify tests to obtain a pass.
- Return severity, file/symbol, concrete failure sequence and evidence for each
  finding; no report file and no implementation commits.

**RG14 — Independent contract/extraction/release review, profile G.**

- Review public catalog/schema/runtime conformance, raw client portability,
  snapshot/event coverage, SDK unknown-value behavior and role boundaries.
- Inspect Régie import boundaries, resource packaging, CLI/config break and
  provider/TUI/process lifetime separation.
- Verify migration refusal ordering, scratchpad discard/global TTL, retained
  worktrees and actual isolated package install evidence.
- Return actionable findings with the same evidence requirements, without seeing
  the coordinator's conclusions or treating RS14 as an authority.

Give both reviewers the original request, both plans, repository invariants,
baseline/candidate SHAs and actual validation evidence. Do not prime them with a
claim that the refactor is correct. Their scopes prioritize attention; either
may flag a substantiated blocker anywhere in the diff.

**Review handoff R14, not acceptance.** The coordinator independently verifies
and triages every finding, then enters the correction/recheck loop in wave 15.
Disputed findings require code/evidence and decisive checks. Final approval
requires no substantiated correctness, safety, architecture or acceptance blocker;
reviewer silence, a completed Theater job or untested confidence is not acceptance.

### Wave 15 — Corrections, final verification and implementation handoff

**Purpose.** Close review findings on the final integrated commit and leave work
recoverable before any worker lifecycle cleanup.

For localized findings, reuse the original implementation worker only if it has
useful context, a compatible clean branch and remaining correction rounds.
Otherwise the coordinator implements the fix. Do not create new fixing workers.
Reviewers remain read-only; ask the same reviewers to recheck the affected diff.

After fixes, the coordinator runs final validation on the exact final commit.
Broaden testing where changed failure paths justify it; reuse unchanged check
evidence only when its applicability to the final tree is explicit. Record final
commit, artifacts and any genuinely unmet gate.

Do not transition the control daemon to RC10 as part of declaring the code ready.
Its running coordinator/workers make it undrained. Production upgrade and release
publication remain separate actions with their own concrete reviewable result.

The final user handoff names the integrated branch/commit, validation, remaining
limitations and workers still available. Follow section 8 before terminating any
worker or removing a worktree.

**Final checkpoint B15.** The exact corrected integration commit satisfies the
technical specification, independent review and final validation gates in §8.2.
If a required check or blocker remains unresolved, retain the candidate and
report that limitation instead of declaring acceptance.

## 6. Theater tool procedure and prompt templates

### 6.1 Before assigning a wave

The coordinator uses the running **control** daemon's tools:

1. `whoami` to confirm coordinator identity and registered cwd.
2. `list_harnesses` and `list_models` to verify both exact profiles/efforts.
3. `list_participants(children_only=true)` to reconcile already running workers.
4. Read Git state and ledger to find the last accepted checkpoint and outstanding
   commits before spawning or reusing anyone.
5. Update coordinator/worker metadata with `update_participant` so the TUI shows
   the current wave/task rather than a stale name or description.

Do not replace an unavailable requested model with a default. Keep independent
coordinator work moving, but leave dependent assignments blocked and explain the
exact catalog/startup mismatch if a user choice becomes necessary.

### 6.2 Spawn templates

The following are argument examples, not instructions to execute while writing
the plan. Substitute the exact checkpoint SHA, full task prompt and actual run
metadata. `wiring="auto"` uses the stable control installation's supported routing;
it does not opt the worker into candidate RC10 runtime code.

Profile S:

```json
{
  "harness": "codex",
  "model": "gpt-5.6-sol",
  "reasoning_effort": "xhigh",
  "approval": "yolo",
  "worktree": true,
  "cwd": "/Users/manaiki.laut/Desktop/theater",
  "base_branch": "<full-checkpoint-sha>",
  "wiring": "auto",
  "name": "rc10-s",
  "description": "RC10 S03: public handshake, role isolation and request validation",
  "prompt": "<complete task handoff from section 6.3>"
}
```

Profile G:

```json
{
  "harness": "pi",
  "model": "mistral/zai-glm-5-3",
  "reasoning_effort": "high",
  "approval": "yolo",
  "worktree": true,
  "cwd": "/Users/manaiki.laut/Desktop/theater",
  "base_branch": "<full-checkpoint-sha>",
  "wiring": "auto",
  "name": "rc10-g",
  "description": "RC10 G03: connect-only SDK and independent raw socket client",
  "prompt": "<complete task handoff from section 6.3>"
}
```

Names must be unique among live participants. Add a run suffix if previous work
already owns those aliases. Record stable IDs rather than later assuming an
alias still names the same participant.

If spawn returns an error/connection loss, inspect participant/job/worktree state
before retrying. A missing response is not proof no worker was created. Never
create a second worker while a possible first one is unaccounted for.

### 6.3 Required implementation prompt

Each task card above is expanded into a handoff with this structure. The
coordinator supplies exact current paths/symbols and useful line references from
the checkpoint; line numbers from the historical baseline alone are insufficient.

```text
You are implementing <task-id> in wave <wave>, profile <S or G>.

Goal: <one concrete outcome from the task card>.
Base: <full SHA>. Interface revision: <committed schema/interface revision>.
Before editing, verify your isolated worktree is clean and at this base.
For an explicitly reused clean branch, fast-forward to this SHA; never reset
or discard unaccounted changes. Report a mismatch before editing.

Read:
- AGENTS.md and applicable repository instructions.
- docs/rc10-implementation-plan.md sections <exact sections>.
- docs/rc10-theater-execution-plan.md, your task card and operating rules.
- <exact existing files/symbols and short necessary snippets>.

Owned files: <closed list/directories, including task-specific test files>.
Forbidden: <shared files, sibling paths, Superset, control runtime/config>.
Interfaces you must implement/use unchanged:
<verbatim signatures, DTO fields, error codes and transaction/callback rules>.

Implementation steps:
<the task card's ordered requirements, narrowed to this assignment>.

Invariants:
- Stable control Theater stays RC9; candidate processes use isolated test homes.
- No inferred presence, identity, cleanup authority or replay of uncertain input.
- No silent API/model/approval/provider changes.
- No nested agents, full suite, unrelated formatting, release or worker cleanup.

Focused validation:
<exact commands and expected scenarios, through the approved isolated runner>.
If blocked by an interface/environment problem, state evidence; do not fake a pass.

Commit the owned implementation and focused tests. Leave the worktree clean.
Do not create report files. Return only commit(s), changed paths, checks and blockers.
Do not start the next task until the coordinator sends its checkpoint and ownership.
```

The prompt must be self-contained even when the worker can read both documents.
Sending only “implement W07” forces the worker to reconstruct scheduling, ownership
and subtle invariants that the coordinator already knows.

### 6.4 Structured implementation handoff

Use `response_format` on supported initial spawns or `send` calls for an
implementation result. Current Theater treats it as prompt guidance and parses
the final JSON; it does not validate the schema or prove correctness.

```json
{
  "type": "object",
  "required": ["task", "base", "commits", "changed_paths", "checks", "blockers"],
  "properties": {
    "task": {"type": "string"},
    "base": {"type": "string"},
    "commits": {"type": "array", "items": {"type": "string"}},
    "changed_paths": {"type": "array", "items": {"type": "string"}},
    "checks": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["command", "outcome"],
        "properties": {
          "command": {"type": "string"},
          "outcome": {"type": "string", "enum": ["passed", "failed", "blocked"]},
          "detail": {"type": "string"}
        },
        "additionalProperties": false
      }
    },
    "blockers": {"type": "array", "items": {"type": "string"}},
    "interface_requests": {"type": "array", "items": {"type": "string"}}
  },
  "additionalProperties": false
}
```

Require plain final JSON without fences when using this hint. A parsing failure
does not mean the commit is invalid or absent; inspect the bounded transcript and
actual branch. Reviews can return normal structured findings without this
implementation schema because they produce no implementation commit.

### 6.5 Waiting, progress, corrections and next assignments

Use `theater_wait.await_sessions(handles=[...], max_wait=290)` for the active
wave's job handles. It returns on available completion; a timeout only stops
waiting. Continue awaiting the same unfinished handles rather than spawning a
replacement or issuing repeated short “are you done” prompts.

Use a yielding/resumable tool call so a 290-second server wait does not block user
communication. For example, an execution wrapper may yield after one second and
resume the same running wait in bounded intervals. Do not launch overlapping
awaits for the same handles. If the actual client imposes a shorter hard timeout,
use the longest safe supported duration and document that transport constraint.

While waiting, review a completed sibling, prepare integration checks or inspect
known blockers. Send concise updates at meaningful boundaries and during long
work; absence of a completed job is not evidence that a worker is stalled.

Use `read_transcript(target=<id>)` for the newest bounded page when a handoff is
missing or behavior needs diagnosis. Continue only with returned cursors if the
needed evidence is older. Do not repeatedly read the entire conversation.

Use `send` for an idle worker's correction or next assignment and retain its
returned job handle. If a working task must change, inspect controls/state and
use the supported Theater steer/interrupt path deliberately. Do not queue the
next wave before the current gate passes: queued work must not begin on a stale
checkpoint simply because the worker became idle.

### 6.6 Focused check commands

G00's utilities must include a candidate subprocess runner, for example
`tests/rc10_support/run_candidate.py`, with a documented argv interface. It sets
the isolated environment for the child command and preserves the parent worker's
control environment. The exact runner invocation is frozen at B00.

Commands supplied to that runner use the worker's own environment and checkpoint
lockfile. Representative task commands after their files exist:

```text
uv run --frozen pytest tests/test_rc10_handshake.py tests/test_rc10_public_dispatch.py
uv run --frozen pytest tests/test_rc10_operations.py tests/test_rc10_idempotency.py
uv run --frozen pytest tests/test_rc10_providers.py tests/test_rc10_provider_fencing.py
uv run --frozen pytest tests/test_rc10_workspaces.py tests/test_worktree.py
uv run --frozen pytest tests/test_rc10_scratchpad.py
uv run --frozen pytest tests/test_rc10_recovery.py tests/test_rc10_control_transfer.py
uv run --frozen pytest tests/test_rc10_event_coverage.py tests/test_rc10_sdk_follow.py
```

The coordinator supplies each worker only its own focused subset and relevant
existing regression anchors from technical spec §14.1. When test paths move in
wave 11, update commands to the committed Régie workspace paths. Run targeted
Ruff/type checks on owned changes where applicable; never a whole-tree formatter.

The coordinator alone runs the full suite, combined coverage, all-project type
checks and the complete release matrix. Worker “passed” results are evidence to
inspect, not a substitute for validating the integrated tree.

## 7. Integration, failures, and corrections

### 7.1 Acceptance procedure for every worker commit

The coordinator performs this procedure before advancing a worker or wave:

1. Confirm the reported commit exists on the recorded worker branch and descends
   from the assigned base. Compare actual changed paths with the ownership list.
2. Inspect the complete relevant diff and commit history, including generated or
   untracked files. A clean reported result does not excuse changes outside scope.
3. Review the actual invariants affected: transaction use, route/identity/presence
   checks, callback uncertainty, retained usage, schema conformance or package
   boundaries as applicable. Read surrounding code, not only added lines.
4. Inspect focused test content and results. Verify tests exercise behavior or
   failure boundaries rather than merely reproducing the implementation.
5. Reproduce the decisive focused checks in the integrated candidate environment
   when needed; record environmental failures separately from product failures.
6. Merge only the exact accepted commit into the integration branch. Apply small
   coordinator-owned composition changes and run the combined wave gate.
7. Record the integration SHA and gate evidence; then authorize the next wave.

Useful read-only Git checks during implementation include:

```text
git show --stat <worker-commit>
git diff --name-status <assigned-base>..<worker-commit>
git diff --check <assigned-base>..<worker-commit>
git merge-base --is-ancestor <assigned-base> <worker-commit>
```

Do not run these with placeholder text, trust a branch label instead of a SHA,
or accept a worker's final prose as proof that the commands/tests passed.

### 7.2 Two correction rounds, then coordinator ownership

If a submission fails review, send the original worker one precise correction:
concrete defect, path/symbol, expected behavior, required evidence and allowed
files. Preserve the original acceptance criteria. The initial implementation
submission is not a correction round.

After that correction, re-review the real commit. If still incorrect, send one
final correction request. After two correction rounds for the assignment, the
coordinator completes the correction locally. Do not repeatedly respawn another
worker to fix the same misunderstood change.

A genuine missing requirement clarified before a submission is not a correction
round. An interface change affecting siblings must go through the interface
checkpoint procedure, not be hidden in a single worker's repair prompt.

For integration defects discovered after both worker commits were accepted,
identify ownership from the failure path. Reuse the original worker only when
its branch can safely incorporate the integration checkpoint and it still has
useful context/remaining rounds. Otherwise fix in the integration branch.

### 7.3 Failure handling matrix

| Situation | Required coordinator response | Do not do |
|---|---|---|
| Requested profile not in catalog or startup fails | Record exact model/effort/authentication evidence; keep dependent lane blocked and request a choice only if needed | Omit the model or silently use another one |
| Spawn response lost | Inspect participants, handles, worktrees and narrow transcript evidence; identify whether a child exists | Blindly spawn a duplicate |
| Await times out | Re-await the same outstanding handles; inspect only if there is evidence of a problem | Treat timeout as crashed/killed or launch a replacement |
| Worker appears stalled | Inspect status, controls, narrow transcript, branch and process identity; check human presence/approval/error state | Send repeated prompts or kill before preserving work |
| Worker hits an ownership boundary | Coordinator resolves the interface/file transfer and updates affected assignments | Allow both lanes to edit a shared file |
| Worker commits outside scope | Review/quarantine those changes; request a bounded correction or integrate only after deliberate reassignment/review | Merge the whole branch because tests passed |
| Worker exits with uncommitted work | Inspect and preserve its worktree/diff before any lifecycle action; recover through the coordinator | Assume no commit means no useful work |
| Lost terminal/native acknowledgment | Follow control-daemon state and uncertainty semantics; inspect before any retry | Assume the message was not delivered and send it again |
| Same-wave merge conflict | Stop gate advancement; resolve semantics and rerun affected combined checks | Resolve mechanically and start downstream workers |
| Required platform/runtime unavailable | Record an unmet gate and exact environment need; continue independent checks | Claim Linux/macOS/native proof from a mock-only run |
| Candidate touches control home/server | Stop candidate work, preserve evidence and assess control state before resuming | Continue testing against the live control installation |
| Coordinator context/session lost | Recover ledger/Git/Theater identities before resuming; respect RC9 parent authority | Claim a fresh participant automatically owns the old children |

Use interruption only as a deliberate control action within the current task's
authority and supported gates. It is not a substitute for understanding whether
work was applied. Permanent worker termination is a separate step in section 8.

### 7.4 Coordinator recovery under RC9

The run ledger must be sufficient for a resumed coordinator to find all worker
commits and outstanding operations without relying on a scratchpad or chat
summary. Before sending more work, reconcile:

- Actual control daemon/installation identity and whether its participants live.
- Current coordinator participant ID and direct-child ownership.
- Each recorded worker's stable ID, harness session identity, status and worktree.
- All branch heads and accepted/unaccepted commits against the integration SHA.
- Outstanding spawn/send job handles and any undispatched queued work.
- Last completed gate, contract revision and current file ownership.

RC10's planned ownership-transfer API is not available in the RC9 control plane.
If a new coordinator participant cannot administer the previous one's children,
do not use an unapproved shell workaround or pretend ownership transferred.
Prefer resuming the existing coordinator identity where supported; otherwise
seek the required local-operator recovery action with the concrete affected IDs.
Independent read-only Git review can continue while that issue is resolved.

The new global scratchpad design is likewise not a recovery mechanism for this
implementation run. Its data is being developed in candidate homes, and the RC9
scratchpad may disappear with its tree. Git history and the durable ledger are
the recovery anchors.

### 7.5 Work package traceability

Before marking a wave complete, check the corresponding source specification
package instead of treating this schedule as permission to omit a requirement.

| Technical package | Execution coverage | Acceptance owner |
|---|---|---|
| W01 contract/DTO/schema | Wave 01, contract completeness check in 10/14 | Coordinator, RS14/RG14 |
| W02 transactional persistence/migration | Wave 02, fault coverage in 13/14 | Coordinator, RS14 |
| W03 provider/binding/ownership/workspace/operation records | Wave 02, exercised by 04–10 | Coordinator, RS14 |
| W04 public dispatcher/SDK/full method surface | Waves 03, 07, 08, 10 | Coordinator, RG14 |
| W05 operations/idempotency | Wave 04, integration in 07–10, faults in 13 | Coordinator, RS14 |
| W06 provider transport and portable fixture | Waves 04–05 | Coordinator, both reviewers |
| W07 launch/adoption/routing/presence | Waves 07–08 | Coordinator, RS14 |
| W08 workspaces/global scratchpad/GC | Wave 06, launch/teardown integration in 07–12 | Coordinator, both reviewers |
| W09 restart/reclaim/control transfer | Wave 09, fault testing in 13 | Coordinator, RS14 |
| W10 journal/snapshot/all-writer coverage | Waves 09–10 | Coordinator, both reviewers |
| W11 extraction/real tmux provider | Wave 11, boundary cleanup in 12 | Coordinator, both reviewers |
| W12 UI/CLI/config break | Waves 11–12 | Coordinator, RG14 |
| W13 packaging/documentation/release checks | Waves 13–15 | Coordinator, both reviewers |

No package is complete merely because its owner submitted a commit. Traceability
requires the integrated behavior and its exit evidence.

## 8. Final review, acceptance, and shutdown

### 8.1 Review prompt requirements

Fresh RS14 and RG14 sessions receive a read-only prompt containing:

```text
Review the integrated RC10 candidate at <candidate SHA> against <baseline SHA>.
Use profile <S or G>; your task is read-only independent review.

Read both RC10 plan documents and AGENTS.md. Inspect the real diff and surrounding
code. Your priority scope is <RS14 or RG14 card>, but report any substantiated
correctness, safety, architecture or acceptance blocker you find.

Validation already run on this exact commit:
<commands, results and limitations, without the coordinator's conclusions>.

Use the isolated candidate environment for focused reproduction only.
Do not edit tracked files, commit fixes, run the full suite, spawn agents,
publish a report file or modify/stop any worker/control process.

Return findings with severity, file/symbol, failure sequence, evidence and
required correction. Distinguish verified defects from unverified concerns.
State what you inspected and any material coverage limit. A clean review must
not imply tests or platforms you did not exercise.
```

Configure review test caches/output outside tracked source as needed. A reviewer
using `approval="yolo"` still has no authority to implement a fix. It remains
available to recheck the coordinator/original worker's corrections.

### 8.2 Resolve findings and decide readiness

The coordinator reproduces or verifies each finding and records its disposition.
For disputed findings, exchange concrete code/test evidence with the same
reviewer. After two exchanges without convergence, run a decisive check where
possible. Escalate a material unresolved blocker to the user with the precise
tradeoff; the coordinator decides nonblocking preferences with a short rationale.

Do not prolong style debates or weaken an approved invariant to obtain agreement.
Do not treat two reviewers agreeing as a substitute for evidence. After fixes,
request re-review of affected paths and re-run appropriate final checks.

The implementation is ready only when:

- Every technical-spec acceptance item and work package has integrated evidence.
- Required full checks pass at the final commit, including independent installs,
  migration refusal/live recovery, real tmux and applicable native regressions.
- All substantiated review blockers are resolved, with material limits reported.
- Source, schemas, CLI/config behavior and release artifacts agree.
- No Superset implementation, operator MCP adapter implementation or remote
  transport has entered the scope.
- Accepted worker work is reachable from the integration branch before cleanup.

If an environment gate remains impossible to run, report the implementation as
awaiting that verification, not fully accepted. Producing the code and producing
the evidence are separate responsibilities.

### 8.3 Preserve work before cleanup

Keep implementation workers and review sessions available through final review.
Before requesting any permanent termination, the coordinator verifies that:

1. All accepted commits are reachable from the integration branch or another
   deliberately retained branch.
2. Uncommitted or rejected-but-recoverable work has been inspected and preserved
   where needed; nothing useful exists only in a soon-to-be-deleted worktree.
3. No outstanding correction or reviewer recheck still needs that session.
4. Stable direct-child IDs and current names are recorded for the proposed cleanup.

The repository's
[`theater-orchestrate` skill](../theater/skills/builtin/theater-orchestrate/SKILL.md)
requires: **“After the work is complete, name every direct child you intend to
kill and ask once.”** The current
[`put_child_back_in_the_wound` tool contract](../theater/mcp/server.py) also
requires an explicit yes for the named direct children and warns that RC9 unique
worktrees and branches are removed on kill.

Therefore, after work is concrete, reviewed and preserved, ask once naming every
direct child proposed for termination. Explain that this confirmation comes
from the Theater lifecycle contract and that RC9 cleanup is irreversible.
An explicit answer authorizes the named set; execute those authorized calls
without asking again between children. Do not bypass the rule with `theater kill`
or raw tmux/process commands. If the user does not authorize termination, leave
the sessions/worktrees available and say so.

For the current tool catalog the permanent-termination method is
`put_child_back_in_the_wound(target=<stable-id>)`. Use the discovered tool catalog
at execution time rather than guessing a `kill_session` method. Do not remove
candidate or worker directories until their owned processes are confirmed stopped.

No cleanup permission is needed to write this execution plan: no workers are
launched or terminated by the documentation task.

### 8.4 Final implementation handoff

Report the integration branch and final SHA, the completed RC10 behaviors,
validation evidence and any remaining risk or blocked gate. Link the technical
and execution plans so the user can trace decisions to implementation waves.

State separately whether workers are still alive, whether cleanup was authorized
and completed, whether artifacts were only built or also published, and whether
the user's control installation remains on RC9. Never imply publication or a
production upgrade merely because the refactor passed its candidate tests.
