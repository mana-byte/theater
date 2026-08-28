# RC3 plugin rework

Status: superseded by [`rc2v2_plan.md`](rc2v2_plan.md), whose package-manifest and shared-channel
architecture is implemented on `mana/rc3-plugin-rework`.

This file is retained as design history. Its loose-plugin layout and deferred-channel statements
are not current behavior; use `docs/harness-plugins.md` for the supported interface.

## RC3 implementation outcome

The evidence audit in `docs/rc3_upstream_signals.md` informed the later manifest architecture. The
branch now implements:

- all four built-ins as self-contained manifest packages;
- `ParticipantObservationContext` gives shipped observers one typed source-opening boundary while
  retaining signature-based compatibility for existing local plugins;
- duplicated Claude/Codex value and timestamp normalization has one shared implementation;
- OpenCode's launch-local session receipt now uses the existing authenticated generic transcript
  receipt transport and supports exact root-session changes;
- characterization and contract tests pin launch, observation, history, trajectory, compatibility,
  and receipt behavior.

`CompositeSource`, generic hook ingress, and native harness OTel ingestion are shared bounded
frameworks. Shipped manifests declare these channels unavailable where upstream installation,
identity, or fan-out cannot yet meet the safety gates below.

## Goal

Make harness integrations small, composable, testable, and honest about the signals they can
provide. Theater should be able to observe a harness through its durable transcript or database,
native event hooks, native OpenTelemetry, tmux screen state, and process facts without moving
harness-specific policy into the daemon.

RC3 is both a structural refactor and a fidelity pass. Structural moves must preserve current
behaviour. Any intentional behaviour improvement lands afterward, with a fixture and an explicit
commit that names the changed observation.

## Pre-refactor baseline

The public split is sound:

- `Harness` owns launch and resume planning.
- `HarnessObserver` opens a per-participant `Source` and classifies screen state.
- `Source` produces normalized `Batch` values.
- `TranscriptSource` owns file tailing, attachment trust, rotation, and history.
- The daemon alone interprets batches, changes status, completes jobs, writes SQLite, and emits
  canonical trajectory records.

The problem is below that boundary. The four shipped entrypoints currently mix launch arguments,
identity proof, discovery, raw storage access, parsing, turn tracking, trajectory mapping, usage,
screen matching, and native integration setup:

| Built-in | Approximate size | Durable source | Other existing evidence |
|---|---:|---|---|
| Claude Code | 1,884 lines | project JSONL | lifecycle receipts, screen |
| Codex | 2,116 lines | date-sharded rollout JSONL | process/open-file proof, screen |
| OpenCode | 2,683 lines | shared SQLite event/message tables | process-local plugin receipt, screen |
| Vibe | 1,795 lines | messages JSONL + meta JSON | isolated-domain marker, screen |

Repeated mechanics are consequently implemented several times: safe value coercion, stable IDs,
timestamps and durations, bounded parser state, tool/path extraction, detail fields, usage mapping,
history parser setup, and screen-tail matching. Fixes are easy to apply to one harness and omit from
the others.

## Non-negotiable boundaries

1. The daemon remains the only state-policy owner and SQLite writer. A plugin reports facts; it
   never completes jobs, changes participants, sends input, or writes Theater state.
2. No core module branches on `claude`, `codex`, `opencode`, or `vibe`. Native field names, marker
   strings, filesystem layouts, commands, and schema knowledge stay inside that adapter.
3. `pane_in_mode` remains the only human-presence signal. Screen classification may describe
   prompt/approval/trust state but may not authorize input.
4. Existing local one-file plugins remain valid. `Harness`, `HarnessObserver`, `Source`, `Batch`,
   and the compatibility facades remain supported.
5. The plugin loader still loads one `*.py` entrypoint with `HARNESS = ...`. Built-ins may use
   packages behind that entrypoint; local plugins may remain one file or use existing `_helper.py`
   support.
6. Missing native data remains missing. Do not infer model, timing, failure, or causality from
   timestamps or prose. Cost remains the exception already defined by the pricing estimator and
   must carry estimated provenance.
