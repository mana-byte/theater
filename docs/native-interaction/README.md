# Native harness interaction roadmap

Status: implementation plan, not a statement that every listed capability is
already shipped.

## Decision

The next product step should be to deepen Theater's cross-harness local control,
not to become a cluster scheduler. The differentiated layer is a common,
identity-safe control plane over the stock interactive CLIs that users already
run. Cluster scheduling is useful, but mature job schedulers and agent platforms
already cover that problem; recreating them would not remove the harness-specific
integration work that Theater is unusually well placed to do.

The native-interaction direction is feasible, but not uniform:

- **Codex** already proves the complete architecture. Maintain it as the reference.
- **OpenCode** is the best next implementation. Its public TUI plugin gets the
  current route, client, event bus, and state while preserving the stock UI.
- **Pi** exposes useful public controls, but its extension API does not yet prove
  the exact turn identity Theater needs. Run the proof spike before implementation.
- **Vibe** has an excellent app-server protocol, but the stock TUI already owns its
  single attached runtime. Do not build a multiplexer without upstream support.
- **Claude Code** exposes hooks and SDK/headless controls, but no verified public
  attachment API for an existing stock TUI. Keep native control proof-gated.

This is therefore an adapter program with explicit stop conditions, not a promise
to eliminate key injection for all harnesses.

## Scope and non-goals

In scope:

- replace `tmux send-keys` with a harness's public native interaction layer where
  that layer can acknowledge the exact operation;
- preserve the stock, human-usable TUI;
- map Theater's harness-neutral capabilities to each harness's native semantics;
- retain legacy delivery per capability when no safe native route exists;
- expose which route and setting fields are actually supported;
- pin every native path to tested upstream releases and fail closed elsewhere.

Out of scope:

- implementing cluster placement, distributed execution, or subscription pooling;
- maintaining forks of harnesses or calling undocumented private internals;
- treating screen scraping as an interaction API;
- replacing a stock TUI with a headless process without an explicit opt-in mode;
- claiming that ACP is the architecture. ACP is one possible transport when its
  semantics satisfy a specific Theater capability.

## Why this is the next step

The alternatives do not improve the same core proposition:

- **Cluster orchestration** raises the concurrency ceiling, but it moves Theater
  into scheduling, credentials, sandboxing, networking, and fleet operations. It
  does not solve how one agent safely talks to a running Codex/OpenCode/Pi/Claude/
  Vibe session. Cluster execution can later consume Theater's control contract;
  it should not precede it.
- **ACP-only integration** would be simpler but excludes harnesses or capabilities
  whose ACP support is absent or narrower than their interactive UI. Keep ACP as a
  per-capability adapter, not a lowest-common-denominator product boundary.
- **More terminal automation** expands coverage but cannot provide native
  acknowledgement, exact turn identity, or reliable recovery after timeout. It is
  a necessary fallback, not the differentiator to deepen.
- **Replacing every TUI with a Theater-owned headless runtime** can offer clean
  APIs, but removes the human interface that makes local collaboration useful.

The evidence for investing is concrete rather than aspirational: Codex already
works end to end with this model, OpenCode exposes the needed public in-TUI client,
and Pi exposes a plausible metadata-bearing entry path. The plan remains bounded by
explicit stop conditions for the two harnesses whose attachment topology does not
currently qualify.

## Capability map

“Gated” means there is a plausible public API but the required identity or
attachment proof is missing. “Legacy” means the current key path remains selected.

| Harness | New turn | Follow-up queue | Steer active turn | Interrupt | Settings | Native observation/session | First-wave decision |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Codex | Native `turn/start` | Theater queue to native send | Native `turn/steer` | Native `turn/interrupt` | Native update + readback | Native thread/turn/item events | Shipped reference; harden compatibility |
| OpenCode | Public SDK `session.promptAsync` | Theater queue to native send | Not equivalent: busy prompt becomes a future input | Public SDK `session.abort`, exact-turn gated | Unavailable initially | TUI route/state/event API | Implement send/queue first; then prove interrupt |
| Pi | Public extension send APIs, identity gated | Gated with send | Public `sendUserMessage(..., { deliverAs: "steer" })`, identity gated | Public `ctx.abort()`, identity gated | Reasoning update already native; model remains proof-gated | Frontend bridge snapshot/events | Run correlation spike; implement only if it passes |
| Vibe | App-server `turn/start` | App-server `turn/queue` | Exact app-server `turn/steer` | Exact app-server `turn/interrupt` | Protocol-dependent | Strong typed session/turn events | Blocked by single-client stock-TUI topology |
| Claude Code | SDK/headless input exists, stock-TUI attachment unproven | Gated | Gated | SDK cancellation exists, stock-TUI attachment unproven | Gated | Hooks/transcripts; SDK when it owns the session | Monitor/prove only; retain legacy controls |

