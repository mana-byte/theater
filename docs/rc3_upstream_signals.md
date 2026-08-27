# RC3 upstream signal audit

Audited 2026-08-27 against local source snapshots. This is source evidence, not a captured
session fixture: no capability is supported until its exact emitted payload has a live fixture and
a tested live path. Durable transcript/database data remains the completion and history authority.

## Scope and versions

A decision is `implement now` only when RC3 can safely migrate an upstream signal. Rich hooks/OTel
additionally need exact native identity, bounded launch-local setup, stable schema, and no exporter
theft. Paths below are exact inspected paths.

| Harness | Upstream source | Installed CLI | Durable authority already consumed by Theater |
|---|---|---|---|
| Claude Code | `/Users/manaiki.laut/Desktop/coding_clis/claude-code` @ `1f6015b5d578adf79c8527443328a216d6b6a3f1`, `v2.1.232` | `2.1.220` | project JSONL |
| Codex | `/Users/manaiki.laut/Desktop/coding_clis/codex` @ `fdbab67c669a3176b13d08ab49493f30a806d2ba` | `0.146.0` | rollout JSONL plus live open-file proof |
| OpenCode | `/Users/manaiki.laut/Desktop/coding_clis/opencode` @ `e23586af2623f1bc2e8e6965d2d7acf7bd03d5c3`, package `1.18.18` | `1.18.11` | shared SQLite event/message/part tables |
| Mistral Vibe | `/Users/manaiki.laut/Desktop/coding_clis/mistral-vibe` @ `a84be0391bf93e93a4025a5e08e8032ecb587123`, `2.24.3` | `2.24.3` | `messages.jsonl` plus `meta.json` |

### Existing Theater receipt transport

The generic authenticated transcript-identity receipt transport is already shipped, not RC3 work:
`theater/daemon/rpc/transcripts.py:_transcript_receipt` owns `transcript.receipt` token
authentication, observer validation, conflict checks, admission, persistence, audit, and token
renewal. `claude.receipt` is its compatibility RPC alias. The hidden
`theater/cli/commands/identity.py:cmd_transcript_receipt` forwards opaque stdin JSON using a private
token file; `cmd_claude_receipt` retains the legacy field-extracting alias.

`theater/daemon/spawning/planning.py:validate_receipt_plan`, `record_launch_identity`, and
`write_plan_files` mint, validate, persist, and privately write the token. The end-to-end generic
transport is covered by `tests/test_generic_receipts.py`. The generic live lifecycle-event inbox is
distinct: it retains bounded non-durable hook facts after identity is known. Its common transport
now exists and has a synthetic fixture, but every shipped harness declares it unavailable until its
native integration satisfies the evidence and safety gates below. Do not conflate it with
`transcript.receipt`.

## Claude Code

The checked-out Claude repository is documentation/plugins, not the CLI runtime: no upstream
transcript writer or OTel exporter implementation is present. Theater's existing durable contract is
`theater/harness/builtin/adapters/claude/observer.py:ClaudeCodeObserver`: it reads
`~/.claude/projects/<slugged-cwd>/<sessionId>.jsonl`. Its
`validate_transcript_receipt` requires `session_id`/ `sessionId` and
`transcript_path`/ `transcriptPath`, validates JSONL location/stem/cwd and checks transcript
evidence. This durable JSONL remains completion authority.

`ClaudeCodeHarness.plan_launch` creates a UUID, passes `--session-id`, and builds generated
`--settings` receipt hooks through `_claude_receipt_settings` and
`_receipt_hook_command`. It already uses the shipped generic receipt flow through the retained
`claude-receipt` compatibility command and its per-participant token. Installed `claude --help` exposes
`--settings <file-or-json>` and session-only `--plugin-dir`; either avoids global config writes.

### Hook contract

`plugins/plugin-dev/skills/hook-development/SKILL.md` is the checked-in upstream contract. Plugin
hooks use `hooks/hooks.json` with a `hooks` wrapper; settings use direct event keys. It documents
`PreToolUse`, `PostToolUse`, `Stop`, `SubagentStop`, `SessionStart`, `SessionEnd`,
`UserPromptSubmit`, `PreCompact`, and `Notification`. Stdin common fields are `session_id`,
`transcript_path`, `cwd`, `permission_mode`, and `hook_event_name`; tool hooks add
`tool_name`, `tool_input`, and `tool_result`; Stop hooks add `reason`. Exit 0 is success, 2
blocking, all others non-blocking. It recommends timeouts but specifies no delivery retry/order.