7. History remains bounded in régie and independently pageable. Hook or OTel buffers never become
   an unbounded transcript substitute.
8. No signal channel may block the daemon event loop. Polling, queues, parsing state, and payloads
   stay bounded.
9. Plugin-native constants stay with that plugin. Only cross-harness limits/defaults belong in
   `theater/constants/`; user-controlled values belong in `theater/config/`.
10. Inline comments stay one line. Longer rationale belongs in module docstrings or this document.

## Design: capabilities, channels, and authority

Do not replace the existing `Source` seam. Generalize behind it.

### Signal classes

| Channel | Strength | Weakness | Intended authority |
|---|---|---|---|
| Transcript / native database | durable, replayable, richest content | delayed, mutable formats, discovery can be ambiguous | semantic events, history, turn IDs, tools, durable usage |
| Native event hook | low latency, exact lifecycle point, can carry launch token | best-effort, version/config dependent, not replayable | exact identity and live lifecycle enrichment |
| Native OTel | precise timing, model/tool/usage attributes, trace IDs | optional, batched, sampled/redacted, exporter may already belong to user | enrichment only in RC3; never sole completion source |
| tmux screen | works when structured output is silent | rendered and fragile | prompt/approval/trust display hints only |
| Process facts | exact for a live process on supported systems | unavailable after death and weaker for adopted shells | liveness and transcript correlation only |

These channels are complementary, not interchangeable. RC3 must not create one global precedence
number and pretend every field follows it. Authority is per fact:

| Fact | Preferred source | Fallback |
|---|---|---|
| pane/process alive | tmux/process | none |
| participant/session identity | launch token, signed receipt, process proof | heuristic candidate requiring operator bind |
| transcript attachment and history | transcript/database | none |
| user/assistant/tool content | transcript/database | hook only when its schema contains the canonical content |
| turn boundary | explicit durable record | exact native lifecycle hook; existing quiet-time rescue remains last resort |
| request/model/tool timing | native record or exact native OTel span | observed timing, clearly labelled |
| usage | native per-request record | native cumulative delta, then shared cost/token estimation rules |
| approval/trust/prompt | screen classifier | unknown |
| Theater MCP classification | daemon canonical projection | never plugin-specific |

No cross-channel merge may use timestamp proximity as identity. Enrichment requires a stable native
request, message, call, span, or turn key. When no exact join exists, keep separate facts and expose
the coverage gap.

### Contract additions

Add only data and protocols to `theater/harness/contracts/`:

- `context.py`: frozen `ParticipantObservationContext`, replacing the growing list of optional
  `open_source_for` parameters for shipped adapters. It carries participant identity, cwd, session
  provenance, known location/domain, creation floor, and live pane process facts.
- `channels.py`: `ChannelKind`, `SignalKind`, immutable channel declarations, and health/error
  values. These describe what a channel claims; they do not perform I/O.
- Optional source-factory/decoder protocols needed by the composition layer.

Add an optional context-based observer method with a default compatibility path. Existing local
plugins continue through `open_source` / `open_source_for`; shipped adapters migrate to the typed
context. Shipped adapters stop depending on signature introspection, but the legacy compatibility
dispatcher remains throughout RC3 so old plugins still receive exactly the arguments they declare.

`contracts/` must not become a `utils.py` dumping ground. Shared code belongs there only when it
defines or validates the public harness boundary. Reusable implementation mechanics live in named
packages beside it:

