# OpenCode native interaction plan

## Recommendation

Implement OpenCode first. It is the strongest next adapter because the stock TUI's
public plugin API exposes the current route, session state, event bus, and generated
SDK client in the same process. Theater already installs an authenticated passive
plugin there, so the work is an extension of an existing boundary rather than a new
process topology.

First ship native **send** and Theater-owned **follow-up queue dispatch**. Ship
native **interrupt** only after exact active-turn correlation passes. Do not expose
**steer** or **settings** in the first wave.

Evidence was inspected at OpenCode commit
[`c470c79513f78aabb2ff88a8c8f7a3a22c4e97af`](https://github.com/anomalyco/opencode/tree/c470c79513f78aabb2ff88a8c8f7a3a22c4e97af).
Re-run the proof against the release actually selected for Theater.

## Capability mapping

| Theater capability | OpenCode surface | Decision |
| --- | --- | --- |
| Session identity | `api.route.current.params.sessionID`; `api.state.session.get/status/messages` | Use and compare with Theater's trusted transcript session before every mutation |
| Send new turn | `api.client.session.promptAsync` with caller-supplied `messageID`; HTTP 204 | Implement |
| Queue follow-up | Theater queue eventually calls the same native send | Implement; Theater remains queue owner |
| Steer active turn | A prompt submitted while busy is persisted for the next model input | Do not map to steer; it is a queued/future turn |
| Interrupt | `api.client.session.abort`, returning boolean | Proof-gated until active turn ID and stale-turn rejection are reliable |
| Settings | No equivalent validated in the current TUI plugin plan | Leave unavailable |
| Status | `api.state.session.status` and `session.status` events | Already used; extend with exact turn identity |
| Completion | assistant message whose `parentID` equals Theater's submitted user `messageID` | Use as exact terminal correlation after conformance proof |

Upstream anchors:

- TUI route identity:
  [`packages/plugin/src/tui.ts:53`](https://github.com/anomalyco/opencode/blob/c470c79513f78aabb2ff88a8c8f7a3a22c4e97af/packages/plugin/src/tui.ts#L53-L63)
- TUI session state:
  [`packages/plugin/src/tui.ts:375`](https://github.com/anomalyco/opencode/blob/c470c79513f78aabb2ff88a8c8f7a3a22c4e97af/packages/plugin/src/tui.ts#L375-L399)
- public client and event bus:
  [`packages/plugin/src/tui.ts:581`](https://github.com/anomalyco/opencode/blob/c470c79513f78aabb2ff88a8c8f7a3a22c4e97af/packages/plugin/src/tui.ts#L581-L628)
- `session.abort`:
  [`packages/sdk/js/src/gen/sdk.gen.ts:548`](https://github.com/anomalyco/opencode/blob/c470c79513f78aabb2ff88a8c8f7a3a22c4e97af/packages/sdk/js/src/gen/sdk.gen.ts#L548-L556)
- `session.promptAsync`:
  [`packages/sdk/js/src/gen/sdk.gen.ts:636`](https://github.com/anomalyco/opencode/blob/c470c79513f78aabb2ff88a8c8f7a3a22c4e97af/packages/sdk/js/src/gen/sdk.gen.ts#L636-L648)
- caller-supplied message ID and 204 response:
  [`types.gen.ts:2683`](https://github.com/anomalyco/opencode/blob/c470c79513f78aabb2ff88a8c8f7a3a22c4e97af/packages/sdk/js/src/gen/types.gen.ts#L2683-L2730)
- abort's boolean response:
  [`types.gen.ts:2373`](https://github.com/anomalyco/opencode/blob/c470c79513f78aabb2ff88a8c8f7a3a22c4e97af/packages/sdk/js/src/gen/types.gen.ts#L2373-L2404)
- busy submission becomes the next model input and assistant `parentID` identifies
  it:
  [`prompt.test.ts:1433`](https://github.com/anomalyco/opencode/blob/c470c79513f78aabb2ff88a8c8f7a3a22c4e97af/packages/opencode/test/session/prompt.test.ts#L1433-L1489)

## Current Theater state

- The manifest selects a frontend-hosted runtime but routes send, queue, and
  interrupt to legacy in
  [`opencode/manifest.py`](../../theater/harness/builtin/plugins/opencode/manifest.py#L119).
- [`OpenCodeFrontendRuntime`](../../theater/harness/builtin/plugins/opencode/runtime.py#L30)
  receives observations and rejects every native mutation.
- [`frontend.py`](../../theater/harness/builtin/plugins/opencode/frontend.py#L18)
  renders the stock TUI plugin with authenticated socket/reconnect/snapshot logic.
- [`OpenCodeTuiLiveSource`](../../theater/harness/builtin/plugins/opencode/live.py#L19)
  validates visible route against the daemon's trusted session, but tracks only
  status—not an exact turn.
- [`FrontendRequests`](../../theater/daemon/harness_runtime/frontend_requests.py#L33)
  already provides bounded, once-only request correlation from daemon to plugin.
- The current passive-only test deliberately asserts that the generated plugin
  contains no `api.client` usage in
  [`test_opencode_frontend.py`](../../tests/test_opencode_frontend.py#L71). That
  assertion must be replaced, not worked around.

## Native identity model

Use the submitted **user message ID** as Theater's OpenCode `native_turn_id`.
OpenCode does not return an assistant ID from `promptAsync`, but it accepts a
caller-supplied `messageID`, persists that user message, and parents the resulting
assistant message to it. This gives Theater an identity at admission time and an
exact terminal correlation later:

```text
Theater operation op-42
  -> OpenCode user message msg_...
     -> OpenCode assistant message ..., parentID = msg_...

Theater native_turn_id = msg_...
```

This convention must be confined to the OpenCode adapter and tested against the
stock release. It does not assert that OpenCode itself calls the user message a
“turn ID”.

The generated ID must satisfy the pinned release's public request schema. The
inspected implementation accepts IDs beginning with `msg`; its own generator is
shown in
[`id.ts:22`](https://github.com/anomalyco/opencode/blob/c470c79513f78aabb2ff88a8c8f7a3a22c4e97af/packages/opencode/src/id/id.ts#L22-L69).
Prefer a public exported generator if the tested release exposes one. Otherwise
keep a small adapter-local generator, validate it with a real `promptAsync` request,
and consider that format part of the exact-release compatibility proof. Do not
import an unexported OpenCode source module.

## Wire design

Extend the current `theater-frontend-v1` connection. Do not create a second socket.
The daemon sends:

```json
{
  "type": "request",
  "id": "frontend-request-id",
  "method": "opencode.send",
  "params": {
    "operation_id": "theater-operation-id",
    "native_session_id": "ses_...",
    "prompt": "Implement the parser"
  }
}
```

After a successful SDK 204, the plugin responds:

```json
{
  "type": "response",
  "id": "frontend-request-id",
  "result": {
    "status": "accepted",
    "operation_id": "theater-operation-id",
    "native_session_id": "ses_...",
    "native_turn_id": "msg_...",
    "session_epoch": 7
  }
}
```

Errors must use the existing response envelope. Suggested definite rejection codes:

- `invalid_request`: missing/invalid bounded fields;
- `wrong_session`: requested session is not the current route;
- `not_ready`: route/state/client is unavailable;
- `busy`: ordinary send was requested while session status is busy;
- `operation_in_progress`: duplicate operation still executing;
- `operation_capacity`: bounded receipt cache is full;
- `native_rejected`: SDK produced a definite non-2xx result.

Transport loss, timeout, response mismatch, malformed success, or session epoch
change after transmission produces `UNKNOWN` in Python. It must never trigger
legacy fallback.

### Send handler sketch

This is illustrative; adapt it to the generated SDK's exact result/error shape in
the pinned release:

```ts
async function performSend(params: Record<string, unknown>, epoch: number) {
  const current = routeState()
  requireCurrentSession(current, params.native_session_id, epoch)
  if (api.state.session.status(current.id)?.type !== "idle") {
    return error("busy", "OpenCode session is not idle")
  }

  const messageID = makeCompatibleMessageID()
  const result = await api.client.session.promptAsync({
    path: { id: current.id },
    body: {
      messageID,
      parts: [{ type: "text", text: params.prompt }],
    },
  })
  requireAccepted204(result)
  requireCurrentSession(routeState(), current.id, epoch)
  return accepted(params.operation_id, current.id, messageID, epoch)
}
```

The pre-call idle check preserves Theater's existing ordinary-send contract.
Although OpenCode supports a prompt while busy, upstream explicitly treats it as
the next model input. Theater's own `QUEUE_FOLLOWUP` already models that intent and
must remain cancellable before dispatch.

## Implementation steps

### 1. Pin and fixture the selected release

Update
[`runtime_plan.py`](../../theater/harness/builtin/plugins/opencode/runtime_plan.py#L12):

- rename the policy from “passive” to the native-control policy;
- initially accept exactly the stock version used by conformance, not the current
  broad `>=1.18.29,<1.19.0` range;
- keep prereleases and unparsable versions unsupported;
- make the diagnostic name the missing/unsupported native control policy.

Add a small fixture under `tests/fixtures/opencode_native_frontend/` containing the
tested version, public request/response examples, and any event records used for
turn correlation. Record the source commit in the fixture README.

### 2. Make the rendered TUI plugin bidirectional

Edit
[`opencode/frontend.py`](../../theater/harness/builtin/plugins/opencode/frontend.py#L18):

- retain the existing token-file authentication, reconnect backoff, frame bounds,
  backpressure checks, snapshots, and disposal behavior;
- add buffered NDJSON parsing for host `request` frames;
- add `respond`, bounded value validators, a bounded operation cache, and a
  serialized mutation tail;
- implement only `opencode.send` initially;
- scope operation receipts to route/session epoch; never serve an accepted receipt
  as if it belonged to a newly selected session;
- keep status and message events flowing while a request is in flight.

Extracting the request machinery into a sibling `frontend_controls.py` renderer is
reasonable if `frontend.py` becomes difficult to review. Do not put cross-harness
policy in generated JavaScript.

### 3. Add exact turn tracking to the live source

Extend
[`opencode/live.py`](../../theater/harness/builtin/plugins/opencode/live.py#L19)
to retain bounded per-session facts:

- current trusted `session_id` and `session_epoch`;
- active Theater-correlatable user message ID;
- assistant message ID and status for that `parentID`, when observed;
- terminal outcome keyed by the user message ID;
- connection health and diagnostics.

Use public TUI state/events, not the database, for fresh active-turn control. The
durable OpenCode source may still provide history. Every event must be rejected if
its session does not equal both the visible route and trusted session.

The source should expose `native_turn_id = submitted user message ID` while the
correlated assistant is active. On terminal assistant/session evidence, emit one
`NativeTurnOutcome` with that same ID. Merely seeing the session become idle is not
sufficient unless the message lineage also establishes which turn ended.

If OpenCode events do not provide enough terminal data, request a bounded snapshot
of `api.state.session.messages(sessionID)` and correlate the assistant `parentID`.
Do not enumerate unrelated sessions.

### 4. Implement Python runtime send

Replace the rejection in
[`opencode/runtime.py`](../../theater/harness/builtin/plugins/opencode/runtime.py#L88).
The method should:

1. call `snapshot()` and require connected health, trusted session, idle execution,
   and native send capability;
2. send `opencode.send` with `operation_id`, `native_session_id`, and prompt through
   `RuntimeFrontendConnection.request`;
3. decode a strict bounded result object;
4. confirm operation ID, session ID, frontend peer generation, and session epoch;
5. return `ControlReceipt(ACCEPTED, native_turn_id=message_id)` only after the SDK
   acknowledgement;
6. map definite pre-mutation adapter errors to `REJECTED`;
7. map timeout, disconnect, malformed success, or post-write identity drift to
   `UNKNOWN`.

Model the peer-generation and epoch checks on Pi's existing
[`update_settings`](../../theater/harness/builtin/plugins/pi/runtime.py#L461).

Snapshot capabilities should advertise `SEND` and `QUEUE_FOLLOWUP` only while the
frontend is connected and its visible session matches the trusted session. A busy
session still supports the transport; busy admission is a separate control-service
decision.

### 5. Route send and queue natively

In
[`opencode/manifest.py`](../../theater/harness/builtin/plugins/opencode/manifest.py#L119):

- remove `SEND` and `QUEUE_FOLLOWUP` from `legacy_fallback` after all send gates
  pass;
- leave `INTERRUPT` in `legacy_fallback`;
- leave `STEER` and `SETTINGS_UPDATE` unavailable.

Do not silently restore legacy send when a connected native request returns
`UNKNOWN`. `wiring=auto` selects a route at setup/compatibility time, not after an
individual uncertain write.

### 6. Prove and optionally enable interrupt

Only begin this stage after send is stable. Add `opencode.interrupt` to the plugin:

1. require request session == visible route == trusted session;
2. derive the current native turn from message lineage;
3. require it equals `expected_native_turn_id` immediately before the SDK call;
4. call `api.client.session.abort({ path: { id: sessionID } })`;
5. require the boolean success response;
6. re-read the route and correlated active turn;
7. return accepted only if the request targeted the exact turn observed at
   admission. Post-call ambiguity is `UNKNOWN`, not `REJECTED`.

Because `session.abort` itself accepts only a session ID, the adapter cannot make
the expected-turn comparison atomic inside OpenCode. A stock-binary race test can
demonstrate observed behavior but cannot prove away this time-of-check/time-of-use
gap. Keep interrupt legacy unless the selected public API binds abort to an
immutable run/turn handle, accepts an expected message/turn ID, or otherwise
documents equivalent atomic semantics. Do not weaken Theater's exact-turn contract
to gain feature parity.

## Test plan

### Python/unit

Update
[`tests/test_opencode_frontend.py`](../../tests/test_opencode_frontend.py#L71):

- replace the passive-only `api.client` prohibition with assertions for the exact
  approved methods and continued absence of unrelated client mutations;
- send an idle request and assert the strict accepted receipt;
- assert busy, wrong session, home route, stale epoch, malformed request, duplicate
  ID, capacity, and definite SDK rejection;
- assert disconnect/timeout after the SDK call yields no replay;
- prove notifications continue during request handling;
- prove a session switch invalidates cached/current turn facts.

Add runtime tests beside the current passive test at
[`test_opencode_frontend.py`](../../tests/test_opencode_frontend.py#L518):

- accepted receipt binds the exact message ID;
- a mismatched operation/session/epoch becomes unknown;
- connection loss before admission rejects only when the bridge can prove no SDK
  mutation started;
- runtime capabilities disappear when trust/connection is lost;
- busy prompt never bypasses Theater's queue.

Add control-service integration cases to
[`tests/test_control_service.py`](../../tests/test_control_service.py): native send,
queue dispatch, terminal evidence, duplicate turn conflict, unknown/no-fallback,
and human-created active turn.

### Executable generated-plugin fixture

Add `tests/fixtures/opencode_frontend_control_conformance.mts`, analogous to the Pi
bridge fixture. Execute the actual string rendered by `frontend.py` against:

- a fake public TUI API with exact generated SDK shapes;
- the real authenticated Unix listener;
- delayed SDK resolution to force session switches and disconnects;
- duplicate/in-progress operation requests;
- inbound and outbound frame-size limits.

### Stock UI gate

Extend
[`tests/test_opencode_stock_ui.py`](../../tests/test_opencode_stock_ui.py#L1),
behind the existing opt-in environment flag, to prove on the pinned binary:

1. launch the ordinary stock TUI and establish its native session;
2. send through Theater without keyboard input;
3. observe the prompt in the stock UI and exactly one provider request;
4. observe assistant lineage `parentID == submitted messageID`;
5. complete the exact Theater job from native evidence;
6. submit a human prompt and verify it is not claimed by a Theater operation;
7. switch sessions during a held request and prove no false accepted result;
8. when an atomic interrupt surface is proposed, race abort against completion/
   new-turn start and prove the new turn is never aborted.

Run the focused suite:

```sh
uv run pytest tests/test_frontend_requests.py tests/test_opencode_frontend.py \
  tests/test_control_service.py
THEATER_OPENCODE_STOCK_CONFORMANCE=1 uv run pytest tests/test_opencode_stock_ui.py
```

## Release and rollback gates

Enable native send only when all of these are true:

- the version probe matches the exact tested release;
- generated-plugin and stock-TUI conformance pass;
- accepted send always returns a unique, persisted, correlatable message ID;
- terminal evidence uses `parentID`, not idle inference;
- timeout/disconnect paths demonstrably never retry or fall back;
- the stock UI displays and can continue the same session.

Rollback is a manifest routing change: put send/queue back in `legacy_fallback` and
remove the release from the native compatibility policy. Preserve durable control
records; do not reinterpret prior unknown deliveries.

## Worth/stop judgment

This adapter is worth implementing because it exercises Theater's distinctive
cross-harness control plane with public APIs and limited new topology. Stop after
the conformance spike if caller-supplied message IDs are not stable, assistant
lineage cannot identify completion, or the plugin cannot validate the visible
session at mutation time. In that case the correct result is richer observation
plus legacy controls, not a private OpenCode fork.
