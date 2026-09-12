# Native harness wiring beyond Codex

This plan extends the Codex pilot in `harness-runtime-plan.md`. It preserves each harness's
stock terminal UI on the same live session and uses the best supported protocol available.
ACP is an option, not a requirement. This plan describes the intended delivery; a checked-in
adapter is not evidence that its live conformance gates have passed.

## Decisions

- Wiring must preserve working capabilities. Choose the route per operation, retaining legacy
  send, queued followups and interrupt wherever the native path is unsupported or unreliable.
- `native` expresses a preference with fallback; `legacy` opts out. Apply selection to new
  Theater spawns. Existing participants retain their launch policy.
- Fall back before transmission, or after proving an attempted operation was not applied.
  Uncertain delivery remains uncertain until reconciliation; never replay a potentially
  accepted prompt into the pane.
- Use supported stock extensions and public APIs. Do not fork a CLI, replace its prompt/UI,
  import private UI/runtime internals, or create a separate headless conversation.
- Preserve approvals, user plugins and MCP configuration, native subagents, durable transcript
  identity, resume/fork behavior and session isolation. Settings updates are session-local.
- Preserve the current Codex compatibility policy. New adapters use a bounded version range
  and capability/conformance probes rather than an exact-release check alone.
- Use existing Theater control APIs. Rewind, explicit compaction and attachment APIs are outside
  this change.

## Capability map

| Harness | Same-session stock UI path | Native contribution in this delivery | Working controls retained | Boundaries |
| --- | --- | --- | --- | --- |
| Codex | Current app-server connection plus attached stock CLI | Existing control and live-observation integration | Current routing and lifecycle | Compatibility policy unchanged |
| OpenCode | Ordinary TUI with a supported passive TUI extension | Authenticated current status; the existing database source owns history | Legacy send, FIFO followups, interrupt | Public prompt calls bypass unsent UI model/agent/variant selections; abort lacks an exact-turn guard |
| Pi | Ordinary interactive CLI with the bundled supported extension | Exact-session snapshots, lifecycle observation and confirmed thinking updates | Legacy send, FIFO followups, interrupt | Model mutation stays disabled; native send/steer/interrupt await admission and run-correlation proof |
| Claude | Ordinary Claude Code with launch-local command hooks | Correlated tool-lifecycle enrichment | Legacy controls and transcript-derived turns/results/usage | Tool hooks and Stop provide no native prompt receipt or exact Theater job completion |
| Vibe | Ordinary Vibe UI | Existing Unified Store observation | Existing legacy controls | No supported stock-UI remote attachment or live extension handle; safe launch-local hooks have not been established |

The initial source/probe targets are OpenCode `>=1.18.29,<1.19.0`, Pi `>=0.84.4,<0.85.0`,
and Claude `>=2.1.202,<3`. These are compatibility candidates, not a claim that all releases
have passed conformance. The Vibe investigation used 2.25.1. Claude's local checkout contains
documentation and plugins rather than the proprietary runtime; label captured payloads and
synthetic tests accordingly.

## Independent workstreams

All implementation branches start at `003e9c759ae6b0445247ebe4f2f5d0fb3af0ba41`.

| Branch | Ownership |
| --- | --- |
| `feature/opencode-harness-rework` | OpenCode plugin, initial generic frontend host and per-capability control routing, associated focused tests |
| `feature/pi-harness-rework` | Pi extension, plugin runtime and launch overlay, Pi fixtures/tests and `pi-harness-rework-plan.md` |
| `feature/claude-harness-rework` | Claude hook observation, exact-session hook admission, hook-specific contracts/tests and `claude-harness-rework-plan.md` |
| `feature/harness-wiring-integration` | Parent review, shared integration, compatibility activation, final validation and documentation |

Workers continue independently; OpenCode does not coordinate or implement Pi-specific work.
The parent resolves shared integration after reviewing committed artifacts. Keep the original
worktrees and commits available; do not merge to main, push or clean up live workers as part
of implementation.

## Shared integration

The daemon owns the participant-local authenticated connection, endpoint and credentials.
Extensions stay alive when Theater disconnects and reconnect without replacing the native
conversation. Bound frame sizes, notification queues, pending requests and retained receipts.
Reject unauthenticated connections before creating a runtime. A replaced connection must not
close or update its successor.