```text
theater/harness/
├── contracts/                 pure values, ABCs, protocols
│   ├── channels.py
│   ├── context.py
│   ├── events.py
│   ├── harness.py
│   ├── launch.py
│   ├── observation.py
│   ├── source.py
│   └── trajectory.py
├── channels/                  reusable multi-signal mechanics
│   ├── composite.py           ownership, fallback, deterministic merge
│   ├── inbox.py               bounded per-participant live-signal queues
│   ├── hooks.py               authenticated hook ingress/source
│   └── otel.py                optional native-telemetry ingress/source
├── normalization/             pure cross-harness parsing helpers
│   ├── values.py              bounded IDs/text, finite numbers, timestamps
│   ├── timing.py              source/observed timing construction
│   ├── usage.py               token/usage conversion and provenance
│   ├── tools.py               structured arguments and path declarations
│   └── facts.py               Event/TrajectoryFact builders
├── screen/
│   └── classifier.py          exact/tail marker matching with explicit precedence
├── transcript/                existing attachment, history, observer, source
└── builtin/
    ├── adapters/
    │   ├── claude/
    │   ├── codex/
    │   ├── opencode/
    │   └── vibe/
    └── plugins/               thin scanner entrypoints, one per harness
```

Do not create all normalization modules speculatively. Extract a helper only when at least two
adapters share the same semantics, not merely similar-looking syntax.

### Source composition

`CompositeSource` remains a `Source`, so daemon observation policy does not change. It owns:

- one primary durable source for attachment, history, control events, and default status;
- zero or more bounded enrichment sources;
- explicit ownership per `SignalKind`;
- fallback order declared once at construction;
- independent channel health and retry/backoff;
- deterministic close order and partial-construction cleanup.

Rules:

- Only the primary source may return `Attachment`, identity-loss evidence, history, or collision
  domain in the first implementation.
- An enrichment source may not emit job-completing control events until its exact turn semantics
  have a dedicated contract test.
- Overlapping ownership is rejected at construction unless one binding is explicitly a fallback.
- A failed enrichment channel does not make the participant unobservable while the durable source
  is healthy.
- Child reads may run concurrently behind bounded timeouts; their results merge in declared source
  order so latency does not make output nondeterministic.
- Records use stable native IDs and revisions for deduplication.
- `progressed` retains its current meaning and is not manufactured by telemetry heartbeats.
- Closing is idempotent, closes every opened child, and preserves the first error only for logging.

### Hook ingress

Generalize the existing receipt mechanism instead of adding one daemon RPC per harness:

1. The launch plan creates a private per-participant token.
2. A native hook invokes a generic, bounded `theater harness-event` command and writes JSON on
   stdin. The command forwards to the daemon; it never opens SQLite.
3. The daemon validates participant, harness, token, channel, and payload size before enqueueing.
4. The plugin decoder owns every native key and maps the opaque payload to contract values.
5. A bounded inbox supplies the participant's hook source. Overflow is observable and degrades
   coverage; it never blocks the hook process.

Keep `claude-receipt` as a payload-transparent compatibility alias of the generic command. Hook
setup must be launch-local and must not rewrite the user's global configuration.

### Native OTel ingress

Theater's existing `observability/` package exports Theater's own logs, metrics, and spans. Native
harness telemetry is a different direction and must not be mixed into that package. The daemon may
host a small generic ingress adapter under `harness/channels/otel.py`; every native signal name and
attribute mapping remains exclusively in its built-in adapter.

Before implementation, verify each installed harness's emitted signal names, attributes, protocol,
batching, redaction, and custom-resource support against `../coding_clis` and a captured local run.
The receiver is built only for protocols actually needed.

Safety requirements:

- Bind loopback only and authenticate every spawned participant with launch-local material.
- Inject an exact participant resource attribute or header; never correlate by cwd/model/time.
- Bound request bytes, attribute counts/lengths, queue size, and retention.
- Accept duplicate/retried exports idempotently.
- Never redirect or silently steal an exporter already configured by the user. Either provide
  explicit opt-in fan-out or leave native OTel untouched.
- Sampling/redaction must surface as partial coverage. Metrics alone cannot reconstruct trajectory.
- OTel remains enrichment in RC3. Transcript/database or exact hook records remain the durable
  completion source.
- Missing optional OTel dependencies disables this channel with an actionable diagnostic, without
  disabling the harness.

## Built-in adapter layout