That proves the existing receipt's identity fields once Theater verifies them against JSONL. It does
not prove richer decoding: no captured installed payload, documented stable turn key, or retry
guarantee exists. Do not rewrite user project/global settings.

### Native OTel

Only `CHANGELOG.md` and gateway examples are available. They name
`OTEL_EXPORTER_OTLP_ENDPOINT`, signal-specific `OTEL_LOGS_EXPORTER`,
`OTEL_METRICS_EXPORTER`, and `OTEL_TRACES_EXPORTER`, OTLP HTTP/mTLS, and log events
`user_prompt`, `api_request`, `tool_result`, `tool_decision`, and
`claude_code.assistant_response`. Recent correlation claims are `message.uuid`,
`client_request_id`, `tool_source`, and `tool_use_id`. Content is gated/redacted through
`OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_ASSISTANT_RESPONSES`, `OTEL_LOG_TOOL_DETAILS`,
`OTEL_LOG_RAW_API_BODIES`, and `CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH`.

The same changelog says managed endpoint settings govern lower-scope signal endpoints. No source
shows additive fan-out, launch-local resource/header injection, batching/retry/sampling semantics,
or a stable turn/request join. Treat all OTel claims as unverified and never repoint user exporters.

## Codex

`theater/harness/builtin/adapters/codex/observer.py:CodexObserver` consumes
`~/.codex/sessions/YYYY/MM/DD/rollout-<local-ISO>-<session_id>.jsonl`.
`session_meta.payload.session_id` names the rollout; an originating pane process holding the file
open is exact live proof. Same-cwd discovery is intentionally heuristic.

Upstream `codex-rs/rollout-trace/README.md`, `src/raw_event.rs:RawTraceEvent`, and
`src/model/session.rs:CodexTurn` describe a separate optional diagnostic bundle. It has ordered
`seq`, `rollout_id`, `thread_id`, `codex_turn_id`, `inference_call_id`, and
`tool_call_id`, enabled through `CODEX_ROLLOUT_TRACE_ROOT`. It is not the normal rollout JSONL
or an RC3 attachment channel without a captured invocation and lifecycle proof.

### Hooks

