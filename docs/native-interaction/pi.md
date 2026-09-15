# Pi native wiring: phase two

## Outcome sought

Extend the shipped Pi frontend runtime with an authoritative active-run identity and
native interrupt through public `ctx.abort()`. Keep interrupt on tmux until exact-run
and stale-turn race proofs pass. Treat native steer as a separate later proof even
though Pi exposes `deliverAs: "steer"`.

The work is mostly inside Pi's existing rendered extension. It does not require a new
daemon transport or database schema.

## Current baseline

- [`pi/manifest.py`](../../theater/harness/builtin/plugins/pi/manifest.py#L118)
  declares a frontend `RuntimeManifest` with native send/queue and reasoning setting.
- The manifest retains interrupt in `legacy_fallback` and marks steer unavailable at
  [`manifest.py`](../../theater/harness/builtin/plugins/pi/manifest.py#L136).
- [`pi/runtime.py`](../../theater/harness/builtin/plugins/pi/runtime.py#L617) sends
  through the authenticated frontend request path; steer and interrupt remain proof
  gated at lines 694–704.
- [`theater_mcp_bridge.ts`](../../theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts#L1388)
  implements idle send and correlates the durable custom entry at line 1521.
- The bridge snapshot currently publishes only the active Theater-attributed send ID
  at [`theater_mcp_bridge.ts`](../../theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts#L1795),
  so human turns and some queued continuations appear active without a
  `native_turn_id`.
- Lifecycle subscriptions are installed at
  [`theater_mcp_bridge.ts`](../../theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts#L1953).

## Public Pi surfaces

Evidence was inspected at Pi commit
[`853a80d26c90a14c1886f0ebb8ffaae133ca2185`](https://github.com/earendil-works/pi/tree/853a80d26c90a14c1886f0ebb8ffaae133ca2185),
package version `0.84.4`:

- `before_agent_start`, `agent_start`, `agent_end`, `agent_settled`, and turn events
  are documented in
  [`extensions.md:530`](https://github.com/earendil-works/pi/blob/853a80d26c90a14c1886f0ebb8ffaae133ca2185/packages/coding-agent/docs/extensions.md#L530-L613);
- `ctx.sessionManager.getLeafId()` is public at
  [`extensions.md:1000`](https://github.com/earendil-works/pi/blob/853a80d26c90a14c1886f0ebb8ffaae133ca2185/packages/coding-agent/docs/extensions.md#L1000-L1011);
- `ctx.signal`, `ctx.isIdle()`, and `ctx.abort()` are public at
  [`extensions.md:1019`](https://github.com/earendil-works/pi/blob/853a80d26c90a14c1886f0ebb8ffaae133ca2185/packages/coding-agent/docs/extensions.md#L1019-L1047);
- `pi.sendMessage` and `pi.sendUserMessage(..., {deliverAs: "steer"})` are public at
  [`extensions.md:1416`](https://github.com/earendil-works/pi/blob/853a80d26c90a14c1886f0ebb8ffaae133ca2185/packages/coding-agent/docs/extensions.md#L1416-L1467);
- interactive `ctx.abort()` restores Pi's native queued messages to the editor and
  calls the agent abort exactly once in
  [`interactive-mode.ts:4372`](https://github.com/earendil-works/pi/blob/853a80d26c90a14c1886f0ebb8ffaae133ca2185/packages/coding-agent/src/modes/interactive/interactive-mode.ts#L4372-L4390).

These APIs make interrupt plausible. They do not by themselves prove that a leaf ID
captured before abort names the currently executing run across retry, compaction, and
queued continuation.

## Capability mapping

| Theater capability | Pi public surface | Phase-two decision |
| --- | --- | --- |
| Idle send | Shipped `pi.sendMessage`, durable custom entry correlation | Keep native |
| Follow-up queue | Theater queue dispatches through idle send | Keep native and Theater-owned |
| Active turn identity | `getLeafId()` plus lifecycle events | Prove for every run, then expose |
| Interrupt | `ctx.abort()` and `ctx.signal` | Implement only after exact-run proof |
| Steer | `sendUserMessage(..., {deliverAs: "steer"})` | Separate proof; unavailable meanwhile |
| Model update | `setModel` can await provider authentication | Keep unavailable; session race remains |
| Reasoning effort | Existing synchronous thinking-level update/readback | Keep native |
| Observation/result | Bridge events plus durable transcript source | Extend with active-run facts; retain transcript terminal authority |

## Phase 0 — prove active-run identity

Extend the executable bridge fixture before changing advertised capabilities. The
candidate algorithm is to capture `ctx.sessionManager.getLeafId()` synchronously in
`before_agent_start` and confirm it in `agent_start` before any await. Record it as an
`activeRun` object distinct from the existing `sendTurn` attribution:

```typescript
type ActiveRun = {
	entryId: string;
	sessionId: string;
	epoch: number;
	signal: AbortSignal;
	source: "human" | "theater" | "unknown";
};
```

`sendTurn` answers “which Theater operation owns this durable entry?” `activeRun`
answers “which exact run is executing now?” Do not merge them. A human-created turn
must populate `activeRun` without gaining a Theater operation ID.

The fixture must prove the chosen entry ID through:

1. an ordinary human prompt;
2. Theater's existing idle send;
3. multiple tool-call/LLM cycles within one agent run;
4. automatic retry;
5. automatic compaction and retry;
6. steering and follow-up messages queued by the stock UI;
7. fast settlement before a control response is emitted;
8. session new/resume/fork/reload and bridge epoch replacement;
9. a new prompt submitted immediately after `agent_settled`.

Capture a lifecycle trace containing event name, session ID, leaf ID, signal identity,
idle/pending flags, and entry type. Do not use prompt text or timestamps to repair an
ambiguous mapping.

Pass condition: one public stable ID names the active run from first cancellable event
through terminal settlement, and a replacement run cannot retain the old ID/signal.
If `getLeafId()` changes during one logical run or is not available before a safe
abort point, native interrupt is a no-go.

## Phase 1 — publish active-run state

In
[`theater_mcp_bridge.ts`](../../theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts#L1275),
add bridge state and small synchronous helpers that:

- capture the proven ID at the proven lifecycle boundary;
- retain the exact `ctx.signal`/context needed for that run;
- clear only when the same run settles or its session epoch is replaced;
- reject contradictory lifecycle events instead of guessing;
- publish `execution_state: "unknown"` if identity cannot be established.

Change the snapshot at line 1823 from `sendTurn?.nativeTurnId` to the authoritative
`activeRun?.entryId`. Preserve `sendTurn` for operation/job attribution and terminal
result accumulation.

Python decoding in
[`pi/runtime.py`](../../theater/harness/builtin/plugins/pi/runtime.py#L580) must reject
an active snapshot without a bounded turn ID. A missing ID while active makes
interrupt unavailable for that snapshot; it does not prove idle.

Use lifecycle ordering from the proof, for example:

```typescript
pi.on("before_agent_start", (_event, ctx) => bridge.beginRun(ctx));
pi.on("agent_start", (_event, ctx) => bridge.confirmRun(ctx));
pi.on("agent_settled", (_event, ctx) => bridge.settleRun(ctx));
```

This snippet is structural. Do not adopt those exact boundaries unless Phase 0 proves
their IDs and signal lifetime.

## Phase 2 — native interrupt request

Add one frontend method:

```json
{
  "method": "pi.control.interrupt",
  "params": {
    "operation_id": "<theater operation>",
    "native_session_id": "<expected session>",
    "expected_native_turn_id": "<expected active run>"
  }
}
```

Handle it in one synchronous mutation section. There must be no `await` between the
last identity check and `ctx.abort()`:

1. validate bounds, method, operation ID, current session, and bridge epoch;
2. return the bounded operation-cache result for an exact duplicate;
3. require non-idle state, an `activeRun`, a non-aborted current signal, and the same
   current context;
4. compare `expected_native_turn_id` exactly with `activeRun.entryId`;
5. reserve an interrupt-correlation record before mutation;
6. call `ctx.abort()` exactly once;
7. mark the operation as possibly applied before yielding.

Then await evidence for that same run:

- `ctx.signal.aborted === true`;
- an assistant terminal message whose `stopReason` is `"aborted"` when Pi emits one;
- `agent_settled` for the same session/run.

Return `ACCEPTED` only when the stock proof establishes the minimum authoritative
combination. A stale session/turn detected before `ctx.abort()` is `REJECTED`. A
throw, timeout, disconnect, session drift, contradictory lifecycle event, or new run
after the call is `UNKNOWN`, because the old run may have been interrupted. Cache all
three terminal results by `operation_id`; never call `abort()` for a duplicate.

The bridge operation cache is bounded at
[`theater_mcp_bridge.ts`](../../theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts#L1919).
Scope interrupt correlation to the bridge epoch, but keep a completed reply available
long enough for the daemon's request retry/reconnect behavior. An in-progress
duplicate returns a bounded “operation in progress” response and does not mutate.

## Phase 3 — expose the route

Implement `interrupt()` in
[`pi/runtime.py`](../../theater/harness/builtin/plugins/pi/runtime.py#L694) with strict
response decoding. It must pass the expected snapshot turn, preserve
`ACCEPTED`/`REJECTED`/`UNKNOWN`, and never translate an ambiguous frontend failure to
legacy keys.

Only after stock race tests pass:

- advertise `RuntimeCapability.INTERRUPT` in the bridge snapshot;
- remove `INTERRUPT` from `legacy_fallback` in
  [`pi/manifest.py`](../../theater/harness/builtin/plugins/pi/manifest.py#L136);
- retain `STEER` in `unavailable_capabilities`;
- leave reasoning effort as the only supported settings field.

If exact interrupt does not pass, keep the current manifest unchanged. Do not expose
a “native interrupt” that merely aborts whatever happens to be current.

## Phase 4 — independent steer proof

Interrupt success does not establish steer safety. Run a separate conformance spike
for `pi.sendUserMessage(prompt, {deliverAs: "steer"})` that proves:

- the expected active run can be validated immediately before enqueue;
- the steered message has a durable ID linked to the Theater operation;
- delivery applies to the expected run and cannot spill into its replacement;
- timeout after enqueue is `UNKNOWN` and duplicates do not enqueue twice;
- Pi's native steering queue and Theater's follow-up queue have distinct ownership.

Only then add `pi.control.steer`, `HarnessRuntime.steer()`, and the capability. This is
optional phase-two stretch work, not part of interrupt acceptance.

## File-by-file work

- `theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts`: `ActiveRun`, lifecycle
  proof integration, interrupt dispatch, once-only cache, and terminal correlation.
- `theater/harness/builtin/plugins/pi/runtime.py`: strict snapshot/result decoders and
  native interrupt method.
- `theater/harness/builtin/plugins/pi/manifest.py`: change routing only after proof.
- `theater/harness/builtin/plugins/pi/frontend.py`: protocol/version bump only if the
  rendered descriptor requires it.
- `tests/fixtures/pi_frontend_bridge_conformance.mts`: executable active-run and abort
  cases; split a control fixture only if the existing file becomes unreadable.
- `tests/test_pi_native_bridge.py`: frame bounds, duplicates, epoch/session/turn drift,
  lifecycle correlation, and unknown semantics.
- `tests/test_pi_frontend_integration.py`: daemon/frontend reconnect and once-only
  behavior.
- `tests/test_control_service.py`: native interrupt routing, stale-turn rejection,
  queue cancellation behavior, terminal evidence, and no fallback after `UNKNOWN`.

Focused verification:

```sh
uv run pytest tests/test_frontend_requests.py \
  tests/test_pi_native_bridge.py \
  tests/test_pi_frontend_integration.py \
  tests/test_control_service.py
```

## Acceptance and stop criteria

Native interrupt ships only if an exact public active-run ID survives human input,
tool loops, retry, compaction, queued continuation, fast settlement, and session
replacement. The check-and-abort race must prove a replacement turn cannot be hit.

Stop and retain legacy interrupt if:

- correlation uses text, timestamps, event position, or the Theater-only `sendTurn`;
- the leaf ID changes ambiguously during one cancellable run;
- an `await` is required between the final identity check and `ctx.abort()`;
- the bridge cannot distinguish old-turn abort from replacement-turn abort;
- timeout/disconnect can cause a replay;
- interrupt changes Pi's queue/editor state in a way Theater cannot report honestly.

That is an acceptable outcome: native send and reasoning settings remain valuable,
and tmux continues to provide the compatibility interrupt.
