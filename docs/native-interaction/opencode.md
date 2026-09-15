# OpenCode native wiring: phase two

## Outcome sought

Replace the shipped in-TUI control bridge with OpenCode's official server topology:

```text
Theater daemon -> detached opencode serve --hostname=127.0.0.1 --port=0
               -> authenticated HTTP requests + SSE events
stock pane     -> opencode attach <discovered-url> --session <session-id>
```

This keeps the stock TUI while removing control delivery from TUI plugin internals.
Native send and live observation move first. Native abort remains separately gated on
exact active-turn identity; legacy Escape remains if the official API is only
session-scoped at the mutation point.

## Why replace the working bridge

OpenCode documents its normal architecture as client/server and supports multiple
clients. The public [`server`](https://opencode.ai/docs/server) and
[`CLI`](https://opencode.ai/docs/cli) surfaces provide:

- `opencode serve` and `opencode attach <url> --session <id>`;
- Basic authentication through `OPENCODE_SERVER_PASSWORD`;
- `/global/health`, `/event`, `/session/status`;
- `/session/:id/prompt_async` and `/session/:id/abort`.

The current bridge works, but it renders a plugin into every TUI and invokes the
in-process SDK client. The server topology is an upstream-owned multi-client boundary,
survives UI restarts, centralizes canonical events, and makes the daemon a first-class
client instead of relaying mutations through the editor process.

## Current baseline

- [`opencode/manifest.py`](../../theater/harness/builtin/plugins/opencode/manifest.py#L119)
  declares `RuntimeHost.FRONTEND`, native send, legacy interrupt, and unavailable
  steer/settings.
- [`opencode/runtime.py`](../../theater/harness/builtin/plugins/opencode/runtime.py#L53)
  implements the frontend runtime; native send begins at line 112.
- [`opencode/frontend.py`](../../theater/harness/builtin/plugins/opencode/frontend.py#L224)
  renders the TUI bridge around `api.client.session.promptAsync`.
- [`opencode/live.py`](../../theater/harness/builtin/plugins/opencode/live.py#L119)
  validates exact message lineage and reconciles the submitted turn at lines 207–247.
- [`opencode/runtime_plan.py`](../../theater/harness/builtin/plugins/opencode/runtime_plan.py#L12)
  qualifies only OpenCode `1.18.29` for the TUI bridge.
- [`opencode/launch.py`](../../theater/harness/builtin/plugins/opencode/launch.py#L25)
  still builds the ordinary standalone TUI launch.

Do not remove this route at the start. It is the parity oracle and rollback path until
the detached server passes stock-binary launch, recovery, and control tests.

## Upstream evidence to pin

At the inspected OpenCode source commit
[`c470c79513f78aabb2ff88a8c8f7a3a22c4e97af`](https://github.com/anomalyco/opencode/tree/c470c79513f78aabb2ff88a8c8f7a3a22c4e97af):

- attach accepts URL, session, password, and username in
  [`attach.ts:7`](https://github.com/anomalyco/opencode/blob/c470c79513f78aabb2ff88a8c8f7a3a22c4e97af/packages/opencode/src/cli/cmd/attach.ts#L7-L44);
- serve prints `opencode server listening on http://<host>:<port>` in
  [`serve.ts:6`](https://github.com/anomalyco/opencode/blob/c470c79513f78aabb2ff88a8c8f7a3a22c4e97af/packages/opencode/src/cli/cmd/serve.ts#L6-L23);
- loopback and port `0` are supported defaults in
  [`network.ts:6`](https://github.com/anomalyco/opencode/blob/c470c79513f78aabb2ff88a8c8f7a3a22c4e97af/packages/opencode/src/cli/network.ts#L6-L16);
- the official SDK discovers a port-0 server URL from that stdout line in
  [`sdk/js/src/server.ts`](https://github.com/anomalyco/opencode/blob/c470c79513f78aabb2ff88a8c8f7a3a22c4e97af/packages/sdk/js/src/server.ts);
- prompt and abort are server handlers in
  [`handlers/session.ts`](https://github.com/anomalyco/opencode/blob/c470c79513f78aabb2ff88a8c8f7a3a22c4e97af/packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts);
- `/event` installs its subscription before streaming events in
  [`handlers/event.ts`](https://github.com/anomalyco/opencode/blob/c470c79513f78aabb2ff88a8c8f7a3a22c4e97af/packages/opencode/src/server/routes/instance/httpapi/handlers/event.ts).

The conformance fixture must record the exact released package version corresponding
to the tested binary. A source commit alone does not qualify an installed release.

## Capability mapping

| Theater capability | Official server surface | Phase-two route |
| --- | --- | --- |
| Server health | `GET /global/health` | Native readiness and reconnect check |
| Session identity/state | session create/read and `GET /session/status` | Native, exact session ID |
| Idle send | `POST /session/:id/prompt_async` | Native after one-to-one message/turn proof |
| Follow-up queue | Theater queue, released through idle send | Keep Theater as sole queue owner |
| Live observation | `GET /event` SSE plus readback | Native; durable source remains fallback |
| Interrupt | `POST /session/:id/abort` | Native only if exact active-turn race proof passes |
| Steer | No proven exact equivalent | Unavailable |
| Settings update | No proven current-session atomic update | Unavailable initially |
| Stock UI | `opencode attach <url> --session <id>` | Native frontend plan |

## Phase 0 — stock topology proof

Create an opt-in conformance test and fixture before shared architecture changes. With
one stock binary, prove:

1. `serve --hostname=127.0.0.1 --port=0` prints exactly one parseable endpoint and
   binds only loopback.
2. Basic auth rejects missing/wrong credentials for health, session, prompt, abort,
   and event routes.
3. A session created through HTTP is the same session shown by
   `attach <url> --session <id>`.
4. Human TUI input and direct `prompt_async` appear in the same canonical event stream
   with stable session/message/part identities.
5. `prompt_async` response semantics establish whether admission happened; capture
   error bodies for busy, invalid session, provider refusal, and rate limit.
6. `/event` reconnect plus session/status/message readback can recover a missed
   terminal event without inventing a new turn.
7. The server remains alive and attach can reconnect after the UI exits.
8. The server remains usable after its launching parent daemon exits and a new client
   authenticates.

Record sanitized HTTP method/path/status, response shapes, SSE event types, identity
fields, stdout prefix, version, and platform under a new
`tests/fixtures/opencode_server_runtime/` directory. Never record the Basic password
or prompt contents containing user data.

Stop if attach creates a separate runtime/session, the API is not authenticated, or
the direct prompt cannot be correlated one-to-one with durable OpenCode history.

## Phase 1 — discovered endpoint lifecycle

The detached runtime currently requires a known Unix endpoint in
[`RuntimePlan`](../../theater/harness/contracts/runtime.py#L439), launches stdout
directly into a private append-only log in
[`backend.py`](../../theater/daemon/harness_runtime/backend.py#L473), and waits for a
Unix socket at
[`spawning/native.py`](../../theater/daemon/spawning/native.py#L250). OpenCode must
use port `0`; do not preselect a “free” TCP port and reopen it later.

Add one small harness-neutral endpoint-discovery contract. A suitable shape is:

```python
@dataclass(frozen=True, slots=True)
class RuntimeEndpointDiscovery:
    parser: Callable[[str], str | None]
    max_bytes: int

@dataclass(frozen=True, slots=True)
class RuntimePlan:
    backend: LaunchPlan
    endpoint: str | None = None
    endpoint_discovery: RuntimeEndpointDiscovery | None = None
```

Allow `NativeSpawnSelection.endpoint` and `RuntimePlanningContext.endpoint` to be
optional only for a detached manifest that declares discovery. Persist the initial
binding with `endpoint=None`; [`RuntimeBinding`](../../theater/harness/contracts/runtime.py#L453)
already permits that. Replace it with the discovered URL using a generation-checked
store update before runtime construction. Every fixed-endpoint path remains required
and unchanged.

The exact type names may vary, but preserve these invariants:

- exactly one of fixed endpoint and discovery is configured;
- core owns the deadline, bounded bytes/line length, log path, process identity, and
  persistence; the plugin owns parsing the documented stdout contract;
- record the stdout file offset before launch and parse only bytes written by this
  generation, because backend logs append;
- accept only an `http` URL with no credentials/path/query/fragment, literal loopback
  host, and valid nonzero port;
- reject multiple conflicting endpoints;
- terminate/reap the just-launched verified process if discovery fails;
- return the discovered endpoint from `launch_backend`, not merely the PID;
- persist it on the exact runtime-binding generation before any HTTP connection or UI
  attach;
- adoption after daemon restart uses only the persisted endpoint and re-verifies the
  process identity before connecting.

Primary shared files:

- `theater/harness/contracts/runtime.py`: optional endpoint-discovery contract;
- `theater/daemon/harness_runtime/backend.py`: bounded post-launch stdout discovery and
  returned endpoint;
- `theater/daemon/harness_runtime/manager.py`: preserve endpoint on launch/adoption;
- `theater/daemon/spawning/native.py`: replace fixed Unix readiness with the resolved
  endpoint and persist before continuing;
- runtime-binding repository methods: one generation-checked endpoint update;
- `tests/test_runtime_backend_process.py`, `tests/test_runtime_storage.py`, and
  `tests/test_runtime_lifecycle_integration.py`: stale log, conflicting line,
  timeout, invalid/non-loopback URL, process exit, persistence, and restart adoption.

Do not make `WebSocketRuntimeIO` accept HTTP URLs. Fixed Unix-WebSocket users must keep
their current behavior unchanged.

## Phase 2 — participant credential

Before launch, core mints a participant-scoped high-entropy password. Reuse the
security/cleanup model of
[`ChannelCredentialRepository`](../../theater/daemon/persistence/repositories/channels.py#L31):
the daemon persists what restart recovery needs, writes a `0600` participant-owned
file, and deletes it with participant state.

The plugin declares the credential need; it never chooses or logs token bytes. Extend
the detached planning contexts with only a `token_file: Path`, following
`RuntimeFrontendInstallContext`. Add a non-repr `LaunchPlan.secret_env` mapping from
environment variable name to private token path; the process launcher resolves it
immediately before `exec` without copying values into `LaunchPlan.env`. Both server
and attach plans use `{"OPENCODE_SERVER_PASSWORD": token_file}` through this seam,
and the runtime reads the same private file into a non-repr client credential.

Do not put the password in pane/backend argv, ordinary `LaunchPlan.env`, tmux history,
runtime binding, endpoint URL, diagnostics, or structured logs.

Mint and record the credential before the detached backend starts; the present
detached branch at
[`spawning/service.py`](../../theater/daemon/spawning/service.py#L205) bypasses the
frontend installer that normally creates live-channel credentials. If the existing
live-channel credential contract cannot be reused for a detached backend without
lying about its purpose, add a narrowly named runtime credential declaration. Do not
overload `receipt_token` and do not copy the secret into
`RuntimeBinding.launch_policy`.

Security tests must cover file mode, symlink refusal, log/repr redaction, mismatched
credential rejection, restart retrieval, and cleanup after participant death.

## Phase 3 — bounded HTTP/SSE I/O

Add plugin-local transport in, for example,
`theater/harness/builtin/plugins/opencode/http.py`. It may depend on a small generic
HTTP primitive, but OpenCode route names and event decoding stay in the plugin.

Required bounds and behavior:

- loopback endpoints only, with redirects disabled;
- Basic auth on every request and reconnect;
- explicit connect, request, header, body, SSE-event, idle, and total deadlines;
- bounded JSON body and SSE line/event buffers;
- only expected JSON content types and UTF-8;
- one owned SSE reader task, bounded event queue, clean cancellation, and no silent
  drop of identity/terminal events;
- reconnect with last known state reconciliation; use SSE event IDs only if the
  qualified release documents/replays them;
- redact auth headers, URL userinfo, prompt bodies, and response bodies from errors;
- distinguish response-before-write rejection from post-write ambiguity.

A plugin-facing interface can remain semantic rather than pretending HTTP is
JSON-RPC:

```python
class OpenCodeClient:
    async def health(self) -> None: ...
    async def create_session(self, *, title: str | None) -> str: ...
    async def prompt_async(self, session_id: str, body: Mapping[str, object]) -> object: ...
    async def abort(self, session_id: str) -> object: ...
    async def session_status(self) -> Mapping[str, object]: ...
    def events(self) -> AsyncIterator[Mapping[str, object]]: ...
```

Tests use a loopback fake server with real streaming and disconnect boundaries. Avoid
mocking away request-body write ambiguity.

## Phase 4 — `OpenCodeServerRuntime`

Add `opencode/server_runtime.py` and `opencode/server_live.py`; keep the current files
until cutover.

`open_session(NEW)` should create the session through the authenticated API, start SSE
before any prompt, read the session back, and return an exact `RuntimeBinding`.
`open_session(RESUME/FORK)` must use OpenCode's official session operations and confirm
the returned identity; do not reuse a predecessor ID when fork semantics create a new
one.

The current generic new-session sequence launches the UI before `open_session(NEW)` at
[`spawning/native.py`](../../theater/daemon/spawning/native.py#L276). Add a narrow
runtime launch-order declaration, for example
`RuntimeSessionOrder.FRONTEND_FIRST | SESSION_FIRST`, defaulting to the existing
`FRONTEND_FIRST`. OpenCode selects `SESSION_FIRST`; Codex remains unchanged. The
selected sequence is:

```text
launch server -> discover/persist endpoint -> connect/health -> subscribe SSE
-> create/resume/fork exact session -> persist session ID
-> build attach plan with exact URL/session -> launch pane -> verify attachment
-> dispatch initial Theater prompt exactly once
```

`frontend_plan()` returns the stock command and preserves model/approval semantics
supported by the server session:

```python
LaunchPlan(
    argv=["opencode", "attach", endpoint, "--session", native_session_id],
    env={"OPENCODE_SERVER_PASSWORD": password},
)
```

The real plan must use the private credential seam rather than exposing `password` as
an ordinary repr-able value. It must also retain Theater's MCP config and any launch
policy that OpenCode applies at server/session creation.

`snapshot()` combines `/session/status`, exact session readback, and ordered SSE state.
It exposes `IDLE` only when canonical status confirms no active run. On SSE loss it
reconciles through bounded reads; inability to prove state becomes `UNKNOWN`, not idle.

Preserve the existing server-side launch plugin that enforces approval and receipt
behavior until conformance demonstrates a public server/session equivalent. This
migration is about control transport, not weakening launch safety.

## Phase 5 — send and terminal evidence

Map one Theater `operation_id` to a stable OpenCode client message ID if the qualified
schema accepts one. Send only while exact session status is idle.

```python
body = {
    "messageID": operation_id,
    "parts": [{"type": "text", "text": prompt}],
}
response = await client.prompt_async(native_session_id, body)
```

The field names are illustrative; use generated/observed schema from the pinned
release. `ACCEPTED` requires:

1. an HTTP result that proves admission;
2. an exact user-message ID in API state/SSE;
3. a one-to-one assistant/turn lineage usable by `NativeTurnOutcome`.

Pre-write validation, authenticated 4xx conflicts, and known invalid session are
`REJECTED` only when the API guarantees no mutation. Connection loss, timeout, 5xx,
malformed body, or session drift after request bytes may have crossed are `UNKNOWN`.
Never replay those requests, including through the old TUI bridge.

Keep Theater as the only follow-up queue owner. The runtime rejects ordinary send
while busy; the control service releases one queued item after canonical idle and
receives a new exact native turn ID.

Translate SSE and readback into the existing OpenCode trajectory semantics. Reuse the
strict lineage rules in
[`opencode/live.py`](../../theater/harness/builtin/plugins/opencode/live.py#L119), but
do not feed server events through a fake TUI epoch. Historical reconciliation may
finish an exact existing job and must be marked as history.

## Phase 6 — proof-gated abort

`POST /session/:id/abort` is session-scoped. Theater interrupt is turn-scoped. Enable
native interrupt only if the qualified SSE/status model supplies an exact current
assistant/run ID and this race is impossible:

```text
Theater validates expected turn A -> A settles -> human starts turn B -> abort request hits B
```

The proof must identify an upstream atomic expected-turn guard, or demonstrate a
server serialization boundary where the ID check and abort occur as one operation.
A client-side check followed by HTTP abort is not atomic.

If the endpoint cannot accept `expected_turn_id`, retain legacy interrupt. Do not call
abort and label the result exact. If an exact mechanism exists, use the same
`operation_id` receipt rules: stale turn is `REJECTED`; uncertainty after write is
`UNKNOWN`; accepted names the interrupted turn and correlates terminal evidence.

## Phase 7 — cutover and deletion

After stock parity passes:

- change `opencode/manifest.py` to `RuntimeHost.DETACHED_BACKEND` with the server
  planner/runtime/live channel;
- replace the TUI compatibility policy with an exact server-topology policy;
- retain interrupt legacy fallback unless Phase 6 independently passes;
- delete control-specific rendering and protocol code from `opencode/frontend.py`;
- keep any launch plugin still required for approval/receipt enforcement;
- remove obsolete frontend fixtures only after equivalent server integration tests
  cover their safety properties.

Do not support both native OpenCode transports behind silent auto-selection. During
development keep the old path on the prior commit/feature flag; at release choose one
qualified route and fail closed outside it.

## Test plan

Minimum focused coverage:

- endpoint discovery: port 0, stale appended log, split line, oversized line,
  conflicting endpoints, non-loopback URL, early exit, timeout, persistence;
- credential lifecycle: Basic auth, `0600`, symlink rejection, redaction, restart,
  cleanup;
- HTTP/SSE: bounds, reconnect, backpressure, malformed JSON, event loss and readback;
- runtime: create/resume/fork, stock attach, UI restart, daemon restart/adoption, exact
  session snapshot;
- send: one operation/one message, duplicate call, busy rejection, timeout after body
  write, simultaneous UI/Theater input, fast completion, rate limit/provider error;
- queue: ordered release, cancellation, no double owner, restart before/after dispatch;
- abort: stale-turn replacement race before changing the manifest;
- compatibility: unqualified versions select legacy wiring.

Suggested new/updated files:

- `tests/test_opencode_server_proof.py`
- `tests/test_runtime_endpoint_discovery.py`
- `tests/test_opencode_http.py`
- `tests/test_opencode_server_runtime.py`
- `tests/test_runtime_lifecycle_integration.py`
- `tests/test_control_service.py`

Run focused tests plus the existing OpenCode suite during migration:

```sh
uv run pytest tests/test_opencode_frontend.py \
  tests/test_opencode_stock_ui.py \
  tests/test_opencode_server_proof.py \
  tests/test_opencode_http.py \
  tests/test_opencode_server_runtime.py \
  tests/test_runtime_lifecycle_integration.py \
  tests/test_control_service.py
```

## Acceptance and stop criteria

Cut over send/observation only when the exact stock release proves:

- server and UI share one session and canonical IDs;
- port-0 discovery is bounded, loopback-only, persisted, and restart-safe;
- authentication secrets never leak to argv/logs/repr;
- direct and human prompts remain distinguishable under concurrency;
- SSE reconnect/readback cannot duplicate or misattribute a job;
- `UNKNOWN` mutations are never replayed;
- approval, MCP, resume/fork, and stock UI behavior retain parity.

Keep the current frontend runtime if server/attach loses approval enforcement, exact
lineage, or restart semantics. Keep legacy interrupt if abort cannot atomically target
the expected turn. Either fallback is preferable to claiming native behavior the
official API cannot prove.
