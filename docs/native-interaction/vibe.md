# Vibe native interaction plan

## Recommendation

Do not implement Vibe native controls in the first wave. Vibe has the richest
public protocol after Codex, including exact expected-turn guards, idempotency keys,
queue identities, session snapshots, and terminal turn states. The blocker is
topology: the stock Textual UI is already the one attached client of its app-server
runtime, and Vibe explicitly does not model multiple simultaneous attached
observers.

Run a release-monitoring/attachment proof before writing a Theater runtime. If Vibe
adds an official frontend plugin, control socket, or multi-client attachment model,
the adapter becomes highly feasible. Until then, keep transcript observation and
legacy Escape interruption.

Evidence was inspected at Vibe commit
[`2817f3df81ae05d49ba9538262edb1d5a18fa006`](https://github.com/mistralai/mistral-vibe/tree/2817f3df81ae05d49ba9538262edb1d5a18fa006),
version `2.25.1`.

## Capability mapping if attachment becomes available

| Theater capability | Vibe app-server surface | Semantic fit |
| --- | --- | --- |
| Start/resume session | `session/start`, `session/resume`, `session/continue`, `session/read` | Strong |
| Send new turn | `turn/start` with `idempotency_key`; returns `PublicTurn` | Strong; exact turn ID at acknowledgement |
| Queue follow-up | `session/turn/enqueue`; returns `queue_item_id`; read/remove/replace operations | Strong, but choose either Vibe or Theater as queue owner |
| Promote queued item to active steer | `session/turn/queue/steer` with `expected_turn_id` | Strong |
| Steer active turn | `turn/steer` with `expected_turn_id` and idempotency key | Strong |
| Interrupt | `turn/interrupt` with `expected_turn_id` | Strong |
| Settings | `session/settings/update` and `session/read` | Likely strong; map only verified fields |
| Active turn | `PublicSessionState.turns`, `PublicTurn.status`, session facade `active_turn_id` | Strong |
| Terminal outcome | `PublicTurn` completed/failed/interrupted plus event watermark | Strong |
| Human interaction | server-to-client callback requests and active callback state | Rich, but requires one authoritative client owner |

Upstream anchors:

- server method catalogue:
  [`protocol.py:167`](https://github.com/mistralai/mistral-vibe/blob/2817f3df81ae05d49ba9538262edb1d5a18fa006/vibe/app_server/protocol.py#L167-L217)
- typed session start/resume/fork:
  [`protocol.py:361`](https://github.com/mistralai/mistral-vibe/blob/2817f3df81ae05d49ba9538262edb1d5a18fa006/vibe/app_server/protocol.py#L361-L448)
- queue/start/steer/interrupt schemas and expected-turn fields:
  [`protocol.py:1758`](https://github.com/mistralai/mistral-vibe/blob/2817f3df81ae05d49ba9538262edb1d5a18fa006/vibe/app_server/protocol.py#L1758-L1886)
- public turn status and identity:
  [`models.py:353`](https://github.com/mistralai/mistral-vibe/blob/2817f3df81ae05d49ba9538262edb1d5a18fa006/vibe/app_server/models.py#L353-L369),
  [`models.py:1116`](https://github.com/mistralai/mistral-vibe/blob/2817f3df81ae05d49ba9538262edb1d5a18fa006/vibe/app_server/models.py#L1116-L1157)
- session facade active turn/start/queue/interrupt:
  [`session.py:295`](https://github.com/mistralai/mistral-vibe/blob/2817f3df81ae05d49ba9538262edb1d5a18fa006/vibe/app_server/session.py#L295-L379),
  [`session.py:404`](https://github.com/mistralai/mistral-vibe/blob/2817f3df81ae05d49ba9538262edb1d5a18fa006/vibe/app_server/session.py#L404-L560),
  [`session.py:659`](https://github.com/mistralai/mistral-vibe/blob/2817f3df81ae05d49ba9538262edb1d5a18fa006/vibe/app_server/session.py#L659-L697)
- public `vibe-app-server` executable:
  [`pyproject.toml:153`](https://github.com/mistralai/mistral-vibe/blob/2817f3df81ae05d49ba9538262edb1d5a18fa006/pyproject.toml#L153-L156)
- stdio transport entrypoint:
  [`stdio.py:18`](https://github.com/mistralai/mistral-vibe/blob/2817f3df81ae05d49ba9538262edb1d5a18fa006/vibe/app_server/stdio.py#L18-L68)

## Current Theater state

- [`vibe/manifest.py`](../../theater/harness/builtin/plugins/vibe/manifest.py#L98)
  has no `RuntimeManifest`; it observes durable Vibe data and declares a legacy
  Escape interrupt.
- Vibe's unified store/source implementation already provides substantial durable
  observation in
  [`unified_source.py`](../../theater/harness/builtin/plugins/vibe/unified_source.py)
  and
  [`unified_store.py`](../../theater/harness/builtin/plugins/vibe/unified_store.py).
- Launch and resume stay stock-CLI driven in
  [`vibe/launch.py`](../../theater/harness/builtin/plugins/vibe/launch.py).

This plan must not destabilize that observation path while an attachment mechanism
is unproven.

## Topology blocker

Vibe's own app-server ADR states:

> One `AppServer` instance owns one attached root runtime and its child-session
> registry. The protocol does not currently model several simultaneous attached
> observers of one runtime.

See
[`docs/adr/0009-app-server-boundary.md:143`](https://github.com/mistralai/mistral-vibe/blob/2817f3df81ae05d49ba9538262edb1d5a18fa006/docs/adr/0009-app-server-boundary.md#L143-L167).
The same connection both opens the session and consumes canonical state/events.
The server also issues client-directed callback and `clientTool/*` requests. Those
requests need one owner for approvals, filesystem/terminal tools, and semantic
responses; they cannot safely be broadcast to two clients.

Consequences:

- Theater cannot launch a second `vibe-app-server` and control the session shown in
  the existing pane; that would be a different runtime/session attachment.
- Theater cannot simply attach a second JSON-RPC client to the app server used by
  the stock UI; the protocol does not support it.
- ACP does not solve this topology. It would still own or create a separate client
  session unless Vibe exposes shared attachment semantics.
- A transparent proxy is not just request forwarding. It must multiplex request
  IDs, state event watermarks, callback ownership, client tools, initialization,
  reconnect, and backpressure while preserving exactly one visible UI. That is a
  new protocol product with a large drift and safety surface.

## Phase 0 — upstream attachment proof

Perform this check against each candidate Vibe release before implementation:

1. Inspect the public method catalogue and ADRs for multi-client/shared-session
   attachment, observer roles, or a frontend plugin API.
2. Confirm whether the stock TUI can be pointed at a Theater-owned app server while
   Theater also has an official control channel that is not a second attached
   client.
3. Confirm who owns server-to-client `callback/call` and `clientTool/*` requests.
4. Confirm that a control observer receives session/turn events and can reconnect
   from an event watermark without stealing UI ownership.
5. Confirm stock UI and Theater controls share the exact session and turn IDs.
6. Build a minimal no-mutation prototype that observes one stock-UI-created turn
   through the official surface.

Record the result in `tests/fixtures/vibe_app_server/README.md` with version, commit,
initialization exchange, topology diagram, callback ownership, and a clear pass/fail
statement. Do not add a runtime manifest on a failed proof.

### Go criteria

At least one upstream-supported shape must exist:

- **observer/control role:** a second client may subscribe and send controls while
  the TUI remains callback owner;
- **stock-TUI extension:** an official in-process extension can receive Theater's
  authenticated local requests and call the app-server facade;
- **shared broker:** Vibe officially owns the multiplexing and documents request,
  callback, and reconnect semantics.

Anything requiring monkey-patching the Textual UI, importing private modules,
screen scraping, or maintaining a Theater JSON-RPC multiplexer is a no-go for the
default adapter.

## Preferred implementation after a successful proof

The exact file shape depends on the upstream attachment surface. Keep the common
runtime contract identical either way.

### Backend/shared-client shape

If Vibe officially permits Theater to own/control the app server while the stock UI
attaches:

- add `vibe/runtime_plan.py` for exact-version probing and pure backend/frontend
  plans;
- add `vibe/runtime.py` implementing `HarnessRuntime` over typed JSON-RPC;
- add `vibe/live.py` translating public state/events to Theater snapshots and
  `NativeTurnOutcome`;
- add a generic bounded stdio JSON-RPC transport only if Theater does not already
  have a suitable internal implementation. Keep it in Vibe's plugin unless another
  shipped runtime genuinely consumes the same protocol;
- declare a detached backend `RuntimeManifest` in `vibe/manifest.py` only when the
  stock `vibe` executable can officially attach to that backend.

### Frontend-extension shape

If Vibe adds an official stock-TUI extension API:

- add `vibe/frontend.py` to install a launch-local extension using the existing
  authenticated frontend Unix connection;
- add `vibe/runtime.py` to issue once-only frontend requests and decode typed
  receipts;
- preserve the stock launch plan and use `RuntimeHost.FRONTEND`;
- mirror OpenCode's peer/session epoch checks.

Do not choose a host shape from convenience; choose the one the tested public Vibe
release supports.

## Runtime semantics once topology is solved

### Send

Map Theater's operation ID to Vibe's `idempotency_key` and require the response's
`PublicTurn`:

```python
params = {
    "idempotency_key": operation_id,
    "session_id": expected_session_id,
    "message": [{"type": "text", "text": prompt}],
    "client_user_message_id": operation_id,
}
response = await request("turn/start", params)
turn = decode_public_turn(response["turn"])
require(turn.session_id == expected_session_id)
return ControlReceipt(
    operation_id=operation_id,
    result=DeliveryResult.ACCEPTED,
    native_turn_id=turn.id,
)
```

The snippet is illustrative. Use Vibe's negotiated protocol version and exact JSON
field aliases from the pinned release.

The runtime must reject ordinary send if the public session has an in-progress
turn. Do not silently reinterpret it as `session/turn/enqueue`.

### Follow-up queue

Prefer Theater ownership initially: dispatch its queue through `turn/start` after
idle. This preserves existing cancellation and job semantics across harnesses.

Vibe's native queue is strong enough for a later optimization, but adopting it
requires persisting both Theater queue sequence and Vibe `queue_item_id`, mapping
queue terminal/removal events, and making cancellation atomic across daemon restart.
Do not let both queues own the same follow-up.

### Steer and interrupt

Send `snapshot.native_turn_id` as Vibe's `expected_turn_id`. A Vibe stale-turn or
conflict response is a definite rejection; timeout after write is unknown. Accepted
receipts must name the expected turn.

For steer, use `turn/steer`, not queue-steer, unless Theater is explicitly promoting
a known Vibe queue item. For interrupt, preserve Vibe's exact-turn error distinctions
instead of collapsing them to “session not busy”.

### Observation and terminal evidence

Translate `PublicSessionState` and event patches into:

- exact native session ID;
- active in-progress `PublicTurn.id`;
- execution state;
- pending callback as `NativeHumanInteraction` when safely representable;
- effective model/settings;
- terminal `NativeTurnOutcome` for completed, failed, and interrupted turns.

Use event watermarks for ordered recovery and call `session/read` after reconnect.
Historical terminal state may reconcile an exact existing job but must not be
treated as a fresh interrupt of current work.

## Alternative opt-in mode: Theater-owned app-server

A headless native Vibe runtime is technically feasible now:

```text
Theater daemon -> vibe-app-server (stdio) -> Vibe runtime
```

It could support nearly the full capability map using only public APIs. It would not
preserve the ordinary stock Textual UI, so it is not a replacement for the current
harness plugin. Consider it only as an explicit launch mode such as
`frontend="theater"` after the cross-harness native work has proved demand.

That separate proposal must include a UI/approval story: Theater would have to own
callback responses, client tools, approval rendering, and user input. It should not
be smuggled into an adapter compatibility change.

## Test plan after attachment passes

Add:

- `tests/test_vibe_native_runtime.py` for typed request/response, identity,
  capabilities, settings, errors, and once-only semantics;
- `tests/fixtures/vibe_app_server/` with negotiated schemas/events from the exact
  release;
- `tests/test_vibe_native_runtime_proof.py` for executable stdio conformance if the
  backend shape is selected;
- `tests/test_vibe_stock_ui.py` for opt-in end-to-end proof that the UI and Theater
  see/control the same session;
- control-service cases for send, queue, steer, interrupt, stale turn, human-created
  turn, terminal evidence, timeout, and restart reconciliation.

The stock test must exercise a server-to-client callback while Theater observes or
controls. That is the topology's hardest case; a simple text-only turn is not enough.

## Release and stop gates

Ship no native route until all are true:

- upstream explicitly supports the selected multi-client/extension topology;
- the stock UI remains attached and owns the intended callbacks;
- exact session/turn IDs are shared;
- unknown writes are never replayed or sent through legacy fallback;
- reconnect and event watermark recovery are proven;
- the version probe fails closed outside the tested release.

Stop if satisfying these gates requires a Theater-maintained proxy or upstream
fork. The Vibe protocol is valuable evidence for what a good harness control API
looks like, but protocol quality does not compensate for attaching to the wrong
runtime.
