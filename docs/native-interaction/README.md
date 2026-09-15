# Native harness wiring: phase two

Status: implementation plan. Phase one is present on `feat/native-adapters`; the
capabilities in the target column below are not shipped until their harness plan's
release gate passes.

## Decision

Keep native semantic transport as the preferred route, but do not promise universal
native parity. Theater should use a harness-owned API when it can identify and
acknowledge the exact mutation. Guarded tmux input remains an explicit compatibility
route where the stock harness exposes no equivalent.

Phase two has five bounded outcomes:

1. prove and, only if proven, add idle native send for Claude Code;
2. give Pi an authoritative active-run identity, then add exact native interrupt;
3. move OpenCode from an in-TUI control bridge to its official server/attach topology;
4. make Codex release qualification repeatable without weakening its version gate;
5. keep Vibe fail-closed while monitoring for an official multi-client attachment.

This improves Theater's local cross-harness control plane. It is not a cluster
scheduler and does not require every capability to stop using tmux.

## Shipped baseline and phase-two target

| Harness | Baseline on this branch | Phase-two target | tmux retained |
| --- | --- | --- | --- |
| Claude Code | Transcript/hooks observation; legacy send and Escape interrupt; no runtime | Stock-binary proof of the messaging socket, then authenticated idle native send with exact transcript correlation | Busy delivery, steer, and interrupt |
| Codex | Detached app-server; native send, steer, interrupt, settings, events, and terminal evidence | Release-qualification workflow and carefully expanded exact-version allowlist | Only when the user explicitly selects legacy mode |
| OpenCode | Stock-TUI frontend bridge; native send and Theater-owned follow-up queue; legacy interrupt; steer/settings unavailable | Detached official `opencode serve`, authenticated HTTP/SSE runtime, stock `opencode attach` UI, then proof-gated abort | Interrupt unless exact active-turn race proof passes |
| Pi | Stock-TUI extension; native send/queue and reasoning setting; legacy interrupt; steer unavailable | Active-run identity for human and Theater turns, exact `ctx.abort()` interrupt, independent steer proof | Interrupt/steer until their individual proofs pass |
| Vibe | Durable observation; legacy send and Escape interrupt; topology proof fails closed | Update the release proof only; implement nothing until Vibe supports an observer/control client, stock-TUI extension, or shared broker | All controls |

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

Claude and Pi fit the existing authenticated frontend runtime path. Their adapters
should not add another generic control protocol. Claude may require one additive
frontend-overlay facility because
[`RuntimeFrontendOverlay`](../../theater/harness/contracts/runtime.py#L726) can add
files but cannot yet safely amend the launch-local `claude.settings.json` that already
contains receipt hooks.

OpenCode is the only planned transport expansion. The existing detached runtime path
assumes a known Unix WebSocket endpoint:

- [`RuntimePlan`](../../theater/harness/contracts/runtime.py#L439) stores the endpoint
  before launch;
- [`spawning/native.py`](../../theater/daemon/spawning/native.py#L250) waits for that
  endpoint;
- [`WebSocketRuntimeIO`](../../theater/daemon/harness_runtime/transport.py#L698)
  speaks JSON-RPC WebSocket;
- [`runtime/wiring.py`](../../theater/daemon/runtime/wiring.py#L38) creates the Unix
  endpoint;
- [`server.py`](../../theater/daemon/server.py#L215) composes the transport.

The OpenCode work therefore needs a narrow lifecycle extension for stdout-discovered
loopback endpoints and plugin-local bounded HTTP/SSE I/O. Do not put OpenCode route
names or event formats into generic daemon code.

No database migration is expected. If endpoint recovery or exact correlation cannot
use the existing runtime binding and native evidence records, stop and design an
explicit Alembic migration rather than hiding durable state in payload text or memory.

## Dependency order

Implement in this order:

1. **Claude proof.** The documented socket is new and the complete user frame is not
   public. A stock-binary conformance result decides whether any Claude production
   runtime is written.
2. **Pi active-run identity and interrupt.** This is localized to the existing bridge
   and can remove a meaningful legacy path if exact identity survives races.
3. **OpenCode topology migration.** This is the largest change and the only shared
   transport/lifecycle work. Preserve the shipped frontend route until parity passes.
4. **Codex release hardening.** Keep the known-good reference stable while building a
   repeatable qualification workflow.
5. **Vibe monitoring.** Refresh evidence, but do not spend implementation effort on a
   Theater-owned multiplexer.

Claude and Pi proofs may be developed independently. OpenCode must not remove its
current TUI bridge until server/attach recovery and parity tests pass. Vibe has no
implementation dependency because its current result is deliberately “no-go.”

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

## Completion criteria

Phase two is complete when each harness reaches its own honest terminal result, not
when every cell is native:

- Claude either ships proven idle send or records a current stock-binary no-go;
- Pi ships only the controls whose exact-run tests pass;
- OpenCode uses `serve`/`attach` for send and observation, with abort separately gated;
- Codex has a reproducible qualification artifact for every allowed release;
- Vibe's fixture names the current inspected release and still prevents speculative
  runtime wiring when topology fails.

## Non-goals

- cluster placement, distributed scheduling, subscription pooling, or remote workers;
- a universal ACP layer or lowest-common-denominator capability set;
- forks, private monkey-patches, screen-scraped control, or undocumented protocol
  dependencies presented as stable integrations;
- replacing the stock TUI with a Theater-owned headless UX by default;
- fake parity: a capability remains legacy or unavailable when its native surface
  cannot meet Theater's identity and acknowledgement contract.
