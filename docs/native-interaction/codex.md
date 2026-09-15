# Codex native wiring qualification record

## Outcome sought

Keep the existing Codex app-server adapter as Theater's reference native backend and
turn release qualification into a repeatable, reviewable workflow. Do not rewrite the
transport and do not broaden compatibility from semver assumptions.

Codex already provides the target topology: Theater and the stock UI attach to one
detached app-server over a private Unix WebSocket, with exact thread/turn controls and
native events.

## Current baseline

- [`codex/manifest.py`](../../theater/harness/builtin/plugins/codex/manifest.py#L115)
  declares the detached runtime and native live channel.
- [`codex/runtime_plan.py`](../../theater/harness/builtin/plugins/codex/runtime_plan.py#L28)
  allows exactly `codex-cli 0.154.0`; the planner starts the app-server at line 211.
- [`codex/runtime.py`](../../theater/harness/builtin/plugins/codex/runtime.py#L385)
  implements send with `clientUserMessageId`, steer at line 424, interrupt at line
  466, and settings update at line 494.
- [`tests/test_codex_native_runtime_proof.py`](../../tests/test_codex_native_runtime_proof.py)
  contains offline fixture assertions plus opt-in stock-binary tests.
- [`tests/fixtures/codex_native_runtime/`](../../tests/fixtures/codex_native_runtime)
  contains the generated schemas and sanitized behavior captured for `0.154.0`.

No phase-two production change is justified until a newly qualified release exhibits
a real dialect difference or fixes a known limitation.

## Capability mapping

| Theater capability | Codex app-server surface | Phase-two action |
| --- | --- | --- |
| Start/resume/fork | `thread/start`, `thread/resume`, `thread/fork` | Requalify unchanged |
| Idle send | `turn/start` plus `clientUserMessageId` | Requalify correlation and UI race |
| Follow-up queue | Theater queue dispatches through `turn/start` | Requalify once-only dispatch |
| Steer | `turn/steer` with `expectedTurnId` | Requalify stale-turn refusal |
| Interrupt | `turn/interrupt` with thread and turn IDs | Requalify response/terminal ordering |
| Settings | `thread/settings/update` and `thread/settings/updated` | Requalify update/readback and supported fields |
| Observation | Thread/turn/item notifications plus readback | Requalify overflow/reconnect reconciliation |
| Stock UI | `codex --remote ... resume <thread>` | Requalify same-thread attachment and approval ownership |

## Phase 0 — make qualification reproducible

Preserve the existing opt-in test entry point:

```sh
THEATER_CODEX_NATIVE_PROOF=1 \
  uv run pytest tests/test_codex_native_runtime_proof.py -k native_smoke -v
```

Add a small maintainer command, preferably a Python module under `tests/native/`, that
accepts an explicit output directory and performs only evidence collection:

1. resolve the exact `codex` binary and record its real path, file digest, `--version`
   output, OS, and architecture;
2. run the vendor generator exactly as documented:

   ```sh
   codex app-server generate-json-schema --out <dir> --experimental
   ```

3. normalize generated JSON by parsing and writing sorted/indented keys; do not
   normalize arrays or discard descriptions/enums;
4. run the stock topology/behavior probe and emit sanitized fixture JSON;
5. print a deterministic recursive diff against the last qualified release;
6. never edit the verified-version allowlist.

The command must refuse a dirty output directory unless passed a newly created
release directory. It must not overwrite prior evidence in place. Credential values,
prompts, absolute home paths, and transcript content are redacted before writing.

## Phase 1 — versioned evidence layout

Move the flat fixture into a per-release bundle so adding a release does not erase the
comparison baseline:

```text
tests/fixtures/codex_native_runtime/
  0.154.0/
    installed_release.json
    protocol_schema/
    handshake.json
    thread_lifecycle.json
    turn_control.json
    approval.json
    capabilities.json
    unsupported_capabilities.json
    ui_topology.json
  index.json
```

`index.json` should map exact versions to their fixture directory, qualification
status, and compatibility dialect. Tests choose the bundle from an explicit expected
version; they must not select “latest” by lexical or semantic comparison.

Migration requirements:

- move the existing files byte-for-byte except deterministic formatting;
- keep the current offline assertions passing against `0.154.0`;
- validate that every allowed version has a complete bundle and every passing bundle
  appears in the allowlist;
- a captured-but-failing candidate may remain in a clearly named `candidates/`
  directory, but must never be interpreted as supported.

## Phase 2 — schema diff gate

Classify every generated-schema difference before running behavior tests:

| Change | Default decision |
| --- | --- |
| Required method removed/renamed | Fail |
| Required request/response field removed or type changed | Fail |
| Expected-turn field removed | Fail steer/interrupt capability |
| New required client/server request | Fail until runtime handles it safely |
| New optional field or notification | Review, then behavior-test |
| Enum expanded | Review every exhaustive decoder; unknown remains fail-closed |
| Description/order-only change | Record, no production change |

At minimum compare `ClientRequest.json`, `ClientNotification.json`,
`ServerRequest.json`, `ServerNotification.json`, `JSONRPCRequest.json`, and
`JSONRPCMessage.json`. Keep the assertions around required methods currently declared
at
[`test_codex_native_runtime_proof.py`](../../tests/test_codex_native_runtime_proof.py#L70).

Do not auto-generate runtime code from the schema during this phase. The existing
decoders deliberately enforce stronger Theater invariants than structural schema
validity.

## Phase 3 — stock-binary behavior gate

For each candidate release, run these cases against the unmodified binary and save
the bounded observations:

### Topology and lifecycle

- app-server listens on the private Unix WebSocket and rejects invalid handshake;
- Theater observer and stock `--remote` UI see the same thread;
- UI readiness is event-based, not a sleep;
- new, resume, fork, and second-client subscription preserve documented IDs;
- abrupt control-client or UI exit does not kill/corrupt the backend;
- daemon restart adopts the same verified PID/start identity and thread;
- approval requests remain owned by the stock UI; Theater never answers them.

### Send and queue

- `clientUserMessageId=operation_id` appears on exactly the admitted user message;
- simultaneous UI and Theater submission returns the actual affected turn and never
  attributes the human message to Theater;
- a busy `turn/start` response is classified according to observed server semantics;
- timeout/disconnect after write is `UNKNOWN` and is not replayed;
- a queued Theater follow-up dispatches once after exact idle;
- a turn that completes before the response still maps admission and terminal event
  to the same job.

### Steer and interrupt

- `turn/steer.expectedTurnId` accepts the active turn and rejects a stale one;
- steer cannot become a new ordinary turn after the expected turn settles;
- interrupt response and `turn/completed(status=interrupted)` may arrive in either
  order without losing terminal evidence;
- stale interrupt cannot affect the replacement turn;
- duplicate Theater operation IDs never cause a second mutation even if Codex itself
  does not deduplicate them.

### Settings and event recovery

- model and effort updates return effective values and emit
  `thread/settings/updated` for the same thread;
- unsupported/invalid fields fail before mutation;
- external UI settings changes refresh Theater's snapshot;
- reconnect/readback recovers current thread, active turn, settings, and exact
  completed outcomes;
- notification-buffer overflow fails visibly and durable reconciliation cannot map a
  historical outcome to a current replacement turn.

Keep stock tests opt-in because they can consume model quota. Offline fixtures remain
the ordinary CI gate, but a skipped stock test never qualifies a release.

## Phase 4 — runtime delta only when evidence requires it

If a candidate passes without protocol changes:

1. add its fixture bundle;
2. add its exact version to `CODEX_RUNTIME_VERIFIED_VERSIONS`;
3. update the compatibility-policy name only if policy semantics changed;
4. run the entire native runtime and spawn/recovery suite;
5. commit the allowlist change last, after evidence and tests.

If schemas or behavior differ, add the smallest explicit dialect branch. A possible
shape is:

```python
@dataclass(frozen=True, slots=True)
class CodexDialect:
    version: str
    methods: CodexMethods
    capabilities: frozenset[RuntimeCapability]
```

Introduce this only after two releases actually require different handling. Until
then, direct exact-version checks are clearer. Never infer a dialect from major/minor
semver or accept an untested range.

Capability loss may be per capability if the rest of the release remains proven. For
example, removal of `expectedTurnId` disables native steer while native send remains
eligible. The manifest/report must state the actual route; do not silently emulate the
missing method with keys after an uncertain native attempt.

## Files to change

- `tests/native/codex_native_client.py`: reusable collection helpers only; preserve
  hard frame/time bounds.
- add `tests/native/qualify_codex_runtime.py`: explicit evidence-capture command with
  no allowlist write.
- `tests/test_codex_native_runtime_proof.py`: parameterize offline bundle checks and
  keep opt-in stock scenarios.
- `tests/fixtures/codex_native_runtime/`: versioned immutable evidence.
- `theater/harness/builtin/plugins/codex/runtime_plan.py`: exact allowlist change last.
- `theater/harness/builtin/plugins/codex/runtime.py`: only for a proven dialect delta;
  preserve current send, steer, interrupt, settings, and reconciliation semantics.
- CI documentation/workflow: run offline bundles normally; expose a manual stock
  qualification job only if credentials and quota are deliberately provided.

Focused verification for a candidate:

```sh
uv run pytest tests/test_codex_native_runtime_proof.py \
  tests/test_codex_native_runtime_plugin.py \
  tests/test_codex_native_ui_bootstrap_proof.py \
  tests/test_runtime_lifecycle_integration.py \
  tests/test_runtime_live_recovery.py
```

Then run the opt-in stock proof with the exact binary whose digest is in the fixture.

## Acceptance and stop criteria

A version enters `CODEX_RUNTIME_VERIFIED_VERSIONS` only when:

- its generated schema and stock behavior bundle are committed;
- every required mutation preserves exact session/turn identity;
- concurrent UI/control, restart, overflow, and stale-turn tests pass;
- approval ownership remains in the stock UI;
- all `UNKNOWN` paths remain terminal and unreplayed;
- the compatibility test proves the bundle/allowlist match.

Keep exact-version fail-closed selection until multiple consecutive releases pass and
upstream publishes a compatibility guarantee strong enough to replace it. Even then,
evidence outranks semver. A failed new release is not urgent production work: `auto`
must select legacy and explicit native must explain why it was refused.
