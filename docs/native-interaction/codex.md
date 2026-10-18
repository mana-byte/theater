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
- [`codex/runtime_plan.py`](../../theater/harness/builtin/plugins/codex/runtime_plan.py#L24)
  admits the qualified release range: a floor, an exclusive ceiling and an excluded tuple
  (`CODEX_RUNTIME_QUALIFIED_*`); the probe at line 123 refuses everything else, and the
  planner starts the app-server at line 241.
- [`codex/runtime.py`](../../theater/harness/builtin/plugins/codex/runtime.py#L385)
  implements send with `clientUserMessageId`, steer at line 424, interrupt at line
  466, and settings update at line 494.
- [`tests/harness/codex/test_native_runtime_proof.py`](../../tests/harness/codex/test_native_runtime_proof.py)
  contains offline fixture assertions plus opt-in stock-binary tests.
- [`tests/fixtures/codex_native_runtime/`](../../tests/fixtures/codex_native_runtime)
  retains two bundles of generated schemas and sanitized behavior, `floor` and `latest`,
  selected by role from `index.json`; both record `"provider": "mock"`. Earlier per-release
  bundles live in git history only.

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
  uv run pytest tests/harness/codex/test_native_runtime_proof.py -k native_smoke -v
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
6. never edit the release gate.

The command must refuse a dirty output directory unless passed a newly created
release directory. It must not overwrite prior evidence in place. Credential values,
prompts, absolute home paths, and transcript content are redacted before writing.

## Phase 1 — versioned evidence layout

Keep the evidence as bundles, so adding a release does not erase the comparison baseline.
Only two stay committed, at fixed names, so a refresh diffs as just the lines that changed:

```text
tests/fixtures/codex_native_runtime/
  floor/ and latest/        # one bundle each, shaped as below
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

`index.json` maps the two role names (`floor`, `latest`) to their fixture directory and
qualification status. Tests choose a bundle by role from the index; they must not pick one
by comparing versions lexically or semantically.

Migration requirements:

- keep the current offline assertions passing against both retained bundles, and check
  that each captured release lies inside the gate;
- validate that the index names exactly the two retained bundles and both are complete;
- a fresh capture waits untracked in `candidates/<release>/` until a bump composes it,
  and is never interpreted as supported.

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
[`test_native_runtime_proof.py`](../../tests/harness/codex/test_native_runtime_proof.py#L70).

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

1. capture its bundle; refresh `latest/` only when the captured evidence differs from the
   retained one (everything except `installed_release.json`, which names the binary);
2. move `CODEX_RUNTIME_QUALIFIED_CEILING` past it, a one-line edit (`codex_bump.py`);
3. update the compatibility-policy name only if policy semantics changed;
4. run the entire native runtime and spawn/recovery suite;
5. commit the ceiling change last, after evidence and tests.

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
then, no branch is clearer. Never infer a dialect from major/minor semver; the range
is only what the pipeline has run, release by release.

Capability loss may be per capability if the rest of the release remains proven. For
example, removal of `expectedTurnId` disables native steer while native send remains
eligible. The manifest/report must state the actual route; do not silently emulate the
missing method with keys after an uncertain native attempt.

## Mock-provider qualification (offline, zero quota)

`tests/native/codex_mock_provider.py` is a scripted OpenAI Responses provider on
loopback that drives every Phase-3 behaviour case — handshake, thread lifecycle,
steer, stale steer, concurrent-submission race, interrupt, approvals, settings
gating, queue gating, UI topology — against the unmodified stock binary with no
network and no model quota. It exists because the ambient provider is not always
reachable and real turns are slow and metered; Codex ships weekly, so
qualification must be cheap and repeatable.

What it proves: the binary's app-server wire behaviour — every response shape,
error code and message, turn identity, subscription/broadcast split, approval
ownership, TUI attach topology — under scripted model streams. Held streams
(deltas, then a bounded keepalive pause) keep a turn provably active so
`turn/steer`, `turn/start`-during-active and `turn/interrupt` are deterministic.
Dispatch matches the request's *last user message*, because `turn/steer` does
not abort the in-flight request: the steer text is queued and a new request
(whose last user message is the steer text) is issued only after the current
stream completes — a whole-body matcher would mis-dispatch on accumulated
history.

What it cannot prove: anything the real model does that the script does not
reproduce — refusal behaviour, multi-tool chains, token/latency realism. A
mock-captured bundle records `"provider": "mock"` in `installed_release.json`
(the collector does this), so mock evidence stays distinguishable from
real-provider evidence. Theater's native Codex runtime consumes the app-server wire
protocol and the stock TUI's `--remote` attach, and the mock leaves both binaries
unmodified (only the model behind them is scripted), so a release that passes
the full mock suite is compatible and belongs inside the gate: the gate exists
to prevent breakage, not to withhold releases that are known to work. Known mock-environment deltas, each explainable and none of them a
behaviour gap: the release tarball's binary has no model-metadata table, so the
app-server emits one advisory `warning` notification a metadata-installed
binary does not; mock turns carry synthetic assistant text.

Exact commands to qualify a candidate release X.Y.Z (ceiling bump always
last, never persisted):

```sh
SCRATCH=$(mktemp -d /tmp/codex-qualify.XXXXXX)
uv run python tests/native/codex_fetch_release.py --version X.Y.Z --dest "$SCRATCH/bin"
uv run python tests/native/codex_qualify_runtime.py \
  --out "$SCRATCH/bundle-X.Y.Z-mock" --capture-behavior --provider mock \
  --baseline tests/fixtures/codex_native_runtime/latest \
  --codex "$SCRATCH/bin/codex"
THEATER_CODEX_NATIVE_PROOF=1 THEATER_CODEX_NATIVE_PROVIDER=mock \
  THEATER_CODEX_NATIVE_BIN="$SCRATCH/bin/codex" \
  THEATER_CODEX_QUALIFY_VERSION=X.Y.Z \
  uv run pytest tests/harness/codex/test_native_runtime_proof.py -k native_smoke -v
THEATER_CODEX_NATIVE_SMOKE=1 THEATER_CODEX_NATIVE_BIN="$SCRATCH/bin/codex" \
  THEATER_CODEX_QUALIFY_VERSION=X.Y.Z \
  uv run pytest tests/harness/codex/test_native_ui_bootstrap_proof.py -k native_ui_bootstrap
rm -rf "$SCRATCH"
```

The env switches are qualification-run only and never persisted:
`THEATER_CODEX_NATIVE_BIN` targets the fetched binary; `THEATER_CODEX_QUALIFY_VERSION`
admits exactly that one version for the test process (the gate itself is
untouched, so a candidate is exercised before any ceiling bump);
`THEATER_CODEX_NATIVE_PROVIDER=mock` wires the smoke suite's isolated CODEX_HOME
at the mock provider instead of copying the ambient config. Real-provider runs
remain the default everywhere (`--provider ambient`, env unset). Review the
printed diff against the baseline: additive schema changes are benign; a removed
or reshaped required method, notification, or error text is a real dialect break.
Then refresh `latest/` if the capture differs, and move `CODEX_RUNTIME_QUALIFIED_CEILING`
last (the daily pipeline does both, see `docs/harness-compat.md`). A release that passes
everything except one capability (for example settings readback) is a dialect decision,
not a silent pass.

## Release dialects

Differences the qualified releases exercise, each handled without a version switch:

- 0.148.0–0.152.1: `thread/read`, `thread/resume` and `thread/fork` never carry
  `model` or `reasoningEffort`, so a settings update is confirmed from the
  `thread/settings/updated` broadcast (delivered only to subscribed connections); a
  broadcast naming another value confirms nothing. 0.152.x also answers
  `includeTurns: true` with `-32601 list_turns is not supported yet`.
- 0.153.0 and later: the readback carries both fields and confirms directly.
- 0.156.0 removes `thread/rollback` (Theater never calls it). 0.159.0 documents
  `Turn.error` for interrupted turns too, so only a failed turn carries `turn_failed`.

## Startup dialogs

A promptless `codex --remote` TUI creates no thread until it clears its startup dialogs, and
nobody answers them for an agent-spawned child. Codex skips its update prompt only when a CLI
prompt is given, so the native UI plan passes `-c check_for_update_on_startup=false`.
`test_codex_native_ui_starts_a_thread_unattended` (opt-in, stock binary) launches that exact
plan with an update available, plus a control without the override that must block. The
workspace-trust dialog is not suppressed: a worktree inside a trusted repo did not show it.

## Files to change

- `tests/native/codex_native_client.py`: reusable collection helpers only; preserve
  hard frame/time bounds.
- add `tests/native/qualify_codex_runtime.py`: explicit evidence-capture command with
  no gate write.
- `tests/harness/codex/test_native_runtime_proof.py`: parameterize offline bundle checks over the
  retained bundles and keep opt-in stock scenarios.
- `tests/fixtures/codex_native_runtime/`: the retained `floor` and `latest` evidence.
- `theater/harness/builtin/plugins/codex/runtime_plan.py`: ceiling bump last.
- `theater/harness/builtin/plugins/codex/runtime.py`: only for a proven dialect delta;
  preserve current send, steer, interrupt, settings, and reconciliation semantics.
- CI documentation/workflow: run offline bundles normally; expose a manual stock
  qualification job only if credentials and quota are deliberately provided.

Focused verification for a candidate:

```sh
uv run pytest tests/harness/codex/test_native_runtime_proof.py \
  tests/harness/codex/test_native_runtime_plugin.py \
  tests/harness/codex/test_native_ui_bootstrap_proof.py \
  tests/daemon/harness_runtime/test_lifecycle_integration.py \
  tests/daemon/harness_runtime/test_live_recovery.py
```

Then run the opt-in stock proof with the exact binary whose digest is in the fixture.

## Acceptance and stop criteria

A release falls inside the gate (`CODEX_RUNTIME_QUALIFIED_CEILING` moves past it) only when:

- its generated schema and stock behavior bundle were captured and compared with `latest`;
- every required mutation preserves exact session/turn identity;
- concurrent UI/control, restart, overflow, and stale-turn tests pass;
- approval ownership remains in the stock UI;
- all `UNKNOWN` paths remain terminal and unreplayed;
- the offline proof shows both retained bundles lie inside the gate.

The gate is a range, but it advances only over an unbroken run of proven releases: the
daily pipeline moves the ceiling past a release only after every release between the old
ceiling and it passed (`scripts/harness_compat/select.py`), and a release that cannot be
admitted is listed in `CODEX_RUNTIME_QUALIFIED_EXCLUDED` with its measured cause. Evidence
outranks semver: the range records releases that were actually run, never an inference from
version numbers, and a release above the ceiling, a prerelease or an unparsable version stays
fail-closed on legacy. A failed new release is not urgent production work: `auto`
must select legacy and explicit native must explain why it was refused.
