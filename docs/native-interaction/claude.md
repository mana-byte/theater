# Claude Code native wiring: phase two

## Outcome sought

Add native **idle send** to the stock Claude Code session only if a stock binary proves
the complete local messaging exchange and exact transcript correlation. Keep busy
delivery, steer, and interrupt on their existing legacy/unavailable routes.

This is a proof-gated implementation, not permission to infer a protocol. Claude's
[cross-session messaging documentation](https://code.claude.com/docs/en/cross-session-messaging)
now establishes an authenticated per-session local socket, but does not document the
complete user-message frame Theater needs.

## Current baseline

- [`claude/manifest.py`](../../theater/harness/builtin/plugins/claude/manifest.py#L19)
  declares no runtime and retains the Escape interrupt at line 27.
- [`claude/launch.py`](../../theater/harness/builtin/plugins/claude/launch.py#L58)
  creates launch-local `SessionStart` and `PreCompact` receipt hooks.
- The same launch planner mints the Claude session ID and passes `--session-id` at
  [`launch.py`](../../theater/harness/builtin/plugins/claude/launch.py#L74).
- [`claude/trajectory.py`](../../theater/harness/builtin/plugins/claude/trajectory.py#L77)
  derives user-turn identity from `promptId` or the user record UUID.
- [`test_claude_native_control_proof.py`](../../tests/test_claude_native_control_proof.py)
  pins the fail-closed manifest and the harness in
  [`tests/native/claude_messaging_client.py`](../../tests/native/claude_messaging_client.py),
  while
  [`messaging_conformance.json`](../../tests/fixtures/claude_native_control/messaging_conformance.json)
  records the live stock-binary result for `2.1.272`: idle submission, exact UUID
  mapping, duplicate suppression, own-child delivery, and socket rebinding pass, but
  authentication and credential rotation do not enforce the boundaries Theater
  requires. Permission-refusal and rate-limit outcomes also remain unclassified.
- The result is a hard no-go for a Claude native runtime in this release. Keep the
  manifest unchanged; the later phases below remain design notes for a future stock
  release whose complete proof passes.

## Upstream facts and constraints

The official messaging surface has these documented properties:

- minimum version `2.1.224` on macOS/Linux and `2.1.234` on Windows;
- same-machine operation across providers, including when feature fetching is off,
  requires `2.1.248` or later;
- it is automatically enabled;
- every Claude session owns a Unix socket or Windows named pipe;
- `CLAUDE_CODE_MESSAGING_SOCKET` and `CLAUDE_CODE_MESSAGING_TOKEN` are available to
  `SessionStart` hooks;
- the first client frame authenticates with
  `{"type":"auth","token":"<token>"}`;
- a message received while idle starts a turn;
- busy delivery is release-dependent: `2.1.272` can leave the message buffered in
  the editor without submitting it.

The last behavior is not Theater's ordinary-send contract. Phase two must expose only
idle send. Socket existence is not proof that the session is idle.

Two independent clients demonstrate this candidate user frame:

- [chelamux `messenger.py`](https://github.com/Devail1/chelamux/blob/main/chela/messenger.py)
- [cctop `cc-send`](https://github.com/DeanLa/cctop/blob/main/plugin/scripts/cc-send)

```json
{
  "type": "user",
  "message": {"role": "user", "content": "..."},
  "from": "uds:<reply-socket>",
  "msg_id": "<uuid>",
  "priority": "next"
}
```

This is test input, not a Theater contract. Production work starts only after the
exact pinned stock binary accepts it and the resulting JSONL proves what `msg_id`
means.

## Capability mapping

| Theater capability | Claude surface | Phase-two route |
| --- | --- | --- |
| Native session identity | Theater `--session-id`, hook payload, transcript | Retain and cross-check all three |
| Idle send | Authenticated messaging socket plus user frame | Native only after conformance passes |
| Follow-up queue | Theater queue dispatches only after authoritative idle | Native through idle send if proven |
| Busy ordinary send | Consumed or editor-buffered depending on release | Do not expose; reject/hold in Theater |
| Steer | No documented exact-active-turn mutation | Unavailable |
| Interrupt | No documented socket operation with expected turn ID | Guarded Escape/Ctrl+C legacy route |
| Settings update | No safe current-session mutation in this surface | Unavailable |
| Observation | Receipt hooks and JSONL transcript | Retain; use as authoritative correlation evidence |
| Terminal result | Existing transcript projection | Retain; bind only by the proven native turn ID |

## Phase 0 — replace the stale proof

Rewrite
[`tests/test_claude_native_control_proof.py`](../../tests/test_claude_native_control_proof.py)
as an opt-in executable conformance test against stock Claude Code `>=2.1.248`.
Update the fixture with exact binary version, platform, probe date, frame bytes with
tokens redacted, response frames, and relevant JSONL records.

The proof must run an isolated disposable session and establish all of the following:

1. `SessionStart` receives a usable socket path and token before any control request.
2. The auth frame is accepted, rejected credentials fail, and no non-auth frame is
   accepted first.
3. The candidate user frame creates exactly one visible user input while idle.
4. A duplicate `msg_id` either has documented idempotency or demonstrably creates a
   second input. Record the result; Theater must not assume upstream deduplication.
5. The socket response or transcript gives a stable admission fact.
6. The sent `msg_id` maps exactly to the transcript's `promptId`, user-record UUID, or
   another durable field. Text/time/order correlation fails the proof.
7. Permission refusal, rate limit, malformed frame, disconnect before reply, and
   immediate process exit have classified outcomes.
8. A busy-session message is classified as consumed during the turn, submitted after
   it, or editor-buffered without submission, and is not exposed as Theater send.
9. Session resume/rotation replaces or preserves socket/token as documented; stale
   credentials cannot mutate the new session.

The fixture should make the conclusion machine-readable, for example:

```json
{
  "version": "2.1.248",
  "idleSend": {
    "frameProven": true,
    "admissionReply": "<bounded shape>",
    "turnIdField": "promptId",
    "duplicateSemantics": "not-idempotent"
  },
  "busySend": "not-adopted",
  "result": "pass: idle send only"
}
```

If the exact turn mapping or authoritative admission fact is absent, stop here. Keep
the manifest unchanged and record a current no-go result.

## Phase 1 — install the launch-local bridge

Use `RuntimeHost.FRONTEND` and Theater's existing authenticated frontend endpoint.
The bridge is a small launch-local helper started by `SessionStart`; it receives the
Claude socket/token from the hook environment and connects outward to Theater.

Required lifecycle:

```text
Claude SessionStart hook
  -> helper reads CLAUDE_CODE_MESSAGING_SOCKET/TOKEN
  -> helper authenticates to the Claude session socket
  -> helper authenticates to Theater's participant frontend endpoint
  -> helper publishes session ID + authoritative idle/busy state
  -> daemon issues once-only idle-send requests
```

The `SessionStart` hook must not block Claude for the lifetime of the bridge. Use
Claude's documented asynchronous-hook mode if the stock proof confirms that it keeps
the messaging environment and lifecycle needed here; do not invent a self-daemonizing
shell wrapper. The command validates the hook payload, preserves the existing
transcript receipt, and starts one helper for the exact participant/session. Fence
duplicate `SessionStart` events with participant/session identity and a private
PID/lock artifact so two helpers cannot control one socket. Socket close or session
replacement makes the old helper exit.

Do not put the Claude token in argv, generated JSON, SQLite, PID files, logs,
exception strings, or `repr`. Read and remove it from the helper environment as soon
as practical, then retain it only in memory. The Theater participant token may stay
in its existing private token file. On daemon restart, the helper reconnects to the
same frontend endpoint and advances its bridge epoch; it must not ask Claude to repeat
a possibly applied message.

Claude's launch already writes `claude.settings.json`. The generic
[`RuntimeFrontendOverlay`](../../theater/harness/contracts/runtime.py#L726) currently
supports additive files only, so blindly adding the same path would overwrite the
receipt hooks. Prefer one of these narrowly scoped solutions, in order:

1. add a validated `transform_files`/replacement field whose keys must already exist
   in the launch plan and whose installer returns the complete replacement content;
2. compose Claude's receipt and bridge hooks in a Claude-local installer before the
   generic overlay is applied.

Do not add a generic JSON merge engine. Whatever seam is chosen must reject an
unknown path, preserve existing hooks, keep private-file permissions, and have one
focused contract test.

Suggested plugin files:

- add `theater/harness/builtin/plugins/claude/frontend.py` to render the helper and
  merge its `SessionStart` hook;
- add `theater/harness/builtin/plugins/claude/claude_frontend_bridge.py` as the bounded
  socket relay, unless the executable proof shows a simpler supported invocation;
- add `theater/harness/builtin/plugins/claude/runtime.py` to decode bridge snapshots,
  issue send requests, and expose only proven capabilities;
- add `theater/harness/builtin/plugins/claude/compatibility.py` for the exact tested
  release policy;
- update `claude/manifest.py` only after the proof passes.

Keep generated helper comments/docstrings within the repository's four-line rule.

## Phase 2 — authoritative idle state

Native send cannot be safely scheduled unless the bridge reports authoritative
session state. A connected socket is not enough. The implementation must prove an
official signal that distinguishes idle from active and remains ordered with message
admission.

The snapshot must include:

```json
{
  "native_session_id": "<claude session id>",
  "bridge_epoch": 4,
  "execution_state": "idle",
  "native_turn_id": null,
  "capabilities": {"send": true}
}
```

If the messaging protocol itself emits lifecycle state, use its sequence/watermark.
If only hooks provide state, the proof must show that hook ordering cannot leave a
stale `idle` window while a human turn has already started. Do not derive state from
pane text, prompt glyphs, process CPU, or elapsed time. If no authoritative state
exists, native send remains disabled even when the user frame is understood.

## Phase 3 — once-only idle send

Add one bridge request with these required fields:

```json
{
  "method": "claude.control.send",
  "params": {
    "operation_id": "<theater operation>",
    "native_session_id": "<expected session>",
    "prompt": "<bounded prompt>"
  }
}
```

Under one serialized bridge critical section:

1. validate method, frame bounds, operation ID, session, bridge epoch, and prompt;
2. return a cached terminal reply for an exact duplicate;
3. require authoritative idle immediately before the write;
4. reserve the operation before writing to Claude;
5. send one proven user frame with a newly recorded `msg_id`;
6. await the proven admission response and exact JSONL turn correlation;
7. return `ACCEPTED` with that `native_turn_id` only after both agree.

Pre-write validation failures are `REJECTED`. Any timeout, socket close, session
rotation, or malformed response after the user frame may have been written is
`UNKNOWN`. Cache `UNKNOWN` for duplicate operation IDs. Never replay it and never let
the daemon fall back to tmux.

The runtime should advertise `SEND` and `QUEUE_FOLLOWUP` only while the bridge is
healthy and the exact session/idle proof is current. It should advertise `STEER` and
`SETTINGS_UPDATE` as unavailable and retain `INTERRUPT` in `legacy_fallback`.

## Tests

Add the minimal focused layers:

- stock-binary conformance in `tests/test_claude_native_control_proof.py`;
- rendered bridge protocol tests in `tests/test_claude_native_bridge.py` covering
  auth, bounds, duplicate IDs, stale epoch/session, idle admission, and unknown writes;
- frontend lifecycle integration in `tests/test_claude_frontend_integration.py` for
  launch overlay, hook coexistence, reconnect, and token secrecy;
- control-service cases for accepted send/job correlation, queue release, busy
  rejection, `UNKNOWN` no-fallback, and terminal transcript evidence;
- one real stock-session smoke test proving the prompt appears and is actually
  submitted, not merely left in the input editor.

Suggested focused command after implementation:

```sh
uv run pytest tests/test_claude_native_control_proof.py \
  tests/test_claude_native_bridge.py \
  tests/test_claude_frontend_integration.py \
  tests/test_control_service.py
```

## Release and stop gates

Enable Claude's runtime only for stock releases that pass the executable fixture. The
first allowlist floor is `>=2.1.248`, but version comparison alone is not proof;
prefer exact qualified versions until two or more consecutive releases demonstrate a
stable frame and identity contract.

Ship idle native send only when:

- the complete frame and authentication exchange are captured from the stock binary;
- an authoritative idle state closes the human-input race;
- one `operation_id` maps one-to-one to a durable Claude turn ID;
- failures after write become cached `UNKNOWN` receipts;
- daemon and frontend restart do not duplicate input;
- the stock TUI remains the user's real session.

Stop and retain legacy send if any result depends on prompt text, time proximity,
event order, socket availability as idle, private binary patching, or credentials on
disk. Retain legacy interrupt regardless: cross-session messaging documents delivery,
not an exact expected-turn cancellation primitive.
