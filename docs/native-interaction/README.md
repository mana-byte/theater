# Native harness wiring

Status: implementation record for `1.0.0rc9`.

## Decision

Keep native semantic transport as the preferred route, but do not promise universal
native parity. Theater should use a harness-owned API when it can identify and
acknowledge the exact mutation. Guarded tmux input remains an explicit compatibility
route where the stock harness exposes no equivalent.

The release reached five bounded outcomes:

1. Claude Code's stock messaging socket failed the required delivery proofs, so its
   controls remain legacy;
2. Pi gained authoritative active-run identity and exact native interrupt;
3. OpenCode moved to its official detached `serve`/`attach` topology;
4. Codex gained repeatable release qualification without weakening its version gate;
5. Vibe remains fail-closed until it exposes a usable multi-client attachment.

This improves Theater's local cross-harness control plane. It is not a cluster
scheduler and does not require every capability to stop using tmux.

## Shipped result

| Harness | `1.0.0rc9` route | tmux retained |
| --- | --- | --- |
| Claude Code | Transcript/hooks observation; messaging proof recorded as a no-go | Send and interrupt |
| Codex | Detached app-server; native send, steer, interrupt, settings, events, and terminal evidence | Only explicit legacy mode |
| OpenCode | Detached authenticated `serve`; native send/follow-up, SSE lifecycle, durable database completion, stock `attach` UI | Interrupt |
| Pi | Stock-TUI extension; native send/follow-up, exact-run interrupt, reasoning setting, and lifecycle | Only explicit legacy mode; steer remains unavailable |
| Vibe | Durable observation; native topology proof remains a no-go | All controls |

Detailed plans: [Claude Code](claude.md), [Codex](codex.md),
[OpenCode](opencode.md), [Pi](pi.md), and [Vibe](vibe.md).

## Route and receipt contract

Choose a route per capability, not per harness:

1. release-qualified native backend or frontend API;
2. ACP only when it supplies the identity, acknowledgement, and semantics needed by
   that capability;
3. guarded `tmux send-keys`;
4. unavailable.

The shipped routing model already supports this split through
[`RuntimeManifest.legacy_fallback`](../../theater/harness/contracts/runtime.py#L807)
and [`ControlRouter`](../../theater/daemon/controls/routing.py#L45). Capability
reporting already exposes the selected transport, runtime host, and supported setting
fields in [`daemon/rpc/controls.py`](../../theater/daemon/rpc/controls.py#L155).

Every native mutation carries one bounded `operation_id` and ends in exactly one of
these meanings:

- `ACCEPTED`: the harness admitted the operation and Theater validated its native
  session and, where relevant, exact native turn identity;
- `REJECTED`: the harness established that it did not apply the mutation;
- `UNKNOWN`: a write, timeout, disconnect, malformed reply, or identity transition
  prevents Theater from knowing whether it applied the mutation.

`UNKNOWN` is terminal for dispatch. Never retry it automatically, turn it into a
legacy key injection, or release a second copy from Theater's queue. The frontend
request cache in
[`frontend_requests.py`](../../theater/daemon/harness_runtime/frontend_requests.py#L33)
is the reference once-only behavior.

Session identity is checked by both the daemon and adapter immediately before a
mutation. Steer and interrupt additionally require the exact active
`native_turn_id`. Socket availability, a busy flag, prompt text, timestamps, or “the
next event” are not identities.

## Shared architecture impact

Pi uses the authenticated frontend runtime path. Claude did not gain a runtime because
its stock messaging surface failed the conformance gates. OpenCode is the only transport
expansion: the detached runtime now supports a bounded stdout-discovered loopback HTTP
endpoint in addition to Codex's known Unix WebSocket endpoint.

The shared detached-runtime pieces are:

- [`RuntimePlan`](../../theater/harness/contracts/runtime.py#L439) declares either a
  fixed endpoint or bounded stdout discovery;
- [`spawning/native.py`](../../theater/daemon/spawning/native.py#L250) waits for fixed
  Unix endpoints while the backend launcher owns stdout discovery;
- [`WebSocketRuntimeIO`](../../theater/daemon/harness_runtime/transport.py#L698)
  speaks Codex JSON-RPC WebSocket, while OpenCode's bounded HTTP/SSE client stays in
  its plugin;
- [`runtime/wiring.py`](../../theater/daemon/runtime/wiring.py#L38) creates the Unix
  endpoint;
- [`server.py`](../../theater/daemon/server.py#L215) composes the transport.

OpenCode route names and event formats remain inside its plugin. Endpoint recovery and
exact correlation fit the existing runtime binding and native evidence records, so no
database migration was required.

## Implementation order

The work landed in this order:

1. Claude stock-binary proof and fail-closed no-go;
2. Pi active-run identity, interrupt, and independent steer no-go;
3. OpenCode endpoint discovery, HTTP/SSE runtime, stock proof, and production cutover;
4. Codex qualification fixtures and interaction-state hardening;
5. Vibe topology evidence refresh and fail-closed no-go.

## Cross-harness acceptance matrix

Every implemented native route must pass the applicable unit tests and a conformance
run against the exact stock binary release.

| Scenario | Required result |
| --- | --- |
| Idle send | One operation creates one user input and one correlated native turn |
| Busy ordinary send | Reject without mutation unless the adapter explicitly defines and reports queue semantics |
| Follow-up dispatch | Theater retains one queue owner; one released item creates one native turn |
| Human input race | A human-created/replacement turn cannot inherit Theater's receipt or job |
| Session transition | Reject before mutation, or return `UNKNOWN` after a possibly applied mutation |
| Duplicate operation | Return the cached terminal receipt; never mutate twice |
| Timeout/disconnect after write | Return `UNKNOWN`; never replay through native or tmux |
| Stale steer/interrupt | Reject without affecting the current or next turn |
| Fast settlement | Admission and terminal evidence remain correlated when the turn ends before the response |
| Daemon restart | Adopt or reconcile the exact runtime/session without duplicating a control |
| Frontend/backend restart | Advance generation/epoch and reject stale requests |
| Event overflow/reconnect | Recover from durable state or fail closed; never infer missing mutations |
| Human presence | Safe native controls follow their proven semantics; every legacy key path still refreshes and enforces absence |
| Secret handling | Credentials are bounded, private, redacted from logs/repr, and removed with participant runtime state |
| Version drift | Compatibility probe disables native routing outside explicitly qualified releases |

## Completion result

Each harness reached an evidence-backed result; native parity was not forced:

- Claude records a current stock-binary no-go;
- Pi ships native send, follow-up delivery, reasoning updates, and exact-run interrupt;
- OpenCode uses `serve`/`attach` for send and observation, with abort still legacy;
- Codex has a reproducible qualification artifact for every allowed release;
- Vibe's fixture names the inspected release and prevents speculative runtime wiring.

## Non-goals

- cluster placement, distributed scheduling, subscription pooling, or remote workers;
- a universal ACP layer or lowest-common-denominator capability set;
- forks, private monkey-patches, screen-scraped control, or undocumented protocol
  dependencies presented as stable integrations;
- replacing the stock TUI with a Theater-owned headless UX by default;
- fake parity: a capability remains legacy or unavailable when its native surface
  cannot meet Theater's identity and acknowledgement contract.
