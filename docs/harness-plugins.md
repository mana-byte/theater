# Harness plugins

A harness plugin teaches Theater how to launch and observe one coding-agent
CLI. It is a named package directory, not a loose Python file. Use public
`theater.harness.contracts` modules; do not import a shipped plugin's private
implementation.

## Package layout and loading

Place a local plugin under `$THEATER_HOME/harnesses/` (normally
`~/.theater/harnesses/`):

```text
$THEATER_HOME/harnesses/
└── acme/
    ├── manifest.py
    ├── launch.py
    ├── source.py
    └── screen.py
```

The directory name, here `acme`, is the canonical harness name. It must use
lowercase letters, digits, `_`, or `-`, starting with a letter or digit.
`manifest.py` exports one root value named `MANIFEST`; there is no separate
manifest name field.

The loader imports each directory as an isolated synthetic package. Relative
imports such as `from .source import source_factory` work, sibling modules in
different plugins cannot collide, and the loader never changes `sys.path`. A
failed import is cleaned from `sys.modules`.

Shipped packages are scanned first and local packages second: a local package
with the same canonical name deliberately overrides a shipped one. A broken
shipped plugin stops startup; a broken local plugin is skipped. The
`theater harnesses` command reports loaded and rejected plugins at the behavior
level; its output schema is not an authoring API.

`[harness].disabled` filters a directory name before import, so it can disable
a plugin that would otherwise fail during import. A top-level legacy file such
as `$THEATER_HOME/harnesses/acme.py` is never executed. It receives this
actionable migration diagnostic:

```text
legacy single-file plugin. Move acme.py to acme/manifest.py and export MANIFEST
```

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
  attachment, if the source supports receipts.

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
`ResumeLaunchOverlay(env=..., transcript_domain=..., cwd=...)`. The overlay may
only adjust those fields and an optional authoritative launch `cwd`; core
applies a cwd override before it creates the successor participant.
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

Claude, Codex, OpenCode, and Vibe explicitly declare richer hook and native
OTel channels, but all are currently unavailable. Their durable transcript or
database sources remain authoritative until safety and evidence gates pass.
Do not advertise an unimplemented native integration.

### Diagnostics

`theater harnesses` derives static capabilities from each validated manifest. It shows the
package and manifest paths, primary source, enrichment bindings, normalized signals, ownership,
and explicit unavailability reasons. With a daemon running, it also shows participant-scoped
channel state, accepted and dropped counts, last successful activity, and the newest bounded
diagnostic. `--json` exposes the same bounded data for tooling.

Runtime diagnostics contain no credentials, native payloads, prompts, or results. A malformed
plugin health snapshot is ignored, and an enrichment-health failure cannot interrupt the durable
source. Without a daemon, the command reports static manifest capabilities only.

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
- Do not alter global/project hooks or steal an OTel exporter.
- Preserve daemon-only ownership of SQLite, pane lifecycle, and tmux input.

## Trust

A plugin is Python executed by the daemon under the daemon user's privileges.
Treat it like a shell plugin: inspect it before installing it and keep the
plugin directory writable only by trusted users.
