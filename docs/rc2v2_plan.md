# RC2v2 plugin manifests and signal channels

Status: implemented through Phase 7 on `mana/rc3-plugin-rework`; final validation and dogfood are
in progress.

This plan supersedes the plugin-layout and future-channel sections of
`docs/rc3_plugin_rework.md`. The modular adapter work already on
`mana/rc3-plugin-rework` remains the behavioral baseline, but its temporary split between
`builtin/plugins/*.py` and `builtin/adapters/<name>/` is not the final architecture.

## Goal

Make a Theater harness plugin a self-contained package described by immutable manifests.
Theater provides reusable launch, observation, composition, hook, OTel, normalization, health,
and lifecycle machinery. A plugin selects that machinery declaratively and supplies explicit typed
callbacks only for genuinely native behavior.

The framework is horizontal. Claude, Codex, OpenCode, Vibe, and third-party harnesses receive the
same contracts and channel implementations. A harness may use more of them than another because
upstream capabilities differ, but Theater does not design or privilege a path for one named
harness.

## Required outcomes

1. A plugin is a named directory containing `manifest.py`, not a loose Python file.
2. The directory name is the canonical harness name.
3. `manifest.py` exports one root `MANIFEST` composed from focused sub-manifests.
4. Custom functions are accepted only through explicit typed manifest fields.
5. All shipped harness-specific production code lives under that harness's plugin package.
6. Shared code contains no native harness names, schemas, paths, markers, commands, or branches.
7. Hooks and native OTel are reusable channel implementations, not per-harness transports.
8. Durable transcript/database input remains the default semantic and history authority.
9. Existing daemon policy, SQLite ownership, tmux safety, job completion, and régie behavior remain
   unchanged unless a later fidelity commit explicitly changes a normalized fact.
10. Channel absence or failure is visible, bounded, and cannot make a healthy durable source fail.

## Non-negotiable boundaries

- The daemon remains Theater's sole SQLite writer and the only process allowed to create, destroy,
  respawn, or inject input into participant panes.
- Plugins report facts. They never change participant status, complete jobs, publish directly to
  the bus, or invoke tmux control operations.
- The reducer remains the sole owner of observation policy and the three independent quiet timers.
- Human presence continues to use `pane_in_mode` only. Screen manifests cannot authorize input.
- A native signal may enrich a durable fact only through an exact native key. Timestamp proximity,
  cwd, model name, or prose similarity is never an identity join.
- Hooks and OTel remain bounded, lossy enrichment unless a particular control event has an exact
  fixture and contract test proving stronger semantics.
- No channel blocks the daemon event loop. File, database, network, parsing, queues, retries,
  payloads, and retained state are bounded.
- Theater never rewrites a user's global/project hook configuration or steals an existing OTel
  exporter. Unsafe installation means that channel stays unavailable.
- Plugin-native constants remain inside the plugin package. Cross-harness defaults and limits
  belong in `theater/constants/`; user-controlled values belong in `theater/config/`.
- Built-in plugins use only the public plugin contracts. There is no privileged built-in API.

## Target filesystem layout