Each `builtin/plugins/<name>.py` becomes a thin, stable entrypoint. It constructs `HARNESS` and
re-exports currently imported compatibility names. Implementation lives under
`builtin/adapters/<name>/`; core code never imports those packages.

Use the same responsibility names where the harness has that responsibility, without forcing
empty symmetry:

- `launch.py`: argv/env/config files, approval/model/reasoning, resume overlay.
- `identity.py`: transcript/session discovery, receipts, collision domain, process proof.
- `source.py`: per-participant mutable cursor/connection and history access.
- `parser.py`: native record decoding and normalized control events.
- `trajectory.py`: trajectory facts, request/tool correlation, usage/timing details.
- `screen.py`: prompt/working/approval/trust classification.
- `constants.py`: only that harness's native keys, markers, filenames, and bounded parser limits.
- `__init__.py`: narrow adapter exports.

Expected harness-specific shape:

| Harness | Keep as durable truth | Candidate enrichment | Important migration risk |
|---|---|---|---|
| Claude Code | JSONL + exact lifecycle receipt | richer lifecycle/tool hooks; native OTel after capture | compaction/rotation, duplicate message blocks, sidechains |
| Codex | rollout JSONL + live process proof | native OTel after capture | same-cwd siblings, resume/fork identity, open-file portability |
| OpenCode | SQLite event/message/part state + plugin receipt | native plugin event feed if it beats DB polling | mutable rows, root/sub-session separation, connection lifecycle |
| Vibe | messages JSONL + meta usage + signed domain | native OTel or app event feed after capture | missing transcript timestamps, rotation, cumulative usage deltas |

Capability declarations must describe tested output, not theoretical upstream support. A harness
version that exposes OTel does not make a feature `SUPPORTED` until Theater's decoder has fixtures
and the live path is enabled. Runtime channel health and static capability are separate.

## Implementation phases

### Phase 0 — freeze behaviour and verify upstream signals

1. Get the branch to a clean, green baseline. Land pending Claude/Vibe fixes separately so a
   structural commit cannot hide behaviour changes.
2. Record current normalized `Event`, `TrajectoryFact`, history, screen, launch, identity, and
   capability outputs for all four built-ins using compact golden fixtures.
3. Capture one real native session per harness covering: response, successful tool, failed tool,
   usage, resume/fork, approval prompt, and daemon restart.
4. Inspect current upstream sources/configuration and run local probes for hooks and OTel. Record
   exact versions, schemas, opt-in flags, exporter conflicts, and missing fields.
5. Decide the smallest useful RC3 channel set. Unsupported or unsafe integrations stay documented
   gaps; no speculative decoder ships.

Gate: existing full suite green; fixture output reviewed; no native channel marked supported from
documentation alone.

### Phase 1 — contracts and reusable mechanics

1. Add typed participant observation context with a backward-compatible dispatch path.
2. Add channel declarations and contract validation.
3. Add `CompositeSource` with primary/enrichment ownership, bounded errors, close semantics, and
   deterministic merge tests.
4. Extract only already-duplicated normalization and screen helpers.
5. Re-export public additions from `theater.harness`; keep old facade imports.
6. Add a parameterized source/observer conformance suite for third-party and built-in adapters.

Gate: no built-in output changes; old single-file plugin fixture still loads and observes; no
daemon module knows a built-in harness name.

### Phase 2 — split built-ins without changing behaviour

Migrate one adapter at a time. Move code, preserve names and outputs, and compare every golden
fixture before and after.

1. Claude: launch, identity/receipts, source/history, parser, trajectory, screen.
2. Codex: launch, discovery/process proof, source/history, parser, trajectory, screen.
3. OpenCode: launch/plugin receipt, read-only store queries, source/history, parser/trajectory,
   screen.
4. Vibe: launch/isolation, discovery/source, meta usage, parser/trajectory, screen.
5. Replace each old plugin file with wiring and compatibility re-exports.
6. Delete superseded functions, comments, imports, and tests of private implementation placement.

Gate per adapter: focused tests, golden parity, ruff, format, mypy, then full harness/trajectory
suite. No mixed structural and semantic commit.

