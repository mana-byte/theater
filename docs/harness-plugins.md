# Harness plugins

A harness plugin teaches Theater how to launch and observe one coding-agent
CLI. It is a named package directory, not a loose Python file. Use public
`theater.harness.contracts` modules; do not import a shipped plugin's private
implementation. A plugin may optionally declare a typed native runtime through
`HarnessManifest.runtime`, which adds live control and observation without
changing Theater's NDJSON protocol — see
[Native runtime wiring](#native-runtime-wiring).

## Package layout and loading

Place a local plugin under `$THEATER_HOME/plugins/` (normally
`~/.theater/plugins/`):

```text
$THEATER_HOME/plugins/
└── acme/
    ├── manifest.py
    ├── launch.py
    ├── source.py
    └── screen.py
```

The directory name, here `acme`, is the canonical harness name. It must use
lowercase letters, digits, `_`, or `-`, starting with a letter or digit.
`manifest.py` exports one root value named `MANIFEST`; there is no separate
manifest name field. The value must be exactly one `HarnessManifest`; the same
catalog also accepts packages containing exactly one `McpServerManifest`.

The loader imports each directory as an isolated synthetic package. Relative
imports such as `from .source import source_factory` work, sibling modules in
different plugins cannot collide, and the loader never changes `sys.path`. A
failed import is cleaned from `sys.modules`.

Shipped packages are scanned first and local packages second: a local package
with the same canonical name deliberately overrides a shipped one. A broken
shipped plugin stops startup; a broken local plugin is skipped. The
`theater harnesses` command reports loaded and rejected plugins at the behavior
level; its output schema is not an authoring API.

`[harness].disabled` filters a harness package before import, so it can disable
a plugin that would otherwise fail during import. A top-level legacy file such
as `$THEATER_HOME/plugins/acme.py` is never executed. It receives this
actionable migration diagnostic:

```text
$THEATER_HOME/plugins/acme.py: legacy single-file plugin. Move acme.py to
acme/manifest.py and export MANIFEST for a harness package
```

(The real message is path-prefixed and names the package kind — "a harness
package" or "an MCP-server package".)

Move its helpers into `acme/`, change sibling imports to relative imports, and
replace `HARNESS` with `MANIFEST`. Restart the daemon after an install or edit.

## A minimal complete package

This package loads without a `Harness` subclass. It demonstrates the public
callback signatures and a tiny JSONL source. The illustrative CLI accepts
`--mcp-config`; adapt argv and transcript details to the real CLI rather than
copying that convention blindly.

### `acme/manifest.py`

```python
from theater.harness.contracts.channels import (
    ChannelCapability,
    ChannelDeclaration,
    ChannelKind,
    SignalKind,
    SignalOwnership,
)
from theater.harness.contracts.manifest import (
    MANIFEST_API_VERSION,
    HarnessManifest,
    LaunchManifest,
    ObservationManifest,
    ScreenManifest,
    SourceManifest,
)

from .launch import plan_launch
from .screen import classify_screen
from .source import source_factory

MANIFEST = HarnessManifest(
    api_version=MANIFEST_API_VERSION,
    binary="acme",
    icon="A",
    aliases=("acme-cli",),
    launch=LaunchManifest(
        planner=plan_launch,
        approvals=("manual", "edits", "yolo"),
        supports_model=False,
        supports_reasoning_effort=False,
        supports_resume=False,
    ),
    observation=ObservationManifest(
        primary=SourceManifest(
            factory=source_factory,
            channel=ChannelDeclaration(
                id="transcript",
                kind=ChannelKind.TRANSCRIPT,
                capabilities=(
                    ChannelCapability(SignalKind.IDENTITY, SignalOwnership.PRIMARY),
                    ChannelCapability(SignalKind.CONTENT, SignalOwnership.PRIMARY),
                    ChannelCapability(SignalKind.TURN, SignalOwnership.PRIMARY),
                ),
            ),
        ),
        screen=ScreenManifest(classifier=classify_screen),
    ),
)
```

`HarnessManifest` and all its sub-manifests are frozen. Use
`dataclasses.replace` to derive a test manifest rather than mutating one. The
compiler validates it before a participant can spawn.

### `acme/launch.py`

```python
import json

from theater.harness.contracts.callbacks import LaunchContext
from theater.harness.contracts.launch import LaunchPlan, theater_binary


def plan_launch(context: LaunchContext) -> LaunchPlan:
    mcp_config = {
        "mcpServers": {
            "theater": {
                "command": theater_binary(),
                "args": ["mcp", "--id", context.participant_id],
            }
        }
    }
    argv = ["acme", "--mcp-config", str(context.config_path)]
    if context.approval == "edits":
        argv.append("--allow-edits")
    elif context.approval == "yolo":
        argv.append("--accept-all")
    if context.prompt:
        argv.append(context.prompt)
    return LaunchPlan(
        argv=argv,
        files={context.config_path: json.dumps(mcp_config) + "\n"},
    )
```

`LaunchManifest.planner` receives a frozen `LaunchContext` with
`participant_id`, `prompt`, `config_path`, `approval`, and optional `model`,
`reasoning_effort`, and `resume`. Return a `LaunchPlan`; do not create files,
start processes, write SQLite, or call tmux in the planner. The daemon writes
the plan's files and launches its argv.

`LaunchPlan.argv` is the command vector. `env` is an environment overlay;
`files` is a path-to-text mapping written before launch; and `private_files`
is a path-to-secret-text mapping written mode 0600. `session_id` is an exact
native session id a planner has minted or otherwise knows before launch.
`transcript_domain` is a stable namespace used for transcript collision policy.

Launch files must stay in Theater-owned participant storage. External plugins
should use `context.config_path` or derive same-participant siblings with
`context.config_path.with_suffix(...)`; arbitrary project and user paths are
rejected before anything is written. Theater records those owned paths so GC
can retry their removal after the participant itself is no longer retained.

The daemon, not a plugin, populates `receipt_token`, writes a declared
`receipt_token_path`, and creates `channel_credentials`. A planner may declare
the token path for a proven launch-local transcript receipt, but never receives
the token bytes. Hook/OTel installers are separate typed channel callbacks;
they return launch-local files and environment, not side effects.

There is no approval default. `LaunchManifest.approvals` must be a non-empty
ordered subset of `manual`, `edits`, and `yolo`; a planner receives the choice
the caller made and translates it to native CLI behavior. Declare model,
reasoning-effort, and resume support truthfully—unsupported requested values
are refused before the planner runs.

Optional interactive controls belong in `HarnessManifest.controls`. An
`InterruptPlan` is immutable data: a short validated tmux-key sequence and an
optional bounded delay between keys. The daemon applies the same lineage,
status, pane-identity, and human-presence gates for every harness; plugins only
declare the native keys their own TUI uses.

The example puts the Theater MCP server in the harness's native configuration.
`theater_binary()` is the public helper for the executable path; the identity
must be the `theater mcp --id <participant-id>` argv, not an assumed inherited
environment variable.

### `acme/source.py`

```python
import asyncio
import json
from pathlib import Path

from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.source import Attachment, Batch, Source

_MAX_RECORDS_PER_READ = 128
_MAX_RECORD_BYTES = 64 * 1024
_MAX_READ_BYTES = 256 * 1024
_MAX_PENDING_RECORDS = 512


class AcmeSource(Source):
    def __init__(self, context: ParticipantObservationContext) -> None:
        self._session_id = context.session_id or context.participant_id
        root = Path(context.cwd or ".") / ".acme" / "sessions"
        self._path = root / f"{self._session_id}.jsonl"
        self._offset = 0
        self._pending: list[bytes] = []
        self._tail = b""
        self._staged: Attachment | None = None
        self._attached = False
        self._closed = False

    async def read(self) -> Batch:
        if self._closed:
            return Batch()
        try:
            size, skipped = await asyncio.to_thread(_attachment_point, self._path)
        except FileNotFoundError:
            return Batch(waiting=True)

        if not self._attached:
            self._staged = Attachment(
                location=str(self._path),
                session_id=self._session_id,
                skipped=skipped,
            )
            self._offset = size
            return Batch(attached=self._staged)

        try:
            data, self._offset = await asyncio.to_thread(_read_chunk, self._path, self._offset)
        except FileNotFoundError:
            return Batch(waiting=True)
        records, overflow = self._take_records(data)
        events, malformed = _events(records)
        malformed = malformed or overflow
        return Batch(
            events=events,
            progressed=bool(data or records),
            error_code="acme_malformed_or_overflow" if malformed else None,
            error="ignored malformed or excess Acme transcript records" if malformed else None,
        )

    def commit_attachment(self) -> None:
        if self._staged is not None:
            self._attached = True
            self._staged = None

    def discard_attachment(self) -> None:
        self._staged = None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True

    def _take_records(self, data: bytes) -> tuple[list[bytes], bool]:
        complete = (self._tail + data).split(b"\n")
        self._tail = complete.pop()
        overflow = False
        if len(self._tail) > _MAX_RECORD_BYTES:
            complete.append(self._tail)
            self._tail = b""
            overflow = True
        self._pending.extend(complete)
        if len(self._pending) > _MAX_PENDING_RECORDS:
            del self._pending[_MAX_PENDING_RECORDS:]
            overflow = True
        records = self._pending[:_MAX_RECORDS_PER_READ]
        del self._pending[:_MAX_RECORDS_PER_READ]
        return records, overflow


def source_factory(context: ParticipantObservationContext) -> Source:
    return AcmeSource(context)


def _attachment_point(path: Path) -> tuple[int, int]:
    size = 0
    records = 0
    with path.open("rb") as stream:
        while block := stream.read(_MAX_READ_BYTES):
            size += len(block)
            records += block.count(b"\n")
    return size, records


def _read_chunk(path: Path, offset: int) -> tuple[bytes, int]:
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(_MAX_READ_BYTES)
        return data, stream.tell()


def _events(records: list[bytes]) -> tuple[tuple[Event, ...], bool]:
    events: list[Event] = []
    malformed = False
    for raw in records:
        if len(raw) > _MAX_RECORD_BYTES:
            malformed = True
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            malformed = True
            continue
        text = record.get("text") if isinstance(record, dict) else None
        if not isinstance(text, str):
            malformed = True
            continue
        events.append(
            Event(
                kind=EventKind.ASSISTANT,
                text=text,
                raw_text=text,
                turn_end=record.get("done") is True,
            )
        )
    return tuple(events), malformed
```

The factory receives the full frozen `ParticipantObservationContext`, including
the Theater participant id, cwd, native session id, `after`, known location,
transcript domain, and provenance. It returns one participant-scoped `Source`;
do not put per-participant cursor or connection state on the manifest callback.

`read()` is asynchronous and must not block the daemon event loop. The example
puts its file read on a worker thread and bounds parsing per poll. A database or
network source needs equivalent bounds on query/page size, retries, payload
size, retained state, and parser work. Missing input is `Batch(waiting=True)`,
not an exception. Malformed native input is ignored or reported with bounded,
non-sensitive error text—never dump its raw payload into diagnostics.

`Batch.events` contains normalized `Event` reports. `progressed=True` means
input was consumed even when it yielded no events. `status`, when a durable
source knows it, is an optional `Status` report; the reducer remains the policy
owner. `trajectory` is additive rich `TrajectoryFact` data, not a control path.
Sources never change registry state, complete jobs, publish to the bus, or
operate tmux.

The first attachment is staged in `Batch(attached=Attachment(...))`. The daemon
checks ownership then calls `commit_attachment()` or `discard_attachment()`;
never move a live cursor to a heuristic candidate before that handshake. A
durable primary should also implement, where its storage permits:

- `history_page(before=..., snapshot=..., limit=..., include_full_text=...)` for bounded,
  cursor-based history reads that do not advance the polling cursor; `history(last_n=...)`
  remains a legacy internal projection;
- `refresh()` when a transcript location can rotate;
- `probe_identity_loss()` only as bounded loss evidence, not a new binding;
- `admit_exact_location(location=..., session_id=...)` for exact receipt-led
  attachment, if the source supports receipts;
- `revoke_attachment()` to withdraw a previously committed attachment;
- the checkpoint quartet `source_checkpoint()` / `pending_source_checkpoint()` /
  `acknowledge_source_checkpoint()` / `rollback_source_checkpoint()` when the
  store needs durable-cursor consistency;
- `health_snapshot()` to report bounded channel health.

`aclose()` releases handles/subscriptions and must be idempotent. The primary
source remains the authority for attachment, identity, completion inputs, and
history even when it also has optional enrichments.

### `acme/screen.py`

```python
from theater.harness.contracts.callbacks import ScreenContext
from theater.harness.contracts.observation import (
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
)


def classify_screen(context: ScreenContext) -> ScreenReading:
    if context.capture.rstrip().endswith("acme>"):
        return ScreenReading(ScreenKind.PROMPT, ScreenConfidence.LOW)
    return ScreenReading(ScreenKind.UNKNOWN, ScreenConfidence.LOW)
```

The screen classifier is a display and rescue hint, never permission to inject
input or a replacement for durable turn evidence. Be conservative: return
`UNKNOWN` when a capture could be working, an approval dialog, or a trust
dialog. A false prompt can cause unsafe control behavior; `AWAITING_INPUT` is
not a control decision.

## Typed callback surface

Every custom function is placed in a named manifest field and receives one
frozen context. There is no callback bag, signature inspection, global name
lookup, or harness-name branch in the manifest runtime. Pure decoders should
only normalize bounded values; a factory/planner may perform only the I/O its
return contract owns and never reach Theater's SQLite connection.

| Manifest field | Context → result | Use |
| --- | --- | --- |
| `launch.planner` | `LaunchContext → LaunchPlan` | Pure launch description. |
| `launch.resume_preflight` | `ResumePreflightContext → None` | Reject an unsafe resume before reservation. |
| `launch.resume_planner` | `ResumeContext → ResumeLaunchOverlay` | Safe native resume overlay. |
| `observation.primary.factory` | `ParticipantObservationContext → Source` | Participant-scoped durable input. |
| `observation.screen.classifier` | `ScreenContext → ScreenReading` | Conservative terminal classification. |
| `observation.identity.*` | typed context → `StreamPoint`/`TranscriptCandidate` | Exact identity and recovery. |
| `observation.lineage.native_children` | `NativeChildrenContext → Sequence[NativeChild]` | Native sub-agent display facts. |
| `models.discoverer` | `ModelDiscoveryContext → Sequence[str]` | Optional model-list suggestion. |
| `mcp.renderer` | `McpRenderContext → McpRenderOverlay` | Native MCP server rendering for the harness. |
| `runtime.probe` | `RuntimeProbeContext → RuntimeCompatibility` | Read-only installed-native compatibility probe. |
| `runtime.plan` | `RuntimePlanningContext → RuntimePlan` | Pure detached-backend plan plus private endpoint. |
| `runtime.factory` | `RuntimeContext → HarnessRuntime` | One live runtime instance per participant. |

The compiler checks callback presence and returned runtime values, while the
manifest validator checks its structural contract. Keep callback modules beside
the manifest and wire them visibly with relative imports.

## Identity, resume, and recovery

`IdentityManifest` fields are all optional. Omission means the capability is
unavailable; it does not authorize a heuristic substitute.

- `stream_floor: StreamFloorContext → StreamPoint | None` records the durable
  stream's pre-launch location for safe successor/resume comparison. Omit it
  when the store cannot prove such a point.
- `transcript_candidates: TranscriptCandidatesContext → Sequence[TranscriptCandidate]`
  lists bounded, operator-visible candidates. It does not attribute them.
- `receipt_validator: ReceiptValidationContext → TranscriptCandidate` validates
  the opaque authenticated `transcript.receipt` payload. It must prove an exact
  location and native session id, not infer either from cwd or time.
- `operator_candidate_admitter: OperatorCandidateContext → TranscriptCandidate`
  revalidates a user-selected candidate before Theater persists trust.

The generic receipt is an identity transport, not a rich hook/event channel.
Its validator owns native field names, path checks, and transcript/database
evidence. Reject invalid input with actionable `ValueError`; do not accept
timestamp, model, cwd, or prose proximity as identity correlation.

Resume is opt-in. `LaunchManifest.supports_resume` defaults to `False`;
`resume_preflight` and `resume_planner` must be callable only when it is true.
The preflight receives a trusted predecessor and may reject before reservation;
it is also used for a dead participant's resume projection, so it must be
synchronous, bounded, and side-effect-free. The planner receives a trusted
predecessor and all trusted matching session owners, then returns
`ResumeLaunchOverlay(env=..., transcript_domain=..., cwd=..., resume_reference=...)`.
Those four fields are the only things a plugin may influence around a resume —
`resume_reference` hands `plan_launch` an alternate native resume reference
(`None` preserves the requested native session id); core applies a non-None
`cwd` override before it creates the successor participant.
`resume_strategy` is `"continue"` or `"fork"` and defaults to `"continue"`;
`resume_takes_prompt` defaults to `True` and must be set to `False` when the
native resume command cannot carry a new prompt. A predecessor with a
transcript domain is fail-closed unless the plugin's resume callback validates
reuse.

## Capabilities, lineage, models, and screens

`ObservationManifest.trajectory_capabilities` is a frozen
`TrajectoryCapabilities` declaration. Its `supported`, `unsupported`, and
`observed` fields are `frozenset[TrajectoryFeature]`; declare only features
your durable parser/source can substantiate. A feature cannot be both supported
and unsupported. Omit the field for the empty, unknown default.

`LineageManifest.native_children` is optional. It receives a transcript path
and returns `Sequence[NativeChild]` with the native session id and optional
agent/path/tool-call metadata. It reports a native lineage edge; it does not
create or control a participant.

`ModelDiscoveryManifest.discoverer` is optional and returns model names the
harness can actually list. It is a suggestion for `theater models`, not a
spawn authorization policy; Theater's `[models]` allowlist remains the gate.
Do not advertise guessed model names.

`ObservationManifest` requires a `ScreenManifest` and classifier. Its reading
is conservative evidence: `PROMPT`, `WORKING`, `APPROVAL`, `TRUST`, and
`UNKNOWN` each carry `LOW` or `HIGH` confidence. A text scrape is normally low
confidence; unknown is the safe answer when it cannot distinguish a prompt from
a sensitive modal.

## Optional signal enrichment

`CompositeSource` combines the durable primary with ordered, bounded
enrichment sources. Enrichment contributes trajectory facts only; it cannot
replace durable completion, attachment, identity, or history. Its timeout,
failure, malformed output, and overflow are bounded channel health and cannot
break primary observation.

Each `ChannelDeclaration` lists `ChannelCapability` values with one explicit
`SignalOwnership`: `PRIMARY`, `ENRICHMENT`, or `FALLBACK`. Duplicate channel
ids, two non-fallback owners for the same signal, ambiguous fallbacks, invalid
bounds, and durable enrichment channels are rejected. A hook/OTel decoder
returns `ChannelFact` values whose fact `native_id` exactly equals the accepted
native correlation key. Never join by timestamp proximity, cwd, model name, or
rendered prose.

### Hooks

`theater/harness/channels/hooks/` provides generic authenticated ingress,
bounded queues/deduplication, off-loop callback execution, and a source
adapter. A `HookChannelManifest` names the channel, explicit `HookBinding`s,
and a launch-local installer. Each binding supplies a native event name,
normalized signals, delivery expectation, exact correlation extractor, and
decoder.

Installers return only launch-local files/environment. Do not rewrite global or
project hook configuration. Payloads are untrusted bounded JSON; diagnostics
must not contain raw payloads or credentials.

An optional `HookChannelManifest.probe` uses the read-only `RuntimeProbeContext` and
`RuntimeCompatibility` contract before installation. The daemon runs it outside the event loop;
the callback must bound its subprocesses. Failed probes and explicit legacy selection omit these
optional native channels. Existing channels without a probe keep their established behavior.
Installers receive immutable `public_files` and may return explicit `replacements` for existing
participant-owned public launch files, allowing launch-local settings to be composed after probing.
Private files cannot be replaced. An optional install is staged atomically; a failure preserves
the ordinary plan and mints no active channel credential.

### Native OTel

`theater/harness/channels/otel/` is a distinct inbound harness channel. It can
provide a bounded loopback-only OTLP receiver and requires authentication plus
participant, harness, channel, binding, delivery, and exact native-key
correlation before a typed decoder receives a record. An `OtelChannelManifest`
declares the protocol, bounds, header/resource correlation fields, bindings,
and a launch-local installer.

The installer receives the private token-file path but never the token bytes. It names one
dedicated exporter-header environment variable through `credential_header_env`; after the callback
returns, core injects `<auth_header>=<token>` into that variable because native OTLP exporters need
the credential value in their launch environment. Core rejects inherited or overlay collisions,
never writes the token into generated public files, and removes the private file with participant
cleanup.

Never repoint or replace an existing user exporter. If additive launch-local
configuration, exact correlation, and a live fixture are not proven, use
`unavailable_reason`. This inbound channel is separate from
`theater/observability/`, which exports Theater's own daemon, CLI, and régie
logs, metrics, and spans.

### Current shipped state

Claude, Codex, OpenCode, Pi, and Vibe explicitly declare richer hook and native
OTel channels, but all are currently unavailable. Their durable transcript or
database sources remain authoritative until safety and evidence gates pass.
Do not advertise an unimplemented native integration. Codex additionally ships
a typed runtime manifest (a detached `codex app-server` backend with the stock
native CLI UI attached to the same thread); its tested compatibility
boundaries and rollout state are described under
[Native runtime wiring](#native-runtime-wiring).

### Diagnostics

`theater harnesses` derives static capabilities from each validated manifest. It shows the
package and manifest paths, primary source, enrichment bindings, normalized signals, ownership,
and explicit unavailability reasons. With a daemon running, it also shows participant-scoped
channel state, accepted and dropped counts, last successful activity, and the newest bounded
diagnostic. `--json` exposes the same bounded data for tooling.

Runtime diagnostics contain no credentials, native payloads, prompts, or results. A malformed
plugin health snapshot is ignored, and an enrichment-health failure cannot interrupt the durable
source. Without a daemon, the command reports static manifest capabilities only.

## Native runtime wiring

A plugin may additionally expose one typed runtime through
`HarnessManifest.runtime: RuntimeManifest | None = None`. The field is
additive: `None` preserves existing behavior exactly, no mandatory abstract
method was added to existing harnesses or sources, existing event constructors
remain valid, and a manifest without the field is legacy wiring by
construction — including a local override of a shipped harness that omits it.
All runtime contracts live in `theater.harness.contracts.runtime`.

The split of ownership is frozen:

- **Plugin runtime** — native protocol, capability detection, configuration
  mapping, session identity, event normalization. Plugin code must not import
  `theater.daemon`; it reaches a native backend only through the injected
  `RuntimeIO` / `RuntimeConnection` seams. The wire protocol spoken to the
  native backend belongs to the runtime implementation; Theater's own daemon
  protocol stays NDJSON version 1.
- **Daemon runtime manager** — backend lifecycle, persisted bindings, and
  exactly one `HarnessRuntime` instance per participant, created once and
  shared between observation and controls. Looking up a runtime returns the
  existing instance or nothing: a short-lived history read can never launch a
  backend or open a control connection. A runtime instance is bound to one
  backend generation; a stale generation cannot replace, disconnect, or signal
  the current one.
- **Daemon control service** — authorization, idle checks, durable operation
  reservation, job correlation, the followup queue, and delivery recovery.
  It is harness-neutral: physical facts arrive through injected gates and
  native delivery through the one `HarnessRuntime` per participant.
- **CLI / MCP / régie** — thin client calls and presentation; every policy
  decision belongs to the daemon.

### A minimal runtime plugin

The manifest half is pure declaration — a read-only compatibility probe, a
runtime factory, and a first-class live-channel declaration. A detached host
also declares a pure backend planner; a frontend host declares a passive
installer that overlays the ordinary launch plan.

```python
# acme/runtime.py
import re

from theater.harness.contracts.channels import (
    ChannelCapability,
    ChannelDeclaration,
    ChannelKind,
    SignalKind,
    SignalOwnership,
)
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    ControlReceipt,
    DeliveryResult,
    HarnessRuntime,
    LiveChannelDeclaration,
    RuntimeBinding,
    RuntimeCompatibility,
    RuntimeContext,
    RuntimeManifest,
    RuntimePlan,
    RuntimePlanningContext,
    RuntimeProbeContext,
    RuntimeRequestError,
    RuntimeSnapshot,
    RuntimeWiring,
    SessionOpenMode,
)
from theater.harness.contracts.source import Source

_VERIFIED_VERSIONS = frozenset({"1.2.3"})
_VERSION = re.compile(r"acme-cli[ \t]+(\S+)")


def probe(context: RuntimeProbeContext) -> RuntimeCompatibility:
    """Read-only: verify the installed binary against the tested policy.

    Theater-verified compatibility, not presumed vendor stability — the
    policy names exactly the releases the plugin's proof exercised.
    """
    version = _probe_installed_version(context.binary or "acme")
    if version not in _VERIFIED_VERSIONS:
        return RuntimeCompatibility(
            supported=False,
            policy="acme-appserver-1.2-verified",
            native_version=version,
            reason=(
                f"acme-cli {version} is not Theater-verified by policy "
                "acme-appserver-1.2-verified (verified: 1.2.3); the native "
                "preference keeps the ordinary launch"
            ),
        )
    return RuntimeCompatibility(
        supported=True,
        policy="acme-appserver-1.2-verified",
        native_version=version,
    )


def plan_backend(context: RuntimePlanningContext) -> RuntimePlan:
    """Pure: describe the detached backend; write nothing, start nothing."""
    return RuntimePlan(
        backend=LaunchPlan(argv=["acme", "app-server", "--listen", context.endpoint]),
        endpoint=context.endpoint,
    )
```

The runtime half implements the seven frozen operations — `open_session(...)`,
`frontend_plan(...)`, `snapshot(...)`, `send(...)`, `steer(...)`,
`interrupt(...)`, `update_settings(...)`, and `aclose()`, which disconnects
only and never terminates the backend:

```python
class AcmeRuntime(HarnessRuntime):
    """One participant's live runtime over the injected I/O seam."""

    def __init__(self, context: RuntimeContext) -> None:
        self._context = context  # immutable facts plus the injected RuntimeIO
        self._connection = None
        self._session_id = context.native_session_id
        self._active_turn: str | None = None
        self._source = AcmeLiveSource(self)

    async def open_session(
        self, *, mode: SessionOpenMode, native_session_id: str | None = None
    ) -> RuntimeBinding:
        if self._connection is None:
            self._connection = await self._context.io.connect(
                self._context.endpoint, timeout=10.0
            )
        # NEW opens the exact session the promptless native UI created;
        # FORK and RECONNECT carry the exact requested native id. Never
        # guess a session from the working directory — a mismatch fails
        # closed.
        reply = await self._connection.request(
            _open_method(mode), {"session_id": native_session_id}, timeout=10.0
        )
        self._session_id = _exact_session_id(reply)
        return RuntimeBinding(
            participant_id=self._context.participant_id,
            backend_generation=self._context.backend_generation,
            wiring=RuntimeWiring.NATIVE,
            native_session_id=self._session_id,
        )

    async def frontend_plan(self, *, native_session_id: str | None = None) -> LaunchPlan:
        # Native UI attachment only; the plan carries no initial prompt, and
        # the backend must not submit one independently either.
        argv = ["acme", "--remote", self._context.endpoint]
        if native_session_id is not None:
            argv += ["resume", native_session_id]
        return LaunchPlan(argv=argv)

    def live_source(self) -> Source:
        return self._source

    async def snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            participant_id=self._context.participant_id,
            backend_generation=self._context.backend_generation,
            native_session_id=self._session_id,
            native_turn_id=self._active_turn,
            capabilities=self._capabilities(),
            health=(
                ConnectionHealth.CONNECTED
                if self._connection is not None
                else ConnectionHealth.UNOPENED
            ),
        )

    async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
        reply = await self._connection.request(
            "turn/start", {"prompt": prompt}, timeout=10.0
        )
        # Report the turn the backend actually returned. If a simultaneous
        # native-UI submission absorbed this message into a human-started
        # turn, that is the turn to record — never fabricate a second one.
        self._active_turn = _exact_turn_id(reply)
        return ControlReceipt(
            operation_id=operation_id,
            result=DeliveryResult.ACCEPTED,
            native_turn_id=self._active_turn,
        )

    async def steer(
        self, *, operation_id: str, native_turn_id: str, prompt: str
    ) -> ControlReceipt:
        # Amend exactly the named turn; a stale-turn refusal stays a refusal.
        try:
            await self._connection.request(
                "turn/amend",
                {"expectedTurnId": native_turn_id, "prompt": prompt},
                timeout=10.0,
            )
        except RuntimeRequestError as error:
            return ControlReceipt(
                operation_id=operation_id,
                result=DeliveryResult.REJECTED,
                error_code="acme_stale_turn",
                error=error.message[:200],
            )
        return ControlReceipt(
            operation_id=operation_id,
            result=DeliveryResult.ACCEPTED,
            native_turn_id=native_turn_id,
        )

    async def interrupt(
        self, *, operation_id: str, native_turn_id: str | None = None
    ) -> ControlReceipt:
        await self._connection.notify("turn/interrupt", {"turnId": native_turn_id})
        return ControlReceipt(operation_id=operation_id, result=DeliveryResult.ACCEPTED)

    async def update_settings(
        self,
        *,
        operation_id: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ControlReceipt:
        # This Acme release exposes no settings API: refuse explicitly
        # instead of emulating unconfirmed application. snapshot() reports
        # the same fact through RuntimeCapabilities.
        return ControlReceipt(
            operation_id=operation_id,
            result=DeliveryResult.REJECTED,
            error_code="acme_settings_unsupported",
            error=(
                "the installed acme-cli exposes no settings update; settings "
                "are fixed at launch for this release"
            ),
        )

    async def aclose(self) -> None:
        # Disconnect Theater's connection only; never terminate the backend.
        if self._connection is not None:
            await self._connection.aclose()
            self._connection = None


def acme_runtime_factory(context: RuntimeContext) -> HarnessRuntime:
    return AcmeRuntime(context)


ACME_RUNTIME = RuntimeManifest(
    probe=probe,
    plan=plan_backend,
    factory=acme_runtime_factory,
    channel=LiveChannelDeclaration(
        channel=ChannelDeclaration(
            id="native-live",
            kind=ChannelKind.LIVE,
            capabilities=(
                ChannelCapability(SignalKind.CONTENT, SignalOwnership.PRIMARY),
                ChannelCapability(SignalKind.TURN, SignalOwnership.PRIMARY),
            ),
        ),
    ),
)
```

Wire it into the package manifest with `runtime=ACME_RUNTIME` (a relative
import from `.runtime`). The elided helpers (`_probe_installed_version`,
`_open_method`, `_exact_session_id`, `_exact_turn_id`, `_capabilities`, and
the `AcmeLiveSource`) are your native protocol details; the contracts require
exactness and bounds, not any particular wire vocabulary. Notes on each:

- `RuntimeConnection.request` raises the typed failures
  `RuntimeRequestError`, `RuntimeRequestTimeout`, and
  `RuntimeConnectionClosed`; never a raw transport exception. The injected
  implementation owns framing, correlation, deadlines, and its single
  bounded receive loop.
- `notifications()` yields `RuntimeNotification` values. Server requests
  (approvals, clarifications) carry `request_id` exactly as the native
  captured it — integer or string, never stringified for correlation.
  Theater records them; the runtime must never answer one.
- `ControlReceipt.result` is a delivery result — `ACCEPTED`, `REJECTED`, or
  `UNKNOWN` — never a job outcome. `UNKNOWN` means transmission or acceptance
  was uncertain: no retry, no tmux fallback, and the operation stays eligible
  only for reconciliation.
- `LiveChannelDeclaration` must wrap `ChannelKind.LIVE`. A live channel is
  never a transcript and never a database; it is the runtime's single live
  `Source`, and it must not be encoded as a `CompositeSource` enrichment.

### Lifecycle and reconnect ownership

The daemon owns every process fact. It persists launch intent before starting
the backend, launches a detached backend whose lifetime does not depend on
daemon pipes or shutdown, performs the native initialization handshake, and
persists the exact native session identity before the initial prompt is
delivered — exactly once, after verified UI readiness. The frontend command
must contain no initial prompt.

`open_session` modes:

- `NEW` — the UI-first order: the daemon launched the promptless native UI from
  `frontend_plan(native_session_id=None)` and the UI created the session; the
  runtime then opens exactly that UI-created session on this participant's
  verified backend and generation. It never guesses a session and never
  fabricates a second one.
- `FORK` — preserves native fork semantics from the given session id.
- `RECONNECT` — attaches to the exact existing session. Identity mismatch
  fails closed; never attach by working-directory resemblance.

Reconnect after a daemon restart re-verifies the backend's process identity,
adopts the already-running backend from the persisted binding, and
resubscribes without starting another conversation. `aclose()` disconnects
Theater's connections only — a healthy backend survives daemon shutdown and
is terminated exclusively by explicit participant kill or confirmed exit,
through daemon-owned teardown that verifies the process identity before
signaling.

The Codex plugin resumes with `excludeTurns=True` and consumes the separate,
bounded `initialTurnsPage.data`. Older exact outcomes come from one owned,
backpressured `thread/turns/list` task: 16 summaries per page, yielding after
64 pages without discarding its cursor. Transient failures retry with bounded
backoff; the pass limit is not a permanent history cutoff.

`RuntimeBinding` carries the persisted wiring, backend generation, lifecycle
phase, endpoint, verified pid, native session id, and protocol/version facts.
Approval, model, and reasoning configuration are applied to the backend that
runs the agent, never to the frontend alone; the binding carries no
credentials.

### Live observation: HybridSource and terminal evidence

Live observation stays `Source`/`Batch`; nothing about the durable
`observation.primary` contract changed for legacy plugins. Two additive
seams exist:

- `Batch.terminal_evidence` — an optional, default-empty sequence of
  `NativeTurnOutcome` values: exact native session/turn identity, the
  terminal outcome (`COMPLETED` / `FAILED` / `INTERRUPTED`), the available
  result, its completeness, and its provenance. It is bounded to 512
  outcomes per batch. Legacy durable sources never populate it; a live
  channel is never a transcript surrogate. Terminal evidence — not a
  status broadcast — is the only thing that completes a Theater job for a
  natively wired participant, and it is persisted before the completion
  becomes visible to awaiters.
- `Source.terminal_evidence_snapshot()` — returns only terminal evidence
  consumed from an upstream live source that has not yet been acknowledged
  as durably delivered. It is synchronous, cancellation-safe, and bounded
  by the same 512-outcome limit; the observer calls it only when
  cancellation can interrupt a composed read before its `Batch` reaches the
  watch loop, so exact evidence survives a cancelled poll. Legacy and
  durable-only sources inherit the empty snapshot.

History-derived outcomes set `from_history=True`; optional `completed_at`
is the native completion time in Unix seconds, never the time of ingestion.
Both survive persistence. An old interruption can finish its exact job, but
cancels only followups known to precede it or tied to that exact predecessor
when native time is absent or ordering within its second is ambiguous.

The daemon composes a `HybridSource` from the durable primary and the
runtime's single live channel — deliberately not another `CompositeSource`
enrichment, because enrichments can never drive authoritative events or
status. The authority split:

- The durable reader keeps attachment, identity, resume floors, history, and
  the persisted checkpoint cursor exactly as before.
- The live channel owns current-turn deltas, the authoritative status while
  it is healthy, and exact native terminal evidence.
- A delayed durable record enriches history; it cannot reopen a turn the live
  channel already reported terminal, and it cannot regress the status the
  live channel last reported while that channel stays healthy.
- On live overflow, error, or disconnect, the composition degrades visibly
  through channel health and reconciles from durable state. It never invents
  completion and never silently drops terminal evidence. Checkpoint
  acknowledgement and rollback are forwarded to both halves independently:
  the durable cursor is the persisted one, while unacknowledged live
  terminal evidence survives a rollback by replay, because it can never be
  produced again — the evidence sink's first-write-wins makes the replay
  idempotent.

For the Codex pilot, the durable parser remains canonical for usage and
billing; native cumulative totals are not added to durable per-response usage.

### Capabilities and version fallback

`RuntimeCapabilities` fails closed: the default supports nothing, a
capability is available only when explicitly listed in `available`, and every
unavailable capability reports one explicit `CapabilityUnavailableReason` —
`not_determined`, `unsupported_native_version`, `gated_by_backend`,
`session_state`, `wiring_mode`, or `theater_policy`. A capability present in
both sets is a construction error; honest refusal is the only way an
unsupported native capability stays disabled. Report per-session truth, not
launch-time truth: Codex reports `session_state` for steer when no turn is
active and for send/interrupt before the session is bound.

Compatibility is probed read-only through `RuntimeCompatibility` and must
mean Theater-verified compatibility, never presumed vendor stability: the
`policy` string names the tested policy and `native_version` the release it
was verified against. Unknown or unsupported versions keep the ordinary
launch under either `auto` or `native`.
The Codex runtime re-checks the version at the connection handshake, so a
binary that changed between probe and connect fails closed.

Settings are separately gated: a runtime must not emulate `update_settings`
by storing values that might apply to an unrelated future turn. Report
effective values only after native confirmation, and leave an uncertain
application visibly uncertain.

### Wiring selection: auto, native, legacy

`wiring` is `auto` | `native` | `legacy` on spawn surfaces
(`theater spawn --wiring ...` and the additive `wiring` spawn parameter);
`auto` is the default. Wiring is unrelated to approval, which still has no
default anywhere.

- `auto` and `native` are preferences: on a new Theater spawn they select a
  compatible runtime when available and otherwise retain the ordinary launch.
  A failed probe is diagnostic only; it does not disable a working legacy
  capability.
- `legacy` is the explicit opt-out and is honoured regardless of the gate.

Existing participants stay pinned to the wiring persisted in their binding; a
rollback to legacy affects future spawns only.

### Controls, jobs, and the queue

The public controls are thin daemon endpoints; the daemon owns every policy
decision and MCP forwards the actual calling participant:

| RPC | CLI | MCP | Semantics |
| --- | --- | --- | --- |
| `participant.steer` | `theater steer` | `steer_session` | Amend exactly the current Theater job's active turn. |
| `participant.queue_followup` | `theater queue` | `queue_followup` | Return a new awaitable send-job handle. |
| `participant.settings.update` | `theater settings` | `update_session_settings` | Idle-only model/reasoning changes. |
| `participant.controls` | `theater controls` | `get_session_controls` | Effective capabilities, health, settings, active turn, queued handles. |
| existing interrupt RPC | `theater interrupt` | `interrupt_session` | Cancel the active turn and every pending followup. |

What a runtime author must know about the surrounding semantics:

- **Ordinary `send` stays idle-guarded.** Known-busy targets are rejected and
  a send cannot jump ahead of queued followups. Pane ownership and copy-mode
  human-presence protection still run for every harness. Controls are
  serialized per participant — never a daemon-wide lock across native I/O.
- **Every mutating call receives an `operation_id`** the daemon reserved
  durably before transmission. A native request id is a correlation fact
  only — never a durable idempotency guarantee, and a persisted operation id
  never justifies retrying a native mutation. The delivery phase
  (`RESERVED` → `QUEUED`/`DISPATCHED` → `SETTLED`) is separate metadata; job
  state stays `running`/`done`/`crashed`/`killed`.
- **`UNKNOWN` delivery is terminal for retry purposes.** An interrupted
  transmission stays potentially delivered: no resend, no tmux fallback. If
  reconciliation cannot resolve it within its deadline, the affected job
  finishes as `crashed` with `delivery_unknown` and a warning that native
  work may have been accepted.
- **Steering amends the current job; it never creates a replacement handle.**
  It requires an active native turn mapped to a running Theater job, sends
  that exact turn id, and preserves the original prompt and response-format
  contract. A stale-turn refusal stays a refusal — never reinterpreted as
  send or queue — and a human-only turn is never given a synthetic job.
- **The followup queue lives entirely in Theater.** Items are reserved in
  `QUEUED` phase with their position from the persisted send-sequence
  allocator, bounded to 32 pending per participant, and dispatched FIFO one
  prompt at a time after an authoritative idle check. Ownership is
  revalidated when an item actually dispatches. A temporarily busy or
  human-present target leaves the item queued; lost ownership, a dead
  target, or a definitive refusal finishes it with an explicit error. A
  normally failed turn does not cancel later followups; an interrupted turn
  cancels every remaining item — including interruption initiated in the
  native UI. Daemon restart fails never-dispatched followups with
  `daemon_restarted` and never replays them.
- **Interrupt cancels first, then interrupts the exact turn.** Pending
  followups are cancelled durably before the native interruption request;
  an interrupt while already idle still clears the queue. The active job
  finishes only from authoritative terminal evidence — an interrupted native
  turn maps to `killed` with an interruption error code, and if completion
  won the race, first-terminal-write-wins. Late evidence never rewrites
  terminal job state.
- **Exact job/turn mapping is enforced by the daemon.** Two Theater jobs
  never bind to one native turn; a conflicting binding fails closed.
  Terminal evidence is keyed by native session and turn identity — text
  equality is not identity.
- **Authorization:** ordinary send retains its current permissions; steer,
  queue, settings, and interrupt require the direct parent or the local
  CLI/régie operator, following the existing kill semantics. Settings
  updates enforce the model/reasoning allowlists and can never change
  approval or sandbox policy.

### The guarded-idle race

Idle checks are guarded, not atomic against simultaneous human input in the
native UI. Theater serializes its controls per participant and rejects
known-busy sessions, but a submission typed into the native UI at the same
moment can cause the backend to accept a Theater send into that human-started
turn. The runtime must report the actual returned turn — never fabricate a
separate one — and the daemon records it rather than inventing a second job.
The same limitation applies to idle-guarded settings updates. There is no
input gateway and no native persistent queue; this race is accepted and
documented, not solved.

### Tested Codex compatibility boundaries

The shipped Codex runtime is verified against exactly what its Wave 0 native
proof exercised: compatibility policy `codex-appserver-0.154-verified`, pinned
to `codex-cli 0.154.0`. The probe runs `codex --version` and refuses any
other release. Within that boundary, the tested facts are: one detached
`codex app-server --listen unix://<private-socket>` backend per participant
(the endpoint carries WebSocket frames with an HTTP Upgrade handshake, not
Theater NDJSON); the stock native CLI UI attached to the same thread; new,
fork, and reconnect behavior; approval and clarification handling with both
clients subscribed — only the native UI answers; steering, interruption, and
(separately gated, experimental) settings operations; and backend/UI survival
after abrupt daemon death.

Vendor documentation labels the WebSocket transport experimental. Theater's
verification covers the pinned release above under the tested policy — it is
not a claim of universal transport stability across Codex versions. With the
Wave 5 release gate passed, `auto` on a new Codex spawn selects native only
inside that verified boundary — codex-cli 0.154.0. Outside it, `auto` and
`native` retain the ordinary launch with the recorded compatibility reason.

### Legacy opt-out and recovery

- Opt out per spawn with `theater spawn --wiring legacy` (or the `wiring`
  spawn parameter). This is honoured regardless of the rollout gate.
- `auto` now selects native for verified-compatible new Codex spawns on the
  pinned release. Today's other shipped harnesses — and any local override
  without a runtime manifest — keep the pane-driven legacy behavior under
  `auto`; the generic rule is unchanged: `auto` selects native only for a
  harness whose runtime manifest's compatibility probe verifies the
  installed release.
- Existing natively wired participants stay pinned to their persisted wiring;
  a rollout rollback only selects legacy for future spawns. To move an
  existing conversation off native wiring, resume it with `wiring=legacy`.
  A detached-host native fork requires its predecessor's persisted native
  session identity; a frontend host keeps its harness's ordinary resume plan.
- A disconnected native runtime fails closed only for capabilities selected
  for its native transport. Manifest-declared legacy fallback capabilities
  keep their existing pane route and guards.
- A harness without a runtime manifest, and a local override that omits the
  field, keep the existing launch, observation, send, and interrupt behavior
  unchanged. Queueing works for them too: a followup dispatches through the
  pane after the legacy idle check.

### The MCP constraint is unchanged

MCP still has no server-initiated turn. Native runtime control is an
additional daemon-owned path: a backend connection or a passive frontend
extension publishing bounded local observations to a daemon-owned Unix
listener. The daemon remains the sole SQLite writer and the only process that
signals or injects into participant panes. The MCP tools are thin forwards of
the same RPCs the CLI uses, and an agent still cannot be woken by a
server-initiated turn.

### Same-session frontend extensions

The shipped OpenCode and Pi plugins use `RuntimeHost.FRONTEND`. Their
`frontend_installer` returns only a launch-local environment/file overlay;
the ordinary argv, approval, user configuration, stores and native UI remain
owned by the harness. An installer failure retains the ordinary launch.
The daemon provisions an independent LIVE credential and passes its private
file path plus a participant Unix endpoint to the extension. It authenticates
the connection before activating the runtime and keeps connection handlers
bounded during replacement and shutdown.

Declare each retained pane route in `legacy_fallback` and each unsupported
control in `unavailable_capabilities`. OpenCode retains send, queued followups
and interrupt and exposes status only. Pi retains those same routes and adds
confirmed thinking updates; model updates are refused because its public API
does not provide the required atomic session guard. Both declare
`drives_job_completion=False`: durable sources own results, tools, usage and
completion. Their live source must revalidate trusted identity and freshness
in `validate_enrichment_batch` after sibling sources yield. A live idle hint
must never complete a legacy-delivered job by itself.

Runtime host and control routes are persisted at launch. Disconnected
frontends therefore retain their proven pane controls. Native requests with
an uncertain outcome are never replayed, and recovering a frontend listener
does not replace its stock UI or discard a provably unsent legacy FIFO.

## Offline authoring checks

The loader can be exercised without starting a daemon or a real CLI. This
self-contained test creates `acme/manifest.py` and installs it from a
temporary local root:

```python
import textwrap

from theater.config import Config
from theater.harness.registry import HARNESSES, install


MANIFEST_SOURCE = """
from theater.harness.contracts.callbacks import LaunchContext, ScreenContext
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.manifest import (
    MANIFEST_API_VERSION, HarnessManifest, LaunchManifest, ObservationManifest, ScreenManifest,
)
from theater.harness.contracts.observation import ScreenKind, ScreenReading

def plan(context: LaunchContext) -> LaunchPlan:
    return LaunchPlan(argv=["acme"])

def screen(context: ScreenContext) -> ScreenReading:
    return ScreenReading(ScreenKind.UNKNOWN)

MANIFEST = HarnessManifest(
    api_version=MANIFEST_API_VERSION, binary="acme", icon="A",
    launch=LaunchManifest(planner=plan, approvals=("manual",)),
    observation=ObservationManifest(primary=None, screen=ScreenManifest(classifier=screen)),
)
"""


def test_acme_manifest_loads(tmp_path):
    package = tmp_path / "acme"
    package.mkdir()
    (package / "manifest.py").write_text(textwrap.dedent(MANIFEST_SOURCE))

    assert "acme" in install(Config(), local_dir=tmp_path)
    assert HARNESSES["acme"].binary == "acme"
```

Keep loader tests deliberately small: prove discovery, manifest compilation,
and local precedence. Add one relative helper module when testing isolated
relative imports. Test `plan_launch`, source attachment/read/error paths,
history, receipt validation, resume overlays, and screen classification
directly with their frozen contexts. Restore the normal registry in fixture
teardown if the test suite continues in the same process.

## Validation and failure behavior

| Problem | Result |
| --- | --- |
| Missing `manifest.py` or `MANIFEST` | Rejected with a path-qualified migration/load diagnostic. |
| `MANIFEST` is a class or wrong type | Rejected; it must be a `HarnessManifest` instance. |
| Import error or invalid API version | Rejected with the manifest path and failure. |
| Invalid directory/name, icon width, approvals, bounds, callbacks, or signal ownership | Rejected by manifest validation before spawn. |
| Broken local plugin | Skipped with a warning and visible to `theater harnesses`. |
| Broken shipped plugin | Fatal startup error, unless disabled before import. |
| Alias, binary, or same-source name collision | Fatal; ambiguity is never resolved by load order. |
| Local package shares a shipped canonical name | Supported local override. |

## Safety checklist

- Keep durable transcript/database observation authoritative; optional
  enrichment must fail independently.
- Bound queues, payloads, parser work, retries, retained identities, and
  history/page reads.
- Preserve exact participant correlation and do not log raw credentials or
  native payloads.
- Never answer a native approval or clarification; record it and let the
  human answer in the native UI.
- `aclose()` disconnects only; a backend is terminated exclusively by
  daemon-owned teardown after process-identity verification.
- Do not alter global/project hooks or steal an OTel exporter.
- Preserve daemon-only ownership of SQLite, pane lifecycle, and tmux input.

## Trust

A plugin is Python executed by the daemon under the daemon user's privileges.
Treat it like a shell plugin: inspect it before installing it and keep the
plugin directory writable only by trusted users.