```text
theater/harness/
├── contracts/
│   ├── manifest.py             immutable root/sub-manifest values
│   ├── callbacks.py            typed callback protocols and runtime contexts
│   ├── channels.py             channel, signal, ownership, and health values
│   ├── context.py              ParticipantObservationContext
│   ├── events.py
│   ├── harness.py
│   ├── launch.py
│   ├── observation.py
│   ├── source.py
│   └── trajectory.py
├── manifests/
│   ├── compiler.py             manifest -> Harness/HarnessObserver runtime objects
│   ├── validation.py           structural and semantic validation
│   └── strategies.py           reusable launch/source/screen strategy constructors
├── loading/
│   ├── discovery.py            named-directory discovery and precedence
│   ├── importer.py             isolated synthetic package imports
│   └── models.py               loaded/broken plugin result values
├── channels/
│   ├── composite.py            primary plus bounded enrichment composition
│   ├── health.py               per-participant runtime channel state
│   ├── hooks/
│   │   ├── ingress.py          authenticated daemon RPC and validation
│   │   ├── inbox.py            bounded queues, dedupe, overflow accounting
│   │   ├── source.py           hook facts exposed through Source
│   │   └── __init__.py         public hook-channel API
│   └── otel/
│       ├── receiver.py         optional loopback OTLP receiver
│       ├── bounds.py           request and attribute validation
│       ├── source.py           accepted native telemetry as enrichment
│       └── __init__.py         public native-OTel channel API
├── normalization/
│   ├── values.py               IDs, finite numbers, bounded text, timestamps
│   ├── timing.py               native/observed/estimated timing provenance
│   ├── usage.py                token usage and cost provenance
│   ├── tools.py                tool arguments, results, errors, and file paths
│   └── facts.py                normalized Event/TrajectoryFact builders
├── builtin/plugins/
│   ├── claude/
│   │   ├── manifest.py
│   │   └── ...                 all Claude-specific implementation modules
│   ├── codex/
│   │   ├── manifest.py
│   │   └── ...
│   ├── opencode/
│   │   ├── manifest.py
│   │   └── ...
│   └── vibe/
│       ├── manifest.py
│       └── ...
├── plugins.py                  compatibility facade over loading/
└── ...                         existing public compatibility facades
```

`theater/harness/builtin/adapters/` and loose files under
`theater/harness/builtin/plugins/` disappear after migration. No compatibility facade may leave
harness-specific production logic outside `builtin/plugins/<harness>/`.

Local plugins use the same shape:

```text
$THEATER_HOME/harnesses/
└── acme/
    ├── manifest.py
    ├── constants.py
    ├── launch.py
    ├── parser.py
    ├── hooks.py
    └── screen.py
```

Only `manifest.py` is loaded automatically. Sibling modules execute only when imported by the
manifest or one of its imports. Nested packages are allowed.

## Manifest model

### Root manifest

The root value is frozen, slotted, and versioned:

```python
MANIFEST = HarnessManifest(
    api_version=PLUGIN_API_VERSION,
    binary="acme",
    binaries=frozenset(),
    icon="◇",
    aliases=("acme-cli",),
    launch=LAUNCH,
    observation=OBSERVATION,
    models=MODELS,
)
```

The folder name supplies `Harness.name`; the manifest does not duplicate it. Validation receives
the canonical folder name and compiles it into the runtime `Harness` object. Renaming a directory
therefore renames the plugin deliberately, while aliases remain explicit manifest data.

`manifest.py` may compose values defined in sibling modules:

```python
from .hooks import HOOKS
from .launch import LAUNCH
from .observation import OBSERVATION

MANIFEST = HarnessManifest(
    api_version=PLUGIN_API_VERSION,
    binary="acme",
    icon="◇",
    launch=LAUNCH,
    observation=replace(OBSERVATION, enrichments=(*OBSERVATION.enrichments, HOOKS)),
)
```

The exact constructor API should favor simple tuples and frozen values over a Python-shaped TOML
DSL. Python functions are already the escape hatch; Theater should not invent a second programming
language inside the manifest.

### Focused sub-manifests

The root composes responsibility-specific values rather than accumulating optional fields:

- `LaunchManifest`: launch planner, approval/model/reasoning support, generated files, environment,
  and resume/fork strategy.
- `ObservationManifest`: primary durable source, ordered enrichment channels, screen classifier,
  and declared normalized capabilities.
- `TranscriptChannelManifest`: discovery, identity, record decoding, history, rotation, and
  compaction behavior for append-oriented stores.
- `CustomSourceManifest`: explicit factory for sources such as OpenCode's read-only SQLite model
  when a generic transcript strategy would lie about its semantics.