### Phase 3 — generic hook channel

1. Implement authenticated bounded ingress and inbox source.
2. Migrate current Claude transcript receipt and OpenCode process receipt through shared transport
   without changing their trust semantics.
3. Add richer hook decoders only where Phase 0 proved stable schemas.
4. Make hook loss/overflow visible as channel health, with durable fallback intact.
5. Test forged tokens, wrong harness/participant, oversized payloads, retries, out-of-order input,
   daemon restart, and hook timeout.

Gate: disabling hooks reproduces Phase 2 output; hook failure cannot block launch or observation.

### Phase 4 — native OTel channel

1. Implement the minimal verified local receiver and lifecycle wiring.
2. Add exact per-participant correlation and bounded queues.
3. Add per-harness decoders in their own packages; shared code only decodes protocol envelopes and
   validates bounds.
4. Join OTel enrichment to durable facts only by stable native keys.
5. Surface partial/sampled/redacted coverage and exporter conflicts.
6. Verify shutdown, backpressure, malformed exports, duplicates, and optional-dependency absence.

Gate: native OTel off yields byte-for-byte normalized parity; on adds information without duplicate
events, requests, tools, usage, or cost. Existing Theater OTLP export still works independently.

### Phase 5 — fidelity fixes by harness

Use the new structure to close only evidenced gaps:

- reliable turn/request IDs and one boundary per turn;
- model/provider attribution;
- request, first-token, tool, and observation timing with honest provenance;
- reported usage plus shared estimation fallback;
- paired tool inputs/results and typed failures;
- transcript/database history parity with live parsing;
- resume/fork/compaction identity continuity;
- accurate capability and channel-health reporting.

Every fix begins with a native fixture and lands in the relevant adapter package. If the same fix
is needed twice, extract the semantic primitive first and parameterize it; do not copy it four
times.

### Phase 6 — cleanup, documentation, and dogfood

1. Remove compatibility internals that were never public; retain documented facade imports.
2. Update `docs/harness-plugins.md`, `docs/architecture.md`, `AGENTS.md`, and the module map.
3. Add a concise channel/capability diagnostic to `theater harnesses` so users can see active,
   unavailable, unsupported, and degraded sources.
4. Run the full suite, coverage, lint, format, mypy, Alembic check, and real tmux tests.
5. Dogfood all four built-ins concurrently, including same-cwd sessions and daemon/régie restarts.
6. Bump RC3 only after the dogfood matrix passes; versioning is not part of refactor commits.

## Orchestration plan

Use `mana/rc3-plugin-rework` as the integration branch. Every implementation worker gets its own
worktree and a branch cut from the latest accepted integration commit. Workers never share a
worktree.

### Wave A — serial foundation

One high-reasoning worker owns only contracts, channel composition, normalization primitives, and
their tests. No built-in adapter edits. The coordinator reviews API shape, compatibility, and
failure semantics before any adapter branch starts.

### Wave B — parallel structural migrations

After Wave A merges, run adapter workers in parallel with disjoint ownership:

- Claude worker: `builtin/adapters/claude/`, `builtin/plugins/claude.py`, Claude tests.
- Codex worker: `builtin/adapters/codex/`, `builtin/plugins/codex.py`, Codex tests.
- OpenCode worker: `builtin/adapters/opencode/`, `builtin/plugins/opencode.py`, OpenCode tests.
- Vibe worker: `builtin/adapters/vibe/`, `builtin/plugins/vibe.py`, Vibe tests.

With four total slots, use two or three workers at once and retain one slot for coordination and
validation. Workers may not edit shared contracts after Wave A; discovered contract gaps return to
the coordinator as a small serial patch before work resumes.

### Wave C — parallel channel integrations

Split by transport, not by random file ranges:

- hook ingress worker owns generic ingress/inbox and security tests;
- OTel ingress worker owns protocol receiver, lifecycle, bounds, and optional dependencies;
- harness workers own only their adapter decoders/configuration and fixtures.

