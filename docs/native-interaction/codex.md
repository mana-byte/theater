# Codex native interaction plan

## Recommendation

Treat Codex as the shipped reference adapter, not new feature work. It already
proves Theater's preferred architecture: a private app-server backend, the unmodified
stock Codex TUI attached with `--remote`, exact thread/turn IDs, acknowledged native
controls, and live terminal evidence.

The next work is compatibility hardening and a repeatable release-upgrade process.
Do not broaden the version range speculatively. Theater currently verifies exactly
Codex `0.154.0`.

Upstream source was inspected at commit
[`968835997714baaff199cfed5f89a2c65d8ca77d`](https://github.com/openai/codex/tree/968835997714baaff199cfed5f89a2c65d8ca77d).
That checkout demonstrates the public protocol and ongoing API evolution; it is not
automatically the source matching Theater's verified `0.154.0` fixture.

## Capability mapping

| Theater capability | Codex app-server method/event | Status |
| --- | --- | --- |
| Start session | `thread/start`; UI-created thread discovery | Shipped |
| Resume session | `thread/resume` | Shipped |
| Fork session | `thread/fork` | Shipped |
| Send new turn | `turn/start`; returns exact turn | Shipped |
| Queue follow-up | Theater queue dispatches through `turn/start` | Shipped |
| Steer active turn | `turn/steer` with expected/current turn identity | Shipped |
| Interrupt | `turn/interrupt` for exact turn | Shipped |
| Settings | verified release's `thread/settings/update` plus `thread/read` readback | Shipped, backend-gated |
| Status/turns | `thread/status/changed`, `turn/started`, `turn/completed` | Shipped |
| Content/tools | item start/completion/delta events | Shipped |
| Human interaction | native server requests for approvals/clarifications | Shipped observation; native UI remains owner |

The inspected upstream method catalogue includes thread lifecycle at
[`common.rs:559`](https://github.com/openai/codex/blob/968835997714baaff199cfed5f89a2c65d8ca77d/codex-rs/app-server-protocol/src/protocol/common.rs#L559-L575),
thread reads at
[`common.rs:837`](https://github.com/openai/codex/blob/968835997714baaff199cfed5f89a2c65d8ca77d/codex-rs/app-server-protocol/src/protocol/common.rs#L837-L852),
and turn controls at
[`common.rs:1023`](https://github.com/openai/codex/blob/968835997714baaff199cfed5f89a2c65d8ca77d/codex-rs/app-server-protocol/src/protocol/common.rs#L1023-L1044).
Turn schemas and statuses live in
[`v2/turn.rs`](https://github.com/openai/codex/blob/968835997714baaff199cfed5f89a2c65d8ca77d/codex-rs/app-server-protocol/src/protocol/v2/turn.rs),
and lifecycle notifications are catalogued at
[`common.rs:1892`](https://github.com/openai/codex/blob/968835997714baaff199cfed5f89a2c65d8ca77d/codex-rs/app-server-protocol/src/protocol/common.rs#L1892-L1935).

## Current Theater implementation

- [`codex/manifest.py`](../../theater/harness/builtin/plugins/codex/manifest.py#L58)
  declares the native live channel and detached runtime without legacy fallbacks.
- [`codex/runtime_plan.py`](../../theater/harness/builtin/plugins/codex/runtime_plan.py#L28)
  defines policy `codex-appserver-0.154-verified` and accepts only `0.154.0`.
- The pure backend planner launches `codex app-server --listen` and the frontend
  planner launches the stock UI with `--remote` at
  [`runtime_plan.py`](../../theater/harness/builtin/plugins/codex/runtime_plan.py#L211).
- [`CodexRuntime.open_session`](../../theater/harness/builtin/plugins/codex/runtime.py#L231)
  handles UI-created, forked, and reconnected sessions.
- [`send`](../../theater/harness/builtin/plugins/codex/runtime.py#L381),
  [`steer`](../../theater/harness/builtin/plugins/codex/runtime.py#L420),
  [`interrupt`](../../theater/harness/builtin/plugins/codex/runtime.py#L456), and
  [`update_settings`](../../theater/harness/builtin/plugins/codex/runtime.py#L484)
  implement native controls.
- The runtime rechecks the version reported by the app-server handshake in
  [`_connect`](../../theater/harness/builtin/plugins/codex/runtime.py#L587).
- Subscription recovery, history reconciliation, exact terminal outcomes, native
  requests, and live source projection continue below
  [`_subscribe_after_rollout`](../../theater/harness/builtin/plugins/codex/runtime.py#L727).
- Protocol fixtures live in
  [`tests/fixtures/codex_native_runtime`](../../tests/fixtures/codex_native_runtime/).

## Invariants to preserve

### One backend, one stock UI, one exact thread

The daemon owns a participant-private app server. The ordinary Codex TUI attaches to
that endpoint. On initial launch, the UI may create the thread; Theater waits for
the exact non-ephemeral `thread/started` record with the expected canonical cwd.
Resume and fork reconcile the returned thread with the requested identity.

Never replace this with a second headless session beside the UI. Never select a
thread merely because it is the most recently listed thread.

### Once-only mutation

The control service persists an operation as dispatched before calling the runtime.
Codex `turn/start` receives the Theater operation ID as `clientUserMessageId` for
correlation, while the app-server-returned turn ID remains authoritative. A timeout
or lost connection after write stays `UNKNOWN`; there is no retry and no tmux
fallback.

The runtime's current send behavior at
[`runtime.py`](../../theater/harness/builtin/plugins/codex/runtime.py#L381) is the
reference for other harnesses:

```python
result = await self._request(
    "turn/start",
    {
        "threadId": session,
        "input": [{"type": "text", "text": prompt}],
        "clientUserMessageId": operation_id,
    },
)
turn_id = bounded_turn_id(result)
return ControlReceipt(
    operation_id=operation_id,
    result=DeliveryResult.ACCEPTED,
    native_turn_id=turn_id,
)
```

The real implementation's timeout, cancellation, connection, malformed result, and
subscription recovery handling must remain around this simplified shape.

### Exact turn and terminal evidence

Steer and interrupt target the turn read from the same runtime snapshot used by the
control service. A server refusal is rejected; an acknowledgement loss is unknown.
Completion comes from exact native turn events or bounded history reconciliation,
not from “thread became idle”.

Historical evidence may finish a known job after reconnect, but must not be treated
as a fresh UI interruption. Preserve `NativeTurnOutcome.from_history` semantics.

### Settings are confirmed state

The runtime mutates settings only while idle and performs native readback. It does
not report accepted when readback is missing or contradictory. Advertise the exact
supported fields through the shared settings-capability improvement in
[README](README.md#p2--report-supported-setting-fields).

## Work package 1 — shared reporting and interrupt safety

Codex benefits from the roadmap-wide core work even though its control transport is
already native:

- add the selected transport to capability RPC output, reporting
  `native_runtime` and host `detached_backend` for Codex controls;
- add supported settings fields so clients can distinguish model and reasoning
  support;
- complete the shared legacy interrupt/queue race fix for other adapters without
  regressing native Codex interruption;
- preserve `NATIVE_RUNTIME` in durable control operation records; runtime host is
  launch metadata and does not justify rewriting historical rows.

Run existing control service and Codex smoke tests after these changes.

## Work package 2 — repeatable release upgrades

### 1. Select one candidate release

Record:

- exact `codex --version` output;
- package/source revision and platform;
- app-server `initialize` response and `userAgent`;
- generated protocol schema or a hash of the relevant public schema;
- the date and command used to run stock conformance.

Do not use the branch head or semver proximity as compatibility evidence.

### 2. Diff the exercised protocol

Compare the candidate against the last verified fixture for:

- initialization dialect and required capabilities;
- `thread/start`, `thread/resume`, `thread/fork`, and `thread/read`;
- `turn/start`, `turn/steer`, and `turn/interrupt` request/response fields;
- settings method name, fields, and readback representation;
- thread/turn/item notifications;
- server requests for approvals and clarification;
- error codes for busy, stale turn, missing thread, and unsupported methods;
- UI `--remote` behavior and exact thread selection.

The inspected newer source already illustrates why this matters: it catalogues an
experimental `turn/settings/update` method, while Theater's verified implementation
uses `thread/settings/update`. Do not update the method based on source inspection
alone; verify the candidate binary end-to-end.

### 3. Refresh fixtures

Update or add files under
[`tests/fixtures/codex_native_runtime`](../../tests/fixtures/codex_native_runtime/):

- `installed_release.json`;
- `handshake.json`;
- `capabilities.json`;
- `thread_lifecycle.json`;
- `turn_control.json`;
- `approval.json`;
- `unsupported_capabilities.json`;
- `protocol_schema/` only for the schema actually exercised.

Keep captured data deterministic and redact account, path, and authentication
material. If two versions need different valid protocol shapes, add versioned
fixture subdirectories rather than making one permissive fixture that proves
neither.

### 4. Adapt narrowly

Edit only the relevant boundaries:

- [`runtime_plan.py`](../../theater/harness/builtin/plugins/codex/runtime_plan.py#L28)
  for the policy name and exact verified set;
- [`runtime.py`](../../theater/harness/builtin/plugins/codex/runtime.py#L180) for
  version-dispatched request/notification decoding when required;
- a new small `protocol_<version>.py` sibling if branching would otherwise spread
  through runtime state management;
- [`manifest.py`](../../theater/harness/builtin/plugins/codex/manifest.py#L115) only
  if a capability must become gated/fallback for the candidate.

Avoid `if version >= ...` throughout the runtime. Parse the handshake once, select a
frozen dialect/capability descriptor, and keep state/control logic version-agnostic.

Illustrative descriptor:

```python
@dataclass(frozen=True, slots=True)
class CodexDialect:
    versions: frozenset[str]
    settings_method: str
    supports_settings: bool
    interrupt_uses_expected_turn: bool
```

Add this only when a second verified release actually differs; do not abstract one
implementation preemptively.

### 5. Extend the exact version set last

Only after fixture, unit, daemon, and stock UI proof passes, add the version to
`CODEX_RUNTIME_VERIFIED_VERSIONS` and update the policy label. Keep handshake
verification: it catches a binary replacement between the read-only spawn probe and
runtime connection.

## Test plan

Existing focused suites:

- [`tests/test_codex_native_runtime_plugin.py`](../../tests/test_codex_native_runtime_plugin.py)
  — runtime protocol and state behavior;
- [`tests/test_codex_native_runtime_proof.py`](../../tests/test_codex_native_runtime_proof.py)
  — installed app-server conformance;
- [`tests/test_codex_native_ui_bootstrap_proof.py`](../../tests/test_codex_native_ui_bootstrap_proof.py)
  — stock UI/private backend topology;
- [`tests/test_codex_native_daemon_smoke.py`](../../tests/test_codex_native_daemon_smoke.py)
  — daemon integration;
- [`tests/test_codex_rollout_race_recovery.py`](../../tests/test_codex_rollout_race_recovery.py)
  — race and history recovery;
- [`tests/test_codex_process_correlation.py`](../../tests/test_codex_process_correlation.py)
  — process/session ownership.

Every candidate release must cover:

1. initialize/initialized and reported-version match;
2. stock UI creates or resumes the exact thread;
3. idle native send returns a unique turn and completes the matching job;
4. simultaneous human/native input cannot bind two jobs to one turn;
5. steer changes only the expected active turn;
6. stale steer and stale interrupt reject without affecting the replacement turn;
7. interrupt cancels queued Theater follow-ups and yields exact terminal evidence;
8. model/reasoning changes are idle-only and confirmed by readback;
9. approval/clarification requests appear without Theater stealing UI ownership;
10. disconnect before/after write, timeout, daemon restart, history replay, and
    subscription loss preserve no-retry semantics;
11. unsupported release selects legacy under `auto` and fails explicit native with
    the recorded reason.

Run at minimum:

```sh
uv run pytest tests/test_codex_native_runtime_plugin.py \
  tests/test_codex_rollout_race_recovery.py tests/test_codex_process_correlation.py \
  tests/test_control_service.py
```

Then run the repository's documented opt-in proof commands/environment for the
candidate stock binary. Do not turn proof tests on unconditionally if they require a
locally installed release or real tmux.

## Release and rollback gates

A Codex release is supported only after all exercised methods, events, errors,
recovery cases, and stock UI attachment pass. If one capability drifts independently
(for example settings becomes experimental or unavailable), fail that capability
closed rather than disabling known-safe send/interrupt without evidence that they
also drifted.

Rollback by removing the release from the exact verified set or gating only the
affected capability. Never loosen handshake validation, accept a broad untested
range, or reinterpret an old `UNKNOWN` operation after rollback.

## Worth judgment

Codex already validates the product thesis: native control can coexist with the
vendor UI and can be safer and more expressive than key injection. Further feature
expansion here has lower value than delivering OpenCode send or running the Pi
proof. The worthwhile recurring investment is compatibility automation that keeps
this reference implementation trustworthy as Codex evolves.