- `HookChannelManifest`: launch-local installation strategy and native event bindings.
- `OtelChannelManifest`: opt-in native endpoint configuration and signal bindings.
- `ScreenManifest`: exact ordered markers or a typed classifier callback.
- `ModelDiscoveryManifest`: an optional typed model discovery callback.
- `UnavailableChannelManifest`: an explicit, user-visible reason a known channel cannot be safely
  installed or decoded.

An absent optional manifest means the plugin makes no claim. An unavailable manifest means the
plugin deliberately reports a known limitation. Static capability and runtime health remain
separate concepts.

### Explicit callback seam

Every custom function appears in a named manifest field governed by one narrow `Protocol`.
Examples include:

- `LaunchPlanner`
- `ResumePlanner`
- `SourceFactory`
- `TranscriptLocator`
- `IdentityResolver`
- `RecordDecoder`
- `HistoryDecoder`
- `HookDecoder`
- `OtelSignalDecoder`
- `ScreenClassifier`
- `ModelDiscoverer`

Each protocol receives a frozen typed context and returns a contract value. Manifest runtime code
does not inspect callback signatures, pass open-ended `**kwargs`, search module globals, infer a
callback from its name, or branch on the owning harness. The legacy signature-introspection path
may exist only while old internal code is being migrated; it is absent from the final manifest
runtime.

Callbacks fall into two explicit categories:

1. Pure decoders/normalizers perform bounded native-value-to-contract conversion and no I/O.
2. Factories/planners may perform only the I/O ownership documented by their return contract. A
   custom source factory may open a harness-owned read-only database; it still cannot reach daemon
   state or Theater's SQLite connection.

The function implementation may live in `parser.py`, `source.py`, or another module beside the
manifest. The manifest must visibly wire it. There is no generic `custom` mapping or opaque escape
hatch accepting arbitrary objects.

### Compilation

`compile_manifest(name, manifest)` produces runtime objects implementing the existing `Harness`,
`HarnessObserver`, and `Source` seams. The daemon, spawning service, reducer, registry, MCP server,
and régie continue consuming those interfaces and do not learn about manifest internals.

Compilation performs all deterministic validation before a participant can spawn:

- API version support;
- folder-name syntax;
- binary, aliases, icon width, and collisions;
- immutable container normalization;
- required primary/source semantics;
- channel identifiers and unique bindings;
- explicit ownership or fallback for overlapping signals;
- required correlation keys for enrichment;
- bounded queue/payload settings;
- callable presence and correct manifest field type;
- configuration combinations that would steal global hooks or exporters.

Built-ins are type-checked by mypy and exercised by a parameterized conformance suite. Runtime
validation checks structure and callability, not Python signature introspection.

## Package discovery and loading

### Discovery rules

The scanner considers sorted direct child directories only. A candidate must:

- not begin with `.` or `_`;
- have a name matching `HARNESS_NAME`;
- contain a regular `manifest.py` file.

The disabled-plugin list is applied to the directory name before any file is imported. A disabled
broken plugin therefore cannot prevent daemon startup.

A visible directory without `manifest.py` is reported as a broken plugin rather than silently
ignored. Unrelated files in `$THEATER_HOME/harnesses` are ignored except legacy top-level `*.py`
plugins, which receive a targeted migration diagnostic.

### Isolated imports

The loader creates a synthetic package keyed by source and resolved directory, sets its
`__path__`, and loads `manifest.py` as that package's `manifest` submodule. This permits ordinary
relative imports such as `from .parser import decode` without adding the plugin directory to
`sys.path`. Two plugins may both contain `parser.py`, `constants.py`, or nested packages without
colliding with each other or the standard library.

`__init__.py` may exist but is not the entrypoint and is not required. Only `manifest.py` defines
the plugin contract. On import failure, every module under that synthetic package prefix is removed
from `sys.modules`, so a rescan cannot reuse partially initialized state.

Built-in and local packages go through the same importer. A local package with the same canonical
name continues to override a shipped package according to existing registry rules; aliases and
binary claims are validated after compilation exactly as they are today.

### One-file transition

RC2v2 intentionally changes the plugin format. Top-level `foo.py` is never executed. It is returned
as a broken legacy plugin with an actionable error:

