# Claude harness rework

## Outcome

Add supported native hook observation to the existing stock Claude integration while preserving
legacy send, followups, interruption, durable transcripts, native subagents and launch/resume
semantics. This is an independent branch; it does not depend on the OpenCode implementation.

Claude channels currently have no reliable admission receipt or exact turn correlation. The inbox
socket has no sufficiently published control contract. Do not use either for native prompt
delivery or claim they replace working legacy controls. No private runtime reverse engineering,
headless replacement session, custom UI, or undocumented socket commands.

## Implementation

- Use the existing public HookChannelManifest, HookBinding, HookInstaller and authenticated ingress
  contracts. Keep Claude-specific code inside its plugin package.
- First capture or verify installed hook payloads in isolated resources. The local Claude checkout
  is documentation/plugins, not proprietary runtime source; label captured versus synthetic test
  fixtures accurately. Only enable schemas and joins that evidence supports.
- Extend launch-local --settings composition without modifying user/project settings or removing
  existing receipt hooks. Preserve explicit per-spawn approval and MCP configuration.
- Target exact daemon-trusted session/transcript identity and tool-use identifiers for hook facts.
  Initially bind supported tool-start/result/failure and lifecycle observations with stable
  identities; omit any signal whose correlation cannot be proven. Never join by prompt text, cwd
  or timing guesses.
- Keep transcript-derived turns/results/usage authoritative. Hooks enrich observation; Stop alone
  is not proof of interruption or successful job completion. Avoid duplicate tools or usage and
  do not add hook-based approval answers.
- Hook delivery is bounded and best-effort. Missing/disabled hooks, malformed payloads, schema
  changes or Theater disconnect retain working legacy controls and durable observation. Declare
  unavailable capabilities honestly rather than shipping optimistic decoders.
- Add focused source-backed diagnostics and documentation for the upstream receipt/turn-correlation
  requirements that still block native controls. Do not add unused runtimes or invented controls.

## Ownership and validation

Own theater/harness/builtin/plugins/claude, new Claude-specific tests/fixtures, and this plan plus
coupled Claude documentation. Use existing generic hook seams; report necessary shared changes to
the parent rather than editing files owned by the OpenCode worker. Pi and OpenCode branches may
continue concurrently; no dependency on their in-progress code.

Test captured payload decoding/correlation, duplicate/reordered/malformed events, daemon-RPC
wrong-session and rotation-race rejection, configuration composition, missing-hook fallback, and
transcript completion remaining authoritative. Run focused checks only; the parent owns the full
suite. Live proof must use isolated Theater/tmux/Claude resources, never the production daemon or
existing participant panes. If a supported payload or join cannot be established, keep that binding
disabled and report the blocker.

Commit on feature/claude-harness-rework. Return commits, changed paths, checks and blockers without
a separate report file. Do not push, merge to main, spawn workers or remove worktrees. The parent
and an independent Pi reviewer using mistral/zai-glm-5-3 at max review the resulting implementation.

## Evidence and compatibility boundary

- Official [Hooks reference: common input fields](https://code.claude.com/docs/en/hooks#common-input-fields)
  documents `session_id`, `transcript_path`, `hook_event_name`, and command-hook JSON on stdin.
  Its [PreToolUse](https://code.claude.com/docs/en/hooks#pretooluse),
  [PostToolUse](https://code.claude.com/docs/en/hooks#posttooluse), and
  [PostToolUseFailure](https://code.claude.com/docs/en/hooks#posttoolusefailure) sections document
  `tool_name`, `tool_input`, and `tool_use_id`; the latter two also document optional `duration_ms`,
  while failure documents optional boolean `is_interrupt` and says a cancelled running tool does not
  fire that event.
- Official [background hooks](https://code.claude.com/docs/en/hooks#run-hooks-in-the-background)
  documents `async: true` only for command hooks, immediate native continuation, same stdin JSON,
  separate background processes without deduplication, and no control effect from async output.
  It also says malformed async JSON could crash a session before v2.1.202. The intended compatible
  range is therefore `>=2.1.202,<3`, conditional on capability evidence rather than an exact build.
- The checked upstream documentation/plugins checkout is
  `1f6015b5d578adf79c8527443328a216d6b6a3f1` (changelog 2.1.232). Its 2.1.119 changelog records
  `duration_ms` for `PostToolUse` and `PostToolUseFailure`; the captured stock fixture is explicitly
  labelled as Claude Code 2.1.220 evidence, not a synthetic upstream schema.
- Non-mutating probes are limited: `claude --version` establishes an installed version; generated
  launch-local settings establish Theater's command shape; and an isolated stock-CLI hook capture
  establishes that one release's events reach stdin. None proves session admission, job correlation,
  terminal turn completion, interrupt completion, ordering, retry semantics, or a future release's
  schema. Those properties require release conformance tests using an isolated Claude/tmux/Theater
  environment that captures all three event payloads and verifies the native UI remains live.

## Explicit unsupported guarantees

- A hook credential authenticates the participant/channel route. Before correlation, ingress snapshots
  the daemon's raw persisted harness/session/provenance/transcript identity and identity-quarantine
  state; Claude canonicalizes the trusted and payload paths in its bounded callback, then requires a
  trusted, non-quarantined exact session/path join. Ingress re-reads the raw snapshot after that
  callback. The admitted snapshot stays with the queued delivery and the source compares it with the
  daemon-owned current snapshot both before decoding and after its bounded decoder await, so a callback
  spanning a rotation, a delayed old-session delivery, or an old delivery already queued before rotation
  is dropped rather than projected into a new source epoch. This is admission evidence only; it is not a
  native control receipt or turn contract.
- The generic hook contract has no native prompt acceptance receipt, Theater job-handle field,
  turn-complete field, delivery receipt, or interrupt acknowledgement. `Stop` and tool-hook events
  must not settle a Theater job. Transcript observation remains the sole durable authority for turns,
  results, usage, and completion.
- Theater currently has no shared version/capability-probe contract on which a plugin can enforce the
  declared range before installing launch-local hooks. An install-time compatibility gate belongs in
  the shared spawning/runtime contract; until then malformed or absent hook input fails closed as an
  optional enrichment and leaves legacy controls and transcript observation intact.