The shared protocol is NDJSON. Its hello is `type=hello`, `protocol=theater-frontend-v1`,
and the participant token. OpenCode sends only `event` and `snapshot` status notifications;
the shared envelope also accepts `history` for other adapters.
Duplex clients additionally accept `type=request` with a string `id`, `method` and `params`,
and reply with `type=response`, the same `id`, and either `result` or `error`.
Pi exposes `pi.snapshot` and `pi.settings.update`; updates carry `operation_id` and
`native_session_id`. A transport adapter must preserve those identities and deadlines.

The integration composes both peers with the shared host and registers Pi's runtime.
Frontend credentials are independent LIVE channel credentials: public launch descriptors contain
the private token file path, never the token. A synchronous connection callback
must not prevent the host from reading the response that callback awaits. Connection loss must
fail pending requests without replay and preserve the legacy pane controls. OpenCode status
expires after three seconds without a refresh and is revalidated against the trusted identity,
current visible route, connection and latest status after sibling observation awaits.

Control reporting must describe the effective routes. Completion and restart recovery must use
the operation's actual delivery transport, not merely the participant's preferred wiring.
Keep admission presence checks, copy-mode protection for legacy input, queue order and uncertain
delivery handling intact. Native settings require a fresh, exact-session idle snapshot and
extension-side guards; cached idle state is insufficient.
Routes and runtime host kind are pinned in the launch policy. Daemon restart preserves queued
legacy followups that have provably never dispatched, including those on a frontend binding.

Claude hooks require trusted daemon identity at admission and protection against rotation during
correlation or before decoding. A payload's matching session id and transcript filename cannot
establish ownership by themselves. Transcripts remain authoritative for turns and usage.
Compatibility checks must run before adding optional native hook settings. Unsupported versions,
failed probes and absent hooks retain the ordinary launch and receipt hooks. Keep launch planners
pure and execute bounded read-only subprocess probes outside the daemon event loop.

## Acceptance and review

Workers commit their own implementation and run focused checks. Parent review examines actual
diffs and failure paths. After integration, the parent runs the full regression suite, formatting,
lint, typing and any required schema check. Do not modify the production daemon, real settings,
existing participant panes or source checkouts for conformance tests.

Required evidence includes:

- A stock native UI remains on the same live session through connect/disconnect/reconnect.
- Legacy send, FIFO followups and interrupt work with the bridge connected, absent or failed.
- Wrong-session, stale-epoch and reordered events cannot change current identity or manufacture
  idle/completion; duplicate observations do not duplicate tools, usage or results.
- Settings rejection, clamping and native readback are represented accurately. Lost responses
  do not become successful updates, automatic replays or rollbacks of later human choices.
- Resume/fork uses the correct durable store and transcript; user config/extensions and native
  subagent discovery remain intact.
- Unsupported versions and failed installation probes preserve ordinary launch behavior.
- Tests execute the extension behavior, including Pi's fresh context object per event, rather
  than relying only on string checks or Python-side synthetic frames.

A fresh independent Pi reviewer using `mistral/zai-glm-5-3` at `max` reviews the exact integrated
commit in its own worktree. The parent verifies findings and owns corrections and final validation.
Unrun live gates and deferred native controls remain explicit in the final handoff.

## Integrated evidence and remaining live coverage

The parent integrated the independent branches and the latest main fixes, then passed the full
regression suite, Ruff, formatting, typing and Alembic checks. A fresh Pi GLM 5.3 max reviewer
reported no blocking code findings; final deltas receive the same review.

Stock Pi 0.84.4 passed ordinary UI launch, authenticated Unix snapshots, confirmed thinking
readback, host restart, same-session reconnect and disposal. Stock OpenCode 1.18.29 passed an
isolated local streamed-provider test with actual busy/idle events, source-level status enrichment,
reconnect idle snapshot, user-plugin composition, preserved model/auto flags and a fresh fork in
the same database. Non-session snapshots are excluded from session and fork proof.

OpenCode's manual/edits approval interaction, actual MCP tool invocation, and arbitrary human
session/subagent navigation still lack a dedicated live test. Those flows retain the ordinary
launch and controls; synthetic identity/route tests cover stale and foreign observations. Native
OpenCode prompt/steer/interrupt and Pi prompt/steer/interrupt/model mutation remain unavailable
until their public APIs can preserve the required semantics.