```text
Move foo.py to foo/manifest.py and export MANIFEST.
```

The CLI must not silently hide it. Documentation, `AGENTS.md`, examples, fixtures, and tests are
updated in the same release. Because Theater is still in release-candidate development, no second
long-lived loader architecture is retained solely for the old file form.

Registry installation and rebuild become directory-aware. Theater does not currently copy, update,
or remove plugin files for users, and this work does not add such a command. Users manage the named
directory under `$THEATER_HOME/harnesses`; a rescan treats a removed directory exactly like a
removed legacy plugin file today.

## Built-in package rule

All production behavior specific to a shipped harness belongs under:

```text
theater/harness/builtin/plugins/<harness>/
```

That includes:

- binary names, aliases, glyphs, flags, commands, and native constants;
- launch and resume planning;
- generated native configuration and plugin files;
- identity, receipt, transcript, session, and collision rules;
- transcript/database discovery and reading;
- parsing, history, trajectory, tool, timing, usage, and cost inputs;
- hook names, installation, schemas, and decoding;
- OTel signal names, attributes, and decoding;
- screen prompt/working/approval/trust classification;
- native sub-agent interpretation.

Production modules outside those packages may refer only to generic contracts and normalized
values. They may not import a built-in package or test `harness == "claude"` and equivalents.
Tests and documentation may name built-ins. An architecture test walks production imports and
rejects imports of built-in internals outside their own packages.

## Shared channel framework

### Channel contracts

`contracts/channels.py` defines immutable generic values:

- `ChannelKind`: durable transcript/database, hook, OTel, screen, process;
- `SignalKind`: identity, lifecycle, content, turn, model, tool, timing, usage, and lineage;
- `SignalOwnership`: primary, enrichment, or explicit fallback per signal;
- `ChannelCapability`: what normalized facts an implementation can produce;
- `ChannelHealth`: inactive, starting, healthy, degraded, failed, plus bounded diagnostics;
- stable native record identity and revision values used for deduplication.

There is no global numeric source precedence. Ownership is per signal because a transcript may own
content while an exact hook owns tool duration and process facts own liveness.

### Composite source

`CompositeSource` preserves the existing `Source` seam. It owns one optional primary durable
source and zero or more enrichment sources.

- Only the primary source owns attachment, history, identity-loss evidence, collision domain, and
  default status in the first implementation.
- Enrichment facts join durable facts only by declared exact keys.
- Overlapping ownership without an explicit fallback is rejected at construction.
- Child reads may execute concurrently behind bounded timeouts, but results merge in declared
  order so scheduling cannot change output.
- Duplicate `(channel, native_id, revision)` records are idempotent.
- A failed enrichment source updates health and leaves the primary source observable.
- Telemetry heartbeats never manufacture `Batch.progressed`.
- Close is idempotent, attempts every child, and preserves cancellation and the first real error.
- Screen-only plugins remain valid with no primary source and the existing conservative fallback.

### Common hook channel

Hook transport and lifecycle are entirely shared. Plugin packages supply only installation
manifests, native bindings, and decoders.

The common path is:

1. A hook-capable launch manifest requests a participant-scoped credential and token-file path.
2. Core mints and persists the credential, writes the private file mode `0600`, and supplies only
   its location to generated native configuration.
3. A native hook invokes `theater harness-event <event>` and writes bounded JSON to stdin.
4. The CLI forwards an opaque envelope to one generic daemon RPC.
5. The daemon authenticates participant, harness, channel scope, delivery ID, and payload bounds
   before enqueueing.
6. A bounded per-participant inbox performs idempotent delivery and records overflow.
7. `HookSource` asks the plugin's declared binding to decode payload into normalized facts.
8. `CompositeSource` joins those facts according to declared ownership and native keys.

`HookChannelManifest` contains an ordered tuple of `HookBinding` values. Each binding declares:

- native event name;
- normalized signals it may produce;
- exact identity/correlation extractor;
- typed decoder;
- authority/fallback status;
- whether native delivery is ordered, retried, or best-effort;
- applicable upstream version constraints when known.

Hook names and payload fields never enter common code. A similarly named `Stop` event from two
harnesses is not assumed to have identical semantics.

Ingress rejects forged tokens, wrong participants/harnesses, disabled bindings, unknown events,
oversized bodies, excessive nesting/attributes/text, invalid UTF-8, and duplicate delivery IDs.
Overflow drops according to one documented bounded policy, increments health counters, and never
blocks a native hook process or the daemon loop.

Receipt identity and richer live events remain separate signal meanings even if they share the
credential and forwarding primitives. Existing trusted transcript receipt behavior must not be
weakened or silently changed by the hook generalization.

### Common native OTel channel

Inbound harness telemetry is distinct from `theater/observability/`, which exports Theater's own
signals. Native ingress lives under `theater/harness/channels/otel/` and uses the same channel,
ownership, health, and composition contracts as hooks.

- Receiver binds loopback only.
- Participant correlation uses launch-local authenticated headers or exact injected resource
  attributes, never cwd/model/time heuristics.
- Protocols are implemented only when a shipped or fixture plugin declares them.
- Request size, signal count, attribute count/length, queue depth, retention, and decode time are
  bounded.
- Retries and duplicate exports are idempotent.
- Sampling, redaction, missing content, and exporter conflicts appear as partial coverage.
- Metrics alone never reconstruct semantic trajectory.
- Optional OTel receiver dependencies are lazy and failure disables only this channel with an
  actionable diagnostic.
- Existing user exporters are never redirected silently. Safe additive fan-out or explicit user
  opt-in is required before a plugin activates native OTel.

An `OtelChannelManifest` declares installation strategy, protocol, resource/header correlation,
and native signal bindings. All vendor signal and attribute names remain in that plugin package.
An unavailable manifest is a valid result when upstream cannot satisfy correlation or fan-out.
The installer receives only the private token-file location. It declares a dedicated exporter
header environment-variable name; core injects the authenticated header value after the callback
returns because native OTLP exporters require the credential in their launch environment.

## Shared normalization

Normalization modules provide small typed functions, not a second parser framework. Extract a
helper when semantics are shared, not merely when two native fields have similar spellings.

- `values.py`: bounded scalar/text/ID coercion and timestamp parsing.
- `timing.py`: native, observed, and estimated intervals with explicit provenance.
- `usage.py`: input/output/cache token normalization and reported/estimated cost provenance.
- `tools.py`: structured tool names, inputs, results, errors, and file declarations.
- `facts.py`: stable normalized event and trajectory construction.

Built-ins should contain almost no custom infrastructure. Custom semantic decoders are expected,
especially for OpenCode's mutable relational model. If multiple built-ins need the same mechanic,
it moves into a named shared helper. If only one native format needs it, it stays in that plugin and
is wired through a typed manifest callback.

## Capabilities and diagnostics

Capabilities are derived from validated manifests, not maintained in a second handwritten table.
Runtime health is derived from active channel state, not static capability.

`theater harnesses` should report, per plugin:

- package path and manifest API version;
- installed executable state;
- primary source kind;
- declared hook names and normalized signals;
- declared native OTel signals;
- active/inactive/unavailable channel state;
- last successful receipt/export;
- bounded decode, authentication, overflow, retry, and correlation errors;
- partial coverage or disabled optional dependencies.

Diagnostics must not include tokens, prompt/result bodies, raw credentials, or unbounded native
payloads.

## Implementation phases

### Phase 0 — freeze and characterize

1. Confirm a clean branch and green full suite.
2. Preserve compact fixtures for launch, resume/fork, identity, live observation, history,
   trajectory, usage, timing, tools, and screen readings for all four built-ins.
3. Record current plugin-loader precedence, disabling, error, installation, and removal behavior.
4. Capture real native hook/OTel payloads where safely possible, with exact installed versions.
5. Separate structural parity tests from intentional future fidelity tests.