`codex-rs/hooks/src/schema.rs:HookEventNameWire` defines `PreToolUse`,
`PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `SessionStart`,
`UserPromptSubmit`, `SubagentStart`, `SubagentStop`, and `Stop`. The generated JSON Schemas
under `codex-rs/hooks/schema/generated/` are exact wire evidence:

- `session-start.command.input.schema.json` requires `session_id`, `transcript_path`, `cwd`,
  `model`, `permission_mode`, and source `startup|resume|clear|compact`.
- `post-tool-use.command.input.schema.json` requires `session_id`, `turn_id`, `tool_use_id`,
  `tool_name`, `tool_input`, and `tool_response`; `agent_id`/ `agent_type` are optional.
  The producer is `schema.rs:PostToolUseCommandInput`.

`hooks/src/engine/command_runner.rs:CommandHookRuntime` limits asynchronous hooks to eight,
applies handler timeouts, kills on drop, and uses `try_send` for completion. Dispatcher selection
is declared display order. This is bounded best-effort execution, not durable/retried delivery.
Installed Codex exposes TOML `-c/--config key=value` and
`--dangerously-bypass-hook-trust`, but this audit did not prove a full launch-only hook-array
encoding, trust behavior on installed 0.146.0, or a live payload. Do not persist config or bypass
trust.

### Native OTel

`codex-rs/config/src/types.rs:OtelConfigToml` has independent `exporter`,
`trace_exporter`, `metrics_exporter`, `span_attributes`, and W3C `tracestate`.
`OtelExporterKind` supports `none`, `statsig`, `otlp-http`, and `otlp-grpc`; HTTP has
JSON/binary protocol, headers, and optional TLS. `core/src/config/otel.rs:resolve_config` defaults
log/trace to none and metrics to Statsig.

`codex-rs/otel/src/provider.rs` creates separate resources and batch log/span exporters and
applies configured span attributes to every span. `otel/src/events/session_telemetry.rs` has
`SessionTelemetry` with conversation ID, model, slug, account metadata, originator, terminal type,
and session source. Per-launch `-c otel.span_attributes.*=...` is a candidate token, but choosing
Theater's `otel.exporter`/ `trace_exporter` replaces a user destination; no fan-out, sampling,
redaction, or rollout-ID mapping is proven. Defer OTel.

## OpenCode

`theater/harness/builtin/adapters/opencode/source.py:OpenCodeSource` opens the shared database
read-only.
It consumes `event(aggregate_id, seq, type, data)` as a monotonic live feed and `message`/`part`
for history, excluding child sessions via `parent_id IS NULL`. Upstream confirms the durable shape
in `packages/core/src/event/sql.ts:EventTable` and
`packages/core/src/session/sql.ts:SessionTable`, `MessageTable`, and `PartTable`.
`EventTable` has the unique `(aggregate_id, seq)` index. The new v2
`packages/schema/src/event.ts:Event.define` supports durable aggregate/sequence/version facts, but
does not promise legacy SQLite spellings consumed by Theater.

### Plugin receipt and richer events

RC3 migrated the OpenCode receipt to the shared authenticated transport.
`OpenCodeHarness.plan_launch` writes a generated config and `TheaterSessionReceipt` plugin,
launching with `OPENCODE_CONFIG=<generated path>`. The plugin invokes Theater's shipped
`transcript-receipt` command with the core-owned token file. It observes strict child exit status,
retries a bounded burst, then retries again on later events until delivery succeeds. Upstream
`packages/opencode/src/config/config.ts` merges `OPENCODE_CONFIG` after global config;
`packages/web/src/content/docs/plugins.mdx` documents configured file plugins.
`packages/sdk/js/src/gen/types.gen.ts:EventSessionCreated` and
`packages/schema/src/v1/session.ts:SessionInfo` prove the receipt's
`event.properties.info.id` and `parentID` fields. The receipt ignores children and submits the exact
root session ID; the source waits for that database row before committing the attachment. A later
root receipt can move the same live process to a new session without cwd fallback.

The documented v1 `packages/plugin/src/index.ts:Hooks` API has an async `event` callback plus
typed `chat.message`, `tool.execute.before`, and `tool.execute.after` callbacks. It documents
session, message/part, permission, and tool event coverage. But
`packages/opencode/src/plugin/index.ts` calls each `hook.event` with `void`, so Theater's generated
callback starts receipt delivery without awaiting it. Existing atomic idempotent receipt is safe;
richer event ingress still needs captured order/loss semantics and schema fixtures first.

### Native OTel

`packages/core/src/observability/otlp.ts` enables OTLP only with
`OTEL_EXPORTER_OTLP_ENDPOINT`, parses `OTEL_EXPORTER_OTLP_HEADERS`, sends logs to
`/v1/logs`, and creates an HTTP trace `BatchSpanProcessor` for `/v1/traces`. Resources include
`service.name=opencode`, service version, deployment environment, `opencode.client`,
`opencode.run`, and `service.instance.id`; user `OTEL_RESOURCE_ATTRIBUTES` are merged.
`opencode.run` is a process run ID, not a Theater participant ID.

One endpoint is configured; source shows no additive fan-out, stable session/turn/tool span contract,
sampling/retry/redaction policy, or safe per-participant header/resource injection. Do not repoint
the endpoint; defer native OTel.

## Mistral Vibe

`theater/harness/builtin/adapters/vibe/observer.py:VibeObserver` reads
`~/.vibe/logs/session/session_*/messages.jsonl` and `meta.json` (authoritative session ID, cwd,
child sessions, cumulative usage). Theater's signed isolated
`VIBE_SESSION_LOGGING__SAVE_DIR` domain is exact attachment evidence. Vibe transcript records lack
timestamps; Theater correctly labels observed timing rather than inventing native time. Upstream
`vibe/core/session/session_logger.py` and `session_index.py` are storage entry points; their tests
create `messages.jsonl` plus `meta.json` with `session_id`, but are not a full live fixture.

### Hooks

`vibe/core/hooks/models.py` declares JSON-stdin `post_agent`, `pre_tool`, and `post_tool`.
All carry `session_id`, `transcript_path`, `cwd`, and optional `parent_session_id`; tool
hooks add tool name/call ID/input, while post-tool adds status, output/error, and `duration_ms`.
`HooksManager.run` serially executes matching hooks.
`HookExecutor` caps each stdout/stderr stream at 1 MiB, applies a 60-second default timeout, kills
the process tree, and has no retry. The built-in Hooks documentation says post-agent runs after a
turn and failures are fail-open unless a tool hook is strict.

This is not launch-local. `vibe/core/config/harness_files/_harness_manager.py:hook_files` reads
only trusted `<project>/.vibe/hooks.toml` and `$VIBE_HOME/hooks.toml`.
`VIBE_HOME` relocates all user state (credentials/config/logs/tools), not a hook overlay.
The environment layer accepts schema `VIBE_*` values but cannot inject an ephemeral hook file.
Do not create project hooks or replace Vibe home; defer richer hooks.

### Native OTel

`vibe/core/config/vibe_schema.py` exposes `enable_otel`, `otel_endpoint`, and
`otel_redaction`. `vibe/core/tracing.py:setup_tracing` requires both telemetry and OTel,
exports OTLP/HTTP `/v1/traces`, makes a batch processor, and applies default/strict/none redaction.
It emits `invoke_agent`, `chat`, `execute_tool`, and hook spans. `agent_span` sets
`gen_ai.conversation.id` from session ID; `tool_span` sets tool call ID/arguments; and
`model_call_span` records provider/model, conversation ID, request message ID, usage, response ID,
and finish reasons.

However, `setup_tracing` unconditionally calls `trace.set_tracer_provider(provider)` and installs
one exporter. It does not merge user resource attributes or exporters; source shows no fan-out or
existing-provider protection. Native Vibe OTel is unsupported for RC3 ingress because it violates
the no-exporter-theft condition.

## Boundary and strict recommendation

Theater's existing `observability/` package is outbound-only: Theater exports its own daemon/CLI/
régie logs, metrics, and spans. Inbound harness OTel is a distinct optional harness channel and must
not configure or reuse Theater's provider. Loss, sampling, redaction, retries, duplicates, and queue
overflow are channel health, never completion authority.

| Work item | Decision | Evidence-bound reason |
|---|---|---|
| Generic transcript receipt transport | already implemented; retain/test | `transcript.receipt`, token lifecycle, and generic end-to-end tests already exist; no new transport implementation. |
| Claude receipt migration | already implemented; retain compatibility alias | Claude already reaches the generic transport through `claude-receipt`; retain the legacy CLI/RPC aliases. |
| OpenCode receipt migration | implemented in RC3 | Generic receipt with bounded retry bursts and later-event self-healing. |
| Generic richer hook/event inbox | framework implemented | Synthetic transport proves authentication, bounds, dedupe, health, and lifecycle; no shipped harness claims native support yet. |
| Claude richer hooks | defer | Documentation-only payloads; no captured installed payload, turn key, or retry contract. |
| Codex richer hooks | defer | Exact schemas and bounds exist, but launch-only config/trust and installed-version capture are unverified. |
| OpenCode richer hooks | defer | Event callbacks are fire-and-forget with no retry; capture order/loss and decoder semantics first. |
| Vibe richer hooks | defer | Stable payload/bounds exist, but only global/project hook files; no bounded launch-local setup. |
| Claude native OTel | defer | Changelog-only evidence; emitted schema, fan-out, and launch correlation are unverified. |
| Codex native OTel | defer | Configurable attributes/exporters exist, but endpoint selection replaces rather than fans out. |
| OpenCode native OTel | defer | OTLP source exists, but one endpoint and no stable session/turn contract or fan-out evidence. |
| Vibe native OTel | unsupported | One global provider/exporter is installed; cannot satisfy no-exporter-theft in RC3. |

The implemented receipt row is an identity improvement, not a new trajectory capability. Before
any native OTel receiver, capture actual installed harness sessions with an exact participant token
and an independently configured user exporter present.
