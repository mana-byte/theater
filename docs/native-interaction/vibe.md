# Vibe native wiring investigation record

## Outcome sought

Do not implement a Vibe native runtime in phase two. Replace speculative adapter work
with a cheap, repeatable release-monitoring proof. The proof should make Theater fail
visibly if Vibe's topology changes, so implementation can begin at the right time.

Vibe's app-server protocol has excellent control semantics. The blocker is ownership:
the stock TUI and Theater cannot currently be independent attached clients of the
same runtime.

## Current baseline

- [`vibe/manifest.py`](../../theater/harness/builtin/plugins/vibe/manifest.py#L98)
  declares no runtime and retains legacy Escape interrupt at line 147.
- Existing durable observation remains in
  [`unified_source.py`](../../theater/harness/builtin/plugins/vibe/unified_source.py)
  and [`unified_store.py`](../../theater/harness/builtin/plugins/vibe/unified_store.py).
- [`test_vibe_native_topology_proof.py`](../../tests/test_vibe_native_topology_proof.py)
  asserts that no runtime is installed and reads the pinned evidence fixture.
- [`topology.json`](../../tests/fixtures/vibe_app_server/topology.json) currently pins
  `2.25.4` at commit `19b5b74faa78d0816b8d4d4c7d7543fc3520678c`.

Do not disturb the observation path while attachment remains unproven.

## Current upstream result

The newer `v2.25.4` version of Vibe's
[`app-server boundary ADR`](https://github.com/mistralai/mistral-vibe/blob/v2.25.4/docs/adr/0009-app-server-boundary.md)
still states that the protocol does not model several simultaneous attached observers
of one runtime.

That restriction is decisive because the connection also participates in state,
callbacks, client tools, and approval behavior. A second app-server would control a
different runtime. A transparent JSON-RPC proxy would need to own and multiplex
request IDs, callback routing, client tools, state watermarks, reconnect,
backpressure, and approval ownership. That is a new protocol product, not an adapter.

## Capability mapping

The protocol fit is strong if an official shared attachment appears:

| Theater capability | Vibe app-server surface | Current decision |
| --- | --- | --- |
| Session start/resume/read | `session/start`, `session/resume`, `session/read` | Blocked by ownership topology |
| Idle send | `turn/start` with idempotency key and returned `PublicTurn` | Blocked by ownership topology |
| Follow-up queue | `session/turn/enqueue` and queue item IDs | Keep Theater queue; no native route now |
| Steer | `turn/steer` with expected turn ID | Blocked by ownership topology |
| Interrupt | `turn/interrupt` with expected turn ID | Legacy Escape until attachment exists |
| Settings | `session/settings/update` plus readback | Blocked by ownership topology |
| Active turn/result | typed session/turn state and event watermark | Existing durable Theater observation only |
| Human interaction | server callbacks and `clientTool/*` | Stock UI must remain sole owner today |

Protocol quality does not make a connection to the wrong runtime useful.

## The only acceptable topology changes

Start implementation only if an upstream release provides at least one documented
shape:

1. **Observer/control role:** a second authenticated client can subscribe and issue
   controls while the stock TUI remains the sole callback/client-tool owner.
2. **Stock-TUI extension:** public extension code inside Vibe can connect to Theater's
   authenticated frontend endpoint and call supported current-runtime APIs.
3. **Shared broker:** Vibe itself supports multiple clients and defines request,
   callback ownership, event replay, and reconnect semantics.

An undocumented socket, private Python import, Textual monkey-patch, screen scraper,
or Theater-maintained JSON-RPC multiplexer does not satisfy the gate.

## Phase 0 — refresh the release fixture

Update the proof to inspect the latest candidate tag rather than treating `2.25.1` as
permanent. For each candidate release:

1. record exact version, git commit, source/archive digest, and inspection date;
2. inspect the app-server ADR and public protocol method catalogue;
3. inspect stock TUI startup to identify who creates the app server and owns its
   connection;
4. search for observer roles, attach/connect commands, frontend extensions, shared
   subscriptions, and event replay cursors;
5. identify ownership of every server-to-client callback and `clientTool/*` request;
6. run a no-mutation topology probe when the public CLI exposes a candidate path;
7. record pass/fail independently for the three acceptable shapes.

The refreshed fixture should stay small and machine-readable:

```json
{
  "version": "2.25.4",
  "commit": "<exact commit>",
  "adr": "docs/adr/0009-app-server-boundary.md",
  "goCriteria": {
    "observerControlRole": false,
    "stockTuiExtension": false,
    "sharedBroker": false
  },
  "callbackOwner": "stock client connection",
  "result": "failed: no supported stock-TUI attachment"
}
```

Update
[`test_vibe_native_topology_proof.py`](../../tests/test_vibe_native_topology_proof.py)
to assert the current fixture version/commit and the three booleans. Keep the manifest
assertions that `runtime is None` and Escape remains legacy while all are false.

The test should fail on a deliberately changed fixture result. That failure is the
signal to review upstream and write a new implementation plan; it must not
automatically enable a runtime.

## Phase 1 — no-mutation proof after a topology change

If one criterion becomes true, prove shared identity before implementing controls:

- launch one unmodified stock Vibe UI;
- attach Theater through only the new documented surface;
- observe a UI-created session and turn with the same exact IDs on both clients;
- disconnect/reconnect Theater without disturbing UI ownership;
- exercise at least one approval or client-tool callback and prove only the intended
  client answers it;
- recover event state from the documented watermark/readback mechanism;
- exit Theater and prove the stock UI/runtime remain usable.

Record sanitized initialization frames, ownership declarations, session/turn IDs,
watermark behavior, and the exact supporting upstream documentation. A text-only turn
without a callback does not pass this proof.

## Conditional implementation plan

Only after Phase 1 passes, replace this section with a plan for the actual supported
shape.

### If Vibe adds an observer/control client

- add `vibe/runtime_plan.py` for exact-version probing and backend/frontend plans;
- add `vibe/runtime.py` for typed JSON-RPC requests and strict receipts;
- add `vibe/live.py` for session/turn snapshots, watermarks, interactions, and
  `NativeTurnOutcome`;
- use Vibe's idempotency key for Theater `operation_id`;
- pass `expected_turn_id` for steer/interrupt and validate returned identity;
- keep Theater as initial queue owner; do not split follow-ups across both queues;
- add the runtime manifest only for the exact proven release.

### If Vibe adds a stock-TUI extension

- add `vibe/frontend.py` using Theater's authenticated frontend endpoint;
- render only public extension APIs and preserve the stock launch;
- reuse the bounded frontend request/operation cache;
- validate session and exact turn in the extension immediately before mutation;
- keep callback/approval ownership in the stock UI.

### If Vibe adds a shared broker

- use the broker's documented authentication and replay model;
- assign Theater an observer/control role with no approval/client-tool ownership;
- map broker connection generation and event watermark to runtime recovery;
- reject any release where role enforcement is advisory rather than server-side.

Whichever shape passes, implement send first, then observation/recovery, then exact
steer and interrupt. Do not infer all capabilities from the existence of one attach
method.

## Separate product option: Theater-owned headless Vibe

The current public protocol could support an explicit mode where Theater owns
`vibe-app-server` and every callback:

```text
Theater UI/control client -> vibe-app-server -> Vibe runtime
```

That would not preserve the stock Textual UI and would require Theater to own
approvals, client tools, prompts, and interaction rendering. Treat it as a separate
product proposal such as `frontend="theater"`, with its own UX and security review.
It is not phase-two native wiring and must not silently replace the existing Vibe
adapter.

## Verification

For the monitoring-only change:

```sh
uv run pytest tests/test_vibe_native_topology_proof.py
```

If topology later passes, minimum stock tests must cover shared session/turn identity,
callback ownership, reconnect/watermark recovery, duplicate operations, stale
expected-turn rejection, post-write `UNKNOWN`, UI survival, and exact-version
fail-closed selection.

## Acceptance and stop criteria

Phase two is complete for Vibe when the fixture points at the current inspected
release and truthfully records the topology result. No production runtime code is an
expected successful result while all three criteria remain false.

Stop if implementation requires a Theater proxy, Vibe fork, private import,
monkey-patch, or second runtime disguised as the stock session. Resume implementation
only when upstream owns the multi-client/extension contract and the no-mutation proof
demonstrates one shared session with unambiguous callback ownership.