Gate: every built-in has reviewed before/after outputs; no structural commit is allowed to change
them.

### Phase 1 — manifest contracts and compiler

1. Add immutable root and sub-manifest types.
2. Add typed callback protocols and frozen runtime context values.
3. Add validation with actionable path-qualified errors.
4. Implement generic runtime `Harness`/`HarnessObserver` objects compiled from manifests.
5. Add reusable strategy constructors without migrating built-ins yet.
6. Add a synthetic manifest-only harness covering launch, source, screen, model discovery, and one
   custom callback.

Gate: compiler passes contract tests without importing daemon, régie, tmux control, or built-ins.

### Phase 2 — package loader

1. Split the current monolithic loader into discovery, isolated import, and validation modules.
2. Discover named folders and load fixed `manifest.py` entrypoints as synthetic packages.
3. Support relative sibling and nested imports without modifying `sys.path`.
4. Apply disabling before import and clean all package modules after failed imports.
5. Preserve local-over-shipped precedence and collision diagnostics.
6. Add explicit legacy-file migration errors.
7. Make registry installation/rebuild directory-aware without adding filesystem management commands.

Gate: synthetic shipped and local packages traverse the exact same loader; collision, failure, and
isolation tests pass.

### Phase 3 — generic sources, channels, and normalization

1. Add channel/signal/ownership/health contracts.
2. Implement `CompositeSource` and bounded child lifecycle.
3. Extract currently duplicated value and timing normalization.
4. Add usage, tool, and fact helpers only where current adapters already prove shared semantics.
5. Add generic transcript/source strategy manifests around existing source behavior.
6. Add parameterized source/channel conformance tests.

Gate: no daemon policy changes and no built-in names in shared modules.

### Phase 4 — migrate every built-in package

Move all four shipped adapters to `builtin/plugins/<name>/` against the same accepted manifest API.
The work may run in parallel, but no single adapter defines a private extension or becomes a
special compiler branch.

For each built-in:

1. Move every native implementation module into its plugin directory.
2. Compose launch, primary source, screen, identity, model, and capability manifests.
3. Wire custom functions only through declared callback fields.
4. Replace direct class construction with manifest compilation.
5. Preserve golden output and public command behavior.
6. Update tests to import the new package only where a native fixture genuinely needs it.

After all four pass:

1. Delete `builtin/adapters/`.
2. Delete loose built-in plugin files and temporary loader compatibility.
3. Remove dead classes, helpers, imports, and placement tests.
4. Add the production import-boundary architecture test.

Gate: all four pass the same conformance suite and full behavioral parity matrix.

### Phase 5 — common hook capability

1. Add participant-scoped channel credentials without weakening receipt authentication.
2. Add generic CLI/RPC ingress, bounds, dedupe, and audit events.
3. Add bounded inbox, `HookSource`, health, and lifecycle cleanup.
4. Add declarative install strategies for launch-local settings, generated config, environment,
   and generated native plugin files.
5. Add hook binding manifests and decoder protocols.
6. Make every built-in declare its safely implemented bindings or an explicit unavailable reason.
7. Enable all integrations that satisfy the same fixture, isolation, and safety gates; do not ship
   guessed support to manufacture parity.

Gate: one generic synthetic plugin proves the full transport independently of a vendor; every
built-in reports an honest capability; hook failure leaves durable observation intact.

### Phase 6 — common native OTel capability

1. Add optional bounded loopback receiver and lifecycle.
2. Add authenticated participant correlation and signal dedupe.
3. Add OTel binding manifests and decoder protocols.
4. Make every built-in declare safe support or an explicit unavailable reason.
5. Enable only non-destructive integrations with captured payload fixtures and exact joins.
6. Verify coexistence with Theater's outbound observability and any configured user exporter.

Gate: disabling native ingress restores exact Phase 4 normalized behavior; enabling it adds facts
without duplicate trajectory, usage, cost, logs, spans, or metrics.

