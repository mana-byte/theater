# Pi native interaction plan

## Recommendation

Pi is feasible enough to justify a bounded proof spike, but not ready for native
send/steer/interrupt today. Its stock TUI extension API exposes the actions Theater
needs; the blocker is proving a stable one-to-one identity from a Theater operation
to Pi's persisted entry/active turn. Do not enable a control merely because the
extension call returned without throwing—both public send methods return `void`.

Keep the existing native reasoning-setting support. Implement other controls only
if the proof described below passes on the pinned stock release.

Evidence was inspected at Pi commit
[`853a80d26c90a14c1886f0ebb8ffaae133ca2185`](https://github.com/earendil-works/pi/tree/853a80d26c90a14c1886f0ebb8ffaae133ca2185).

## Capability mapping

| Theater capability | Pi stock-TUI extension surface | Decision |
| --- | --- | --- |
| Session identity | read-only `ctx.sessionManager`, current bridge snapshot/session epoch | Already used; retain exact trusted-session checks |
| Execution state | `ctx.isIdle()`, `ctx.signal`, `ctx.hasPendingMessages()` | Already used in bridge snapshot |
| Send new turn | `pi.sendMessage(custom, { triggerTurn: true, deliverAs: "nextTurn" })` or `pi.sendUserMessage(...)` | Proof-gated; prefer custom message if its `details.operation_id` survives to a persisted entry |
| Queue follow-up | Theater queue to native send; Pi also supports `deliverAs: "followUp"` | Keep Theater as queue owner until exact correlation/cancellation is proven |
| Steer | `pi.sendUserMessage(..., { deliverAs: "steer" })` or custom `sendMessage` | Proof-gated by expected active turn and acknowledgement |
| Interrupt | `ctx.abort()` plus current `AbortSignal` | Proof-gated by exact active turn; `abort()` itself accepts no expected ID |
| Reasoning setting | `pi.setThinkingLevel`, read back with `getThinkingLevel` | Already native; retain |
| Model setting | `pi.setModel` | Keep proof-gated: authentication await has no atomic expected-session guard |
| Observation | `agent_*`, `turn_*`, `message_*`, UI prompt events, read-only session entries | Extend only as needed for exact correlation |

Upstream anchors:

- `isIdle`, abort signal, `abort`, and pending messages:
  [`types.ts:309`](https://github.com/earendil-works/pi/blob/853a80d26c90a14c1886f0ebb8ffaae133ca2185/packages/coding-agent/src/core/extensions/types.ts#L309-L349)
- `sendMessage` with custom metadata/delivery mode and `sendUserMessage`:
  [`types.ts:1364`](https://github.com/earendil-works/pi/blob/853a80d26c90a14c1886f0ebb8ffaae133ca2185/packages/coding-agent/src/core/extensions/types.ts#L1364-L1378)
- model/thinking setters:
  [`types.ts:1415`](https://github.com/earendil-works/pi/blob/853a80d26c90a14c1886f0ebb8ffaae133ca2185/packages/coding-agent/src/core/extensions/types.ts#L1415-L1422)
- public lifecycle events and turn indexes:
  [`types.ts:716`](https://github.com/earendil-works/pi/blob/853a80d26c90a14c1886f0ebb8ffaae133ca2185/packages/coding-agent/src/core/extensions/types.ts#L716-L795)
- persisted session entry IDs and custom-message metadata:
  [`session-manager.ts:46`](https://github.com/earendil-works/pi/blob/853a80d26c90a14c1886f0ebb8ffaae133ca2185/packages/coding-agent/src/core/session-manager.ts#L46-L55),
  [`session-manager.ts:123`](https://github.com/earendil-works/pi/blob/853a80d26c90a14c1886f0ebb8ffaae133ca2185/packages/coding-agent/src/core/session-manager.ts#L123-L151)
- richer headless RPC commands and acknowledgements:
  [`rpc-types.ts:20`](https://github.com/earendil-works/pi/blob/853a80d26c90a14c1886f0ebb8ffaae133ca2185/packages/coding-agent/src/modes/rpc/rpc-types.ts#L20-L44),
  [`rpc-mode.ts:394`](https://github.com/earendil-works/pi/blob/853a80d26c90a14c1886f0ebb8ffaae133ca2185/packages/coding-agent/src/modes/rpc/rpc-mode.ts#L394-L430)

The RPC mode is evidence that Pi has acknowledged controls, but it is not the
default solution: launching Pi as a Theater-owned headless RPC process would replace
the stock TUI. It may become a separate opt-in runtime later.

## Current Theater state

- [`pi/manifest.py`](../../theater/harness/builtin/plugins/pi/manifest.py#L118)
  routes send, queue, and interrupt to legacy; steer is unavailable; settings is
  native.
- [`PiFrontendRuntime.update_settings`](../../theater/harness/builtin/plugins/pi/runtime.py#L461)
  is a useful once-only request, session epoch, peer generation, and readback model.
- Send/steer/interrupt deliberately reject through the proof gate at
  [`pi/runtime.py`](../../theater/harness/builtin/plugins/pi/runtime.py#L583).
- Runtime capabilities advertise that gate at
  [`pi/runtime.py`](../../theater/harness/builtin/plugins/pi/runtime.py#L851).
- The rendered bridge already accepts `pi.snapshot` and `pi.settings.update` in
  [`theater_mcp_bridge.ts`](../../theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts#L1248).
- The bridge's session lifecycle, epoch, snapshots, operation cache, and settings
  tail should be reused, not replaced.
- Existing conformance coverage lives in
  [`tests/test_pi_native_bridge.py`](../../tests/test_pi_native_bridge.py),
  [`pi_frontend_bridge_conformance.mts`](../../tests/fixtures/pi_frontend_bridge_conformance.mts),
  and
  [`tests/test_pi_frontend_integration.py`](../../tests/test_pi_frontend_integration.py).

## The identity problem

Theater must bind an accepted send to one exact `native_turn_id` before the job can
be completed safely. It must also compare steer/interrupt with the exact active
turn. Pi currently exposes several nearby facts, none of which alone proves that
mapping:

- `turn_start.turnIndex` is a runtime sequence number, but the send method does not
  return the index it will trigger;
- `sendUserMessage` carries no Theater metadata and returns `void`;
- `sendMessage` can carry `details.operation_id`, but returns `void`;
- persisted `SessionEntry` values have IDs and parent IDs, but the proof must show
  that the injected custom message can be found deterministically and that the
  response turn remains linked to it;
- `ctx.abort()` acts on “current work” and has no expected-turn parameter.

Timestamp proximity, prompt-text matching, a busy transition, or “the next
`turn_start`” are not sufficient. A human prompt, queued message, retry,
compaction, reload, or extension event reordering can create the same observations.

## Phase 0 — correlation proof spike

Keep this phase isolated from manifest routing. It may add test-only bridge methods
or instrumentation, but must not advertise native controls.

### Candidate strategy

Inject a custom message with unique metadata and make it the turn-producing input:

```ts
pi.sendMessage(
  {
    customType: "theater.control",
    content: prompt,
    display: true,
    details: {
      protocol: "theater-pi-control-v1",
      operation_id: operationId,
      native_session_id: sessionId,
    },
  },
  { triggerTurn: true, deliverAs: "nextTurn" },
)
```

Immediately and on lifecycle events, inspect the public read-only session manager
for the persisted `custom_message` entry whose details contain the operation ID.
The candidate `native_turn_id` is its persisted entry ID only if the proof shows:

1. exactly one entry is created for one operation;
2. its session and parent chain are stable;
3. the corresponding assistant/turn completion can be linked to that entry without
   prompt or timestamp heuristics;
4. reload/resume preserves the same entry ID and metadata;
5. a queued/busy delivery still resolves to the correct later turn;
6. a human prompt between request and execution is not claimed;
7. a duplicate request cannot create a second entry.

If public APIs expose the entry ID only after a lifecycle event, the frontend
request may wait a short bounded interval for that evidence before replying. A
timeout after `sendMessage` is `UNKNOWN`; it must not retry.

### Proof matrix

Add executable cases to the TypeScript conformance fixture:

| Case | Required observation |
| --- | --- |
| Idle injection | one persisted custom message; one linked turn; stable entry ID |
| Two sequential operations | two distinct IDs and ordered terminal outcomes |
| Duplicate operation ID | one injection; cached receipt or in-progress error |
| Human input racing injection | human turn and Theater turn remain distinguishable |
| Busy `nextTurn` | exact queued entry is later executed once |
| Busy steer candidate | amendment is tied to the expected active turn, not a future turn |
| Session switch before mutation | definite rejection; no entry in either session |
| Session switch after `sendMessage` | unknown unless exact entry proof resolves it |
| Bridge reconnect | no replay; persisted mapping can be re-observed read-only |
| Pi reload/resume | entry ID and operation metadata survive |
| Compaction/branch | mapping either survives exactly or capability fails closed |
| Abort/retry | terminal state cannot be attributed to a neighboring turn |

Run the same matrix against the stock pinned Pi binary in an opt-in integration
test. A fake extension context cannot prove persistence or event ordering.

### Proof result record

Create `tests/fixtures/pi_native_control/README.md` with:

- Pi version and source commit;
- the exact public API calls used;
- captured, redacted event and session-entry sequences for each case;
- the chosen `native_turn_id` definition;
- known event-order guarantees or observed limitations;
- pass/fail conclusion for send, steer, and interrupt independently.

Do not treat “send passed” as proof for steer or interrupt.

## Phase 1 — native send, only after proof

### Bridge method

Add `pi.control.send` beside the existing request methods in
[`theater_mcp_bridge.ts`](../../theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts#L1248).
Its parameters are:

```json
{
  "operation_id": "...",
  "native_session_id": "...",
  "prompt": "...",
  "delivery": "nextTurn"
}
```

Admission sequence:

1. validate bounded fields and reserve the operation ID in the existing bounded
   cache;
2. capture current bridge/session epoch and context object;
3. require current trusted session, `ctx.isIdle()`, and no pending messages for an
   ordinary send;
4. inject the metadata-bearing message;
5. await public persisted-entry/lifecycle evidence found by Phase 0;
6. revalidate context/session/bridge epochs;
7. return accepted with exact operation/session/turn IDs;
8. cache the terminal response for exact duplicates.

Keep ordinary send idle-only. Theater's queued follow-up is dispatched through this
same method only after the control service has determined the participant is ready.
Do not pass Pi `followUp`/`nextTurn` busy queue semantics through until Theater can
cancel and attribute them consistently.

### Runtime method

Replace the proof-gated
[`send`](../../theater/harness/builtin/plugins/pi/runtime.py#L583) using the same
shape as `update_settings`:

- snapshot and require native session, connected peer, trusted identity, and idle;
- capture peer generation, session epoch, and bridge epoch;
- request `pi.control.send` once;
- decode strict `status`, `operation_id`, `native_session_id`, `native_turn_id`, and
  epoch fields;
- accepted only after every identity matches;
- definite bridge admission errors become rejected;
- timeout, disconnect, malformed success, or epoch drift become unknown.

Extend `_runtime_snapshot()` to include the exact active `native_turn_id` produced by
the proven lifecycle mapping. Add `SEND` and `QUEUE_FOLLOWUP` to `_capabilities()`
only while that mapping is healthy.

### Manifest routing

In [`pi/manifest.py`](../../theater/harness/builtin/plugins/pi/manifest.py#L118),
remove `SEND` and `QUEUE_FOLLOWUP` from `legacy_fallback` only after both bridge and
stock-binary conformance pass. Leave interrupt legacy and steer unavailable until
their independent phases pass.

## Phase 2 — steer proof and implementation

Pi's `deliverAs: "steer"` is semantically promising. Before exposing it:

1. capture the bridge's exact active `native_turn_id`;
2. receive `expected_native_turn_id` from Theater;
3. synchronously check expected == active immediately before `sendMessage` or
   `sendUserMessage`;
4. attach the steer `operation_id` through the metadata-bearing API if possible;
5. prove the persisted event/entry belongs to the same active turn rather than a
   new queued turn;
6. prove a turn ending/restarting around the call cannot apply the steer to the
   replacement turn;
7. return an accepted receipt only with the same expected turn ID.

If Pi's public extension API cannot atomically guard the active turn, leave Theater
steer unavailable. A fast check followed by a `void` call is not enough by itself.

## Phase 3 — interrupt proof and implementation

Add `pi.control.interrupt` only if the active-turn proof is stable. Parameters must
include operation/session/expected-turn IDs. The bridge must:

- require `ctx.signal` and a non-idle current context;
- compare exact expected and current turn;
- call `ctx.abort()` once;
- correlate the resulting terminal lifecycle event to that same turn;
- distinguish “already ended before abort” from “abort requested”;
- report post-call ambiguity as unknown.

`ctx.abort()` has no acknowledgement or expected-turn parameter. If a stock-binary
race test can make the old turn end and a new one start between check and call, this
phase fails and legacy Escape remains the correct route.

## Settings follow-up

Preserve
[`update_settings`](../../theater/harness/builtin/plugins/pi/runtime.py#L461) and
the bridge readback semantics. Add `supported_fields: ["reasoning_effort"]` to the
shared capability report. Keep `model` rejected with
`model_update_proof_gated`: public `setModel` awaits provider authentication, and
the bridge cannot hold an atomic expected-session guard through that await.

Only revisit model updates if upstream adds an expected-session token or a public
transaction that performs authentication before committing to the current session.

## File-by-file work

- [`theater_mcp_bridge.ts`](../../theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts#L1248):
  test-only proof instrumentation first; then control methods, receipt cache reuse,
  session/turn checks, lifecycle correlation.
- [`pi/runtime.py`](../../theater/harness/builtin/plugins/pi/runtime.py#L435): strict
  control result decoders, active turn in snapshot, delivery result mapping, dynamic
  capabilities.
- [`pi/manifest.py`](../../theater/harness/builtin/plugins/pi/manifest.py#L118):
  route only independently proven capabilities.
- [`pi/frontend.py`](../../theater/harness/builtin/plugins/pi/frontend.py): change
  only if the frontend overlay or descriptor needs a protocol/version bump.
- [`tests/fixtures/pi_frontend_bridge_conformance.mts`](../../tests/fixtures/pi_frontend_bridge_conformance.mts):
  extend the executable bridge harness; split a second control fixture if size
  obscures settings coverage.
- [`tests/test_pi_native_bridge.py`](../../tests/test_pi_native_bridge.py): rendered
  bridge protocol, duplicate, epoch, and failure semantics.
- [`tests/test_pi_frontend_integration.py`](../../tests/test_pi_frontend_integration.py):
  daemon/frontend once-only behavior.
- [`tests/test_control_service.py`](../../tests/test_control_service.py): send/job,
  exact turn, steer, interrupt, queue, terminal evidence, and unknown/no-fallback.

Focused checks after each phase:

```sh
uv run pytest tests/test_frontend_requests.py tests/test_pi_native_bridge.py \
  tests/test_pi_frontend_integration.py tests/test_control_service.py
```

## Release gates and stop conditions

Native send may ship only if a public, persisted ID is returned or observed before
acceptance and terminal evidence maps to it one-to-one. Steer and interrupt require
additional stale-turn race proof. Every path must preserve the stock TUI.

Stop and keep legacy controls if any of these are true:

- correlation relies on prompt text, timestamps, or “next event” ordering;
- injected metadata is absent after reload/resume;
- a human or queued input can steal the mapping;
- the extension cannot tell which turn `abort()` or `steer` affected;
- a timeout can cause the bridge or daemon to replay the call.

That outcome would still leave Pi with useful native status and settings. The proof
spike is worthwhile because it has a bounded cost and a credible persisted-entry
candidate, but failure should end this path until upstream exposes stronger IDs or
acknowledged extension controls.