Merge generic transport first, then adapter integrations one at a time. Run deduplication and
fallback tests after every merge so two individually correct channels cannot combine incorrectly.

### Review protocol

For every worker:

1. Prompt includes exact owned paths, invariants, baseline fixture, required tests, forbidden scope,
   and expected commit boundary.
2. Worker returns commit hash and checks only; no long report.
3. Coordinator reads the complete diff and surrounding code, runs focused checks independently,
   and asks for corrections when needed.
4. After at most three correction rounds, coordinator makes remaining changes directly.
5. Only reviewed commits are cherry-picked or fast-forwarded onto the integration branch.
6. Finished worktrees are removed after integration; no unrelated changes are committed.

Final review is cross-cutting: dependency direction, dead code, verbose comments, compatibility,
boundedness, cancellation, shutdown, identity safety, and all four harness parity. A second reviewer
gets a read-only pass after the coordinator is satisfied.

## Test strategy

Prefer a small parameterized conformance suite over four copies of every test.

### Shared contract tests

- legacy local plugin load and compatibility dispatch;
- context-based built-in source construction;
- source attachment staging and trust;
- composite ownership conflicts and fallback;
- deterministic ordering, deduplication, revisions, and close behavior;
- bounded payloads, queues, history pages, and parser state;
- cancellation and partial-construction cleanup;
- channel failure isolation and health reporting.

### Per-harness fixture tests

- launch/resume argv, environment, and generated files;
- exact identity plus contested same-cwd sessions;
- live parse equals paged-history parse for the same native records;
- one control turn boundary per native turn;
- stable IDs across restart and pagination;
- tool call/result pairing, nested calls, failures, and Theater MCP classification;
- model, timing, usage, cost, and provenance;
- rotation, compaction, fork/resume, truncation, and malformed trailing records;
- screen prompt/working/approval/trust classification with false-negative bias.

### Integration and performance tests

- daemon restart while each durable and live channel is active;
- one channel unavailable while the participant remains observable;
- multiple agents of the same harness in one cwd;
- hook/OTel retry and duplicate delivery;
- no event-loop I/O stalls from large transcript scans or SQLite queries;
- bounded memory under long sessions and telemetry bursts;
- no duplicate bus, trajectory, usage, or cost records when channels overlap;
- real tmux spawn, send, stage, resume/fork, kill, and detach under `nix develop`.

## Commit boundaries

Keep history reviewable:

1. characterization fixtures;
2. observation context and channel contracts;
3. shared source/normalization mechanics;
4. one structural commit per built-in adapter;
5. generic hook transport;
6. one hook integration per harness;
7. generic OTel transport;
8. one OTel integration per harness;
9. fidelity fixes, one behavior per commit;
10. dead-code/docs cleanup;
11. version bump after dogfood.

## Definition of done

- Shipped plugin entrypoints contain wiring and compatibility exports, not implementations.
- Every implementation module has one clear responsibility; no replacement monolith appears.
- Core contains no native harness schema, marker, path, or command knowledge.
- Shared semantic behavior has one tested implementation and is reused by at least two adapters.
- All four adapters preserve launch, identity, control events, history, screen safety, and trajectory
  behavior before intentional fidelity commits are applied.
- Channel ownership and fallback are deterministic, bounded, and visible to users.
- Native hooks and OTel cannot corrupt identity, duplicate accounting, steal a user's exporter, or
  become required for basic observation.
- Local one-file plugins continue to load unchanged.
- Full tests, coverage, lint, format, mypy, Alembic check, and real tmux validation pass.
- Concurrent real sessions for Claude, Codex, OpenCode, and Vibe survive daemon and régie restarts.

## Explicit non-goals

- Moving reducer policy into plugins.
- Replacing durable history with hooks or telemetry.
- Parsing conversation content from tmux screen captures.
- Inferring private chain-of-thought.
- Supporting an upstream signal merely because its source repository contains related code.
- Breaking third-party plugins to make built-ins prettier.
- Building a general-purpose OpenTelemetry collector.