### Phase 7 — diagnostics, documentation, and cleanup

1. Derive capability output from manifests and expose runtime channel health.
2. Rewrite `docs/harness-plugins.md` around package manifests and explicit callbacks.
3. Update `docs/architecture.md`, `AGENTS.md`, README references, examples, and config descriptions.
4. Remove old helper-import machinery and every obsolete compatibility path.
5. Audit for dead code, harness-name branches, misplaced native constants, and verbose comments.
6. Update any workflow, script, packaging rule, or test fixture that assumes `plugins/*.py`.

Gate: a new third-party package can be authored from documentation without importing any private
module.

### Phase 8 — full validation and dogfood

1. Run full pytest, coverage, Ruff check/format, mypy, and Alembic check.
2. Run real tmux tests under `nix develop`.
3. Spawn concurrent Claude, Codex, OpenCode, and Vibe sessions in same and different cwd values.
4. Exercise spawn, resume/fork, send/await, history, trajectory, native sub-agents, kill, daemon
   restart, and régie restart.
5. Exercise hooks/OTel enabled, disabled, malformed, overflowing, unavailable, and interrupted.
6. Verify no user hook or exporter configuration was changed.
7. Review memory, event-loop latency, queue bounds, file descriptors, and shutdown cleanup.

Gate: no release/version bump until all available built-ins pass dogfood and every unavailable
channel is accurately reported.

## Test strategy

### Manifest and loader tests

- frozen values and API-version rejection;
- folder-name validation and canonical runtime name;
- missing/wrong `MANIFEST` diagnostics;
- invalid callback and channel ownership diagnostics;
- relative and nested imports;
- same-named sibling modules in separate plugins;
- no `sys.path` pollution or standard-library shadowing;
- failed-import cleanup and repeat scan;
- disabled plugin never imported;
- local override and alias/binary collisions;
- direct legacy `.py` migration message;
- repeat registry installation, local replacement, and removed-directory rescans;
- broken local isolation and broken shipped fatal policy.

### Shared channel tests

- primary/enrichment ownership and explicit fallback;
- deterministic merge under different completion orders;
- stable ID/revision dedupe;
- bounded queue, payload, parser state, and error text;
- cancellation and partial-construction cleanup;
- source failure isolation and recovery;
- idempotent close;
- no false `progressed` from telemetry;
- history and attachment restricted to the primary source;
- health transitions and bounded diagnostics.

### Hook security and behavior tests

- valid, expired, forged, wrong-participant, wrong-harness, and wrong-scope credentials;
- unknown/disabled event names;
- oversized, malformed, nested, and duplicate payloads;
- ordering, retries, later-event recovery, overflow, and daemon restart;
- native hook timeout does not block launch or the daemon;
- decoder exceptions affect only that channel;
- exact correlation required before enrichment;
- receipt identity semantics unchanged.

### Native OTel tests

- loopback-only binding and authentication;
- HTTP/gRPC support only when declared;
- request/attribute/signal/queue bounds;
- duplicate export and retry idempotence;
- sampled/redacted/partial coverage;
- exact participant and native-record correlation;
- optional dependency absence;
- exporter conflict refusal and explicit fan-out;
- independent shutdown from Theater's outbound observability;
- no semantic reconstruction from metrics alone.

### Built-in parity tests

Parameterize a common suite over every built-in manifest:

- load, metadata, availability, launch, approval, model, and reasoning behavior;
- resume/fork identity and transcript ownership;
- live parsing equals paged history for the same native data;
- stable turns, requests, tools, errors, models, timing, usage, and cost provenance;
- malformed/truncated/rotated/compacted input;
- screen safety and conservative unknown handling;
- same-cwd concurrent participants;
- no imports or constants outside the owning plugin package.

## Orchestration after validation

Implementation uses Theater sessions in isolated worktrees. Shared contracts merge before workers
consume them; workers do not independently evolve the manifest API.

### Wave A — serial contract foundation