Detailed plans: [Codex](codex.md), [OpenCode](opencode.md), [Pi](pi.md),
[Vibe](vibe.md), and [Claude Code](claude.md).

## Architectural contract

### Transport precedence

Choose a route independently for each capability:

1. a release-verified native backend or stock-frontend API;
2. ACP, only when it supplies the identity, acknowledgement, and semantics that
   capability requires;
3. the existing guarded tmux path;
4. unavailable.

A harness need not use one transport for everything. For example, Pi may use a
native settings method while send and interrupt remain legacy. The existing
[`RuntimeManifest`](../../theater/harness/contracts/runtime.py#L788) already
supports `legacy_fallback` and `unavailable_capabilities` per capability, and
[`ControlRouter`](../../theater/daemon/controls/routing.py#L45) performs the
selection.

### Identity and acknowledgement

Every mutating native request carries a Theater `operation_id`. A request is:

- `ACCEPTED` only after the native API acknowledged admission and Theater
  validated the returned session/turn identity;
- `REJECTED` only when the harness establishes that the mutation was not applied;
- `UNKNOWN` when the connection, timeout, malformed reply, or session transition
  prevents Theater from knowing whether it was applied.

`UNKNOWN` is terminal for delivery. Never replay it, never translate it to a
legacy key injection, and never dispatch the queued item a second time. The
existing once-only frontend request implementation already records this rule in
[`FrontendRequests`](../../theater/daemon/harness_runtime/frontend_requests.py#L33).

Session checks are required at both ends:

- the daemon compares the runtime snapshot with the participant's trusted native
  session;
- the in-process adapter compares the request's `native_session_id` with the
  currently visible/attached session immediately before mutation;
- any await between validation and mutation must be treated as a race boundary and
  followed by another validation, unless the upstream operation itself provides an
  atomic expected-session guard.

Steer and interrupt additionally require the exact active `native_turn_id`.
“This session is busy” is not enough: a stale interrupt must not hit the next turn.
The service already enforces exact steering at
[`ControlService.steer`](../../theater/daemon/controls/service.py#L631) and exact
native interruption at
[`ControlService.interrupt`](../../theater/daemon/controls/service.py#L1440).

### Stock UI and ownership

The adapter must run as either:

- a detached native backend to which the vendor's stock UI officially attaches,
  as Codex does; or
- an official extension/plugin inside the existing stock UI, as OpenCode and Pi
  allow.

The adapter may not create a second session that merely looks like the session in
the pane. The daemon remains the only component allowed to choose routing, mutate
Theater state, or inject legacy keys. A frontend extension may call only its own
harness's public API after an authenticated request from the daemon.

## Shared prerequisites

### P0 — make legacy interrupt atomic with queue dispatch

Fix this before expanding native controls. The legacy path performs presence and
pane checks, then separately calls
[`cancel_queued_followups`](../../theater/daemon/controls/service.py#L1554) before
delivering keys in
[`interruption.py`](../../theater/daemon/rpc/interruption.py#L91). Because queue
dispatch uses the participant control lock elsewhere, these operations can
interleave.

Required change:

1. Move legacy interrupt admission into `ControlService` or add one service method
   that owns the participant control lock for the complete operation.
2. Under that same lock: refresh absence, verify pane identity, re-read the
   participant, check `WORKING`, reject copy mode, refresh absence again, cancel
   queued follow-ups, and deliver the interrupt keys.
3. Do not await a helper that independently reacquires the same non-reentrant lock.
   Add a lock-held queue-cancellation helper if needed.
4. Persist/broadcast the interrupt only after key delivery succeeds. A failed key
   delivery must not report `interrupted: true`.

Minimum race test: hold legacy key delivery, run a queue dispatch concurrently,
and prove no follow-up can be admitted between queue cancellation and the
interrupt. Cover the already-idle case as well.

Primary files:

- [`theater/daemon/rpc/interruption.py`](../../theater/daemon/rpc/interruption.py#L73)
- [`theater/daemon/controls/service.py`](../../theater/daemon/controls/service.py#L1440)
- [`tests/test_interrupt_rpc.py`](../../tests/test_interrupt_rpc.py)
- [`tests/test_legacy_queue_dispatch.py`](../../tests/test_legacy_queue_dispatch.py)

### P1 — report the selected transport

The public capability report currently says whether a capability is available,
but [`_effective_capabilities`](../../theater/daemon/rpc/controls.py#L155) discards
whether the route is native or legacy. Add an additive `transport` field to each
entry:

```json
{
  "send": {
    "available": true,
    "transport": "native_runtime",
    "runtime_host": "frontend",
    "reason": null,
    "detail": null
  }
}
```

Use the existing `ControlTransport` values: `native_runtime`, `legacy_tmux`, or
`null` when unavailable. Add `runtime_host` (`detached_backend` or `frontend`) when
the selected route is native. Do not infer these values from capability availability
in clients. Add `acp` to the transport contract only with the first real ACP route,
not speculatively.

Primary files:

- [`theater/daemon/rpc/controls.py`](../../theater/daemon/rpc/controls.py#L60)
- [`tests/test_control_rpc.py`](../../tests/test_control_rpc.py)
- [`tests/test_mcp_controls.py`](../../tests/test_mcp_controls.py)
- CLI/régie rendering only where capability details are already shown.

### P2 — report supported setting fields

[`RuntimeSettings`](../../theater/harness/contracts/runtime.py#L208) contains
effective values but cannot distinguish “supported and currently `None`” from
“unsupported”. Extend the capability report, not necessarily the settings value
object, with explicit fields:

```json
{
  "settings_update": {
    "available": true,
    "transport": "native_runtime",
    "runtime_host": "frontend",
    "supported_fields": ["reasoning_effort"]
  }
}
```

If the support set belongs in the runtime contract, introduce a small immutable
`RuntimeSettingsCapabilities` value and include it in `RuntimeSnapshot`. Preserve
backward-compatible defaults. Validate that a request containing an unsupported
field is rejected before any native mutation.

Primary files:

- [`theater/harness/contracts/runtime.py`](../../theater/harness/contracts/runtime.py#L208)
- [`theater/daemon/rpc/controls.py`](../../theater/daemon/rpc/controls.py#L155)
- [`tests/test_harness_runtime_contracts.py`](../../tests/test_harness_runtime_contracts.py)
- [`tests/test_control_rpc.py`](../../tests/test_control_rpc.py)

### P3 — retain the frontend once-only protocol

The authenticated Unix frontend connection already supports bounded request and
response frames through
[`UnixFrontendConnection.request`](../../theater/daemon/harness_runtime/frontend.py#L53).
Reuse it for OpenCode and Pi. Do not add a parallel socket protocol.

Every rendered frontend handler must:

- validate method, frame size, `operation_id`, session identity, and capability;
- keep a bounded operation receipt cache for duplicate IDs;
- serialize mutations that can race with session transitions;
- answer an exact duplicate with its cached terminal reply;
- answer an in-progress duplicate without starting another mutation;
- clear or scope receipts on session/bridge epoch changes;
- make errors explicit and bounded.

## Delivery sequence

1. **Core safety and reporting:** P0, P1, and P2.
2. **OpenCode send:** bidirectional TUI adapter, exact message lineage, native
   send and Theater queue dispatch. Keep native interrupt gated.
3. **OpenCode interrupt:** enable only after active assistant/turn correlation and
   stale-turn rejection pass the stock-release test.
4. **Pi proof spike:** demonstrate whether public lifecycle events can correlate a
   Theater operation to a persisted Pi turn across busy, reload, and session switch.
5. **Pi controls if proven:** otherwise leave the existing native settings-only
   design intact.
6. **Vibe and Claude proof monitoring:** implement only when an official
   stock-TUI attachment surface satisfies the gates in their plans.
7. **Codex maintenance:** test new releases one at a time and extend the exact
   verified-version set only after conformance passes.

Do not parallelize first-wave control implementation across all harnesses. It would
duplicate protocol mistakes before OpenCode establishes the second working adapter
shape.

## Change surface

Most behavior remains in harness plugins. Shared changes should be small and made
once:

| Area | Expected production scope |
| --- | --- |
| Shared daemon | interrupt/queue locking in `daemon/controls` and `daemon/rpc`; additive capability transport reporting |
| Shared contracts | optional settings-field support metadata; no harness-specific method names |
| OpenCode | rendered frontend, runtime, live source, compatibility probe, manifest |
| Pi | existing rendered bridge/runtime/manifest, only after the proof fixture passes |
| Codex | compatibility policy/fixtures and narrow dialect changes for verified releases |
| Vibe | no production files until attachment proof; then a plugin-local runtime/front-end package |
| Claude | no production files until attachment proof; hooks remain observation-only |

No database migration should be needed for OpenCode or Pi if the current control
operation and native-evidence records can store their IDs as designed. If an
implementation discovers that durable queue ownership or correlation needs a new
column, stop and write the Alembic migration explicitly; do not overload payload
text or derive durable state from in-memory caches.

## Cross-harness acceptance matrix

Each capability implementation must exercise the applicable cells. Unit fakes are
necessary but not sufficient: the final release gate runs against the pinned stock
harness binary.

| Scenario | Required proof |
| --- | --- |
| Idle send | One native user input is admitted; receipt has exact operation/session identity; one Theater job maps to one native turn |
| Busy ordinary send | Rejected without mutation unless the harness defines ordinary send as queueing and Theater explicitly adopts that semantic |
| Theater follow-up queue | Item remains Theater-owned until dispatch; dispatch produces exactly one native submission |
| Native/busy queue semantics | Document whether native queueing can be cancelled and distinguish it from active-turn steering |
| Session switch before admission | Reject as `wrong_session`/`session_changed`; no input reaches either session |
| Session switch after uncertain write | Return `UNKNOWN`; never replay or use legacy fallback |
| Human present | Native calls remain safe because they do not inject keys; legacy fallback still obeys presence and copy-mode gates |
| Disconnect before write | Reject if the adapter proves no mutation occurred |
| Disconnect or timeout after write | Return `UNKNOWN`, retain once-only operation record, never replay |
| Duplicate operation ID | At most one native mutation; return cached receipt or in-progress error |
| Daemon restart | No automatic replay of an uncertain operation; current native session is re-established before new controls |
| Exact turn completion | Terminal event is correlated to the submitted native turn, not merely “session became idle” |
| Stale interrupt/steer | Exact expected-turn mismatch rejects and cannot affect the current/new turn |
| Stock UI | User can see and interact with the same session; prompt, output, approval, and interrupt state remain coherent |
| Unsupported release | `wiring=auto` selects legacy; explicit native fails with a precise compatibility reason |

## Compatibility and release policy

For every harness/release combination:

1. record the exact upstream version and source commit used for inspection;
2. vendor only test fixtures or generated protocol schemas needed for conformance,
   respecting the upstream license;
3. run pure compatibility probes before selecting native wiring;
4. run executable adapter conformance in CI where it can be deterministic;
5. run opt-in stock-binary tests for UI and end-to-end behavior;
6. extend the accepted version set/range only after all required scenarios pass;
7. fail closed on prereleases, unknown output, or drifted protocol shape.

Semver range compatibility is not evidence by itself. Start with the exact tested
release; widen a range only when upstream explicitly guarantees the relevant API
and Theater has boundary tests for that guarantee.

## Completion definition

This roadmap is successful when Theater can accurately say, per running
participant and per capability, which transport is active; native operations have
once-only, exact-session semantics; native steer/interrupt have exact-turn
semantics; and unsupported harnesses degrade honestly to guarded legacy delivery.
It is not necessary for all harnesses to reach parity.