One high-reasoning worker owns manifest contracts, callback protocols, compiler, validation, and
synthetic conformance tests. The coordinator reviews this API before any migration begins.

### Wave B — parallel shared mechanics

Use disjoint workers for:

- package discovery/import/install behavior;
- channel contracts, health, and `CompositeSource`;
- normalization and reusable source strategies.

The coordinator resolves shared API changes centrally. Workers receive exact owned paths,
invariants, forbidden scope, fixtures, and required checks.

### Wave C — parallel built-in packages

Migrate the four built-ins against the frozen API in parallel worktrees. All workers start from the
same accepted integration commit. They may edit only their plugin package and harness-specific
tests; shared-contract gaps return to the coordinator.

No harness receives product priority. Merge order is logistical only, and the phase is incomplete
until all four satisfy parity and placement gates.

### Wave D — shared signal transports

Implement hook and native-OTel frameworks by transport, with synthetic plugins proving each common
path. Adapter bindings then land independently inside their owning packages. An adapter integration
cannot modify common transport to branch on its name.

### Review rules

1. Workers use yolo mode in their own worktrees and make no unrelated changes.
2. Prompts include paths, interfaces, invariants, fixtures, tests, and explicit non-goals.
3. Workers return commit/check information only; the coordinator reads every diff and surrounding
   code directly.
4. At most two correction rounds go back to a worker; the coordinator fixes remaining issues.
5. Small cross-cutting corrections stay with the coordinator.
6. Finished workers are terminated and worktrees removed after integration.
7. A final read-only reviewer audits architecture, behavior, security, bounds, dead code, comments,
   and all four built-ins after the coordinator is satisfied.

## Commit boundaries

Keep structural and semantic changes reviewable:

1. characterization fixtures;
2. manifest values and callback contracts;
3. compiler and validation;
4. package loader and migration diagnostics;
5. channel contracts and composite source;
6. shared normalization/source strategies;
7. one structural commit per built-in package;
8. deletion of old layout and architecture guard;
9. generic hook transport;
10. hook manifests/bindings grouped by behavior, not hidden transport changes;
11. generic native-OTel transport;
12. OTel manifests/bindings grouped by behavior;
13. diagnostics and documentation;
14. dead-code cleanup and final validation;
15. version bump only after dogfood.

## Definition of done

- Every plugin is a named directory with one `manifest.py` exporting `MANIFEST`.
- Folder name is the canonical harness name.
- Relative local modules work without `sys.path` modification or cross-plugin collisions.
- Built-in and local plugins traverse the same loader and compiler.
- All built-in-specific production code resides under its own
  `theater/harness/builtin/plugins/<name>/` package.
- `builtin/adapters/` and loose built-in plugin files no longer exist.
- No production module outside a built-in package imports its internals or branches on its name.
- Custom behavior enters only through explicit typed manifest callbacks.
- Common launch, source, hook, OTel, normalization, health, and composition mechanics contain no
  vendor-specific schema knowledge.
- All four built-ins satisfy the shared conformance suite and preserve characterized behavior.
- Hook and OTel failures remain bounded enrichment failures with visible diagnostics.
- Durable observation, job completion, history, and identity remain available without optional
  channels.
- Legacy one-file plugins receive an actionable migration error and are never executed.
- Documentation and `AGENTS.md` describe the package-manifest contract accurately.
- Full tests, coverage, lint, formatting, mypy, Alembic, real tmux, and four-harness dogfood pass.

## Explicit non-goals

- Moving reducer policy into manifests or plugins.
- Replacing Python with a TOML/YAML plugin DSL.
- Eliminating every native decoder function.
- Creating a generic callback bag or unrestricted plugin service locator.
- Making every harness claim identical upstream capabilities.
- Parsing semantic conversation state from tmux screen output.
- Replacing durable history with hooks or telemetry.
- Inferring identity or causality from timestamps, cwd, model names, or prose.
- Capturing private chain-of-thought.
- Building a general-purpose OTel collector.
- Silently modifying global/project hook or exporter configuration.
