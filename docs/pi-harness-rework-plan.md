# Pi harness rework

## Outcome

Keep stock interactive Pi and its current Theater launch, MCP extension, transcript isolation,
resume/fork behavior and approval restrictions. Add live observation and confirmed session-local
thinking updates through supported extension APIs; model mutation stays proof-gated. Preserve working
legacy send, followup and interrupt routes. Native integration must never reduce existing capabilities.

This branch is an independent implementation workstream. OpenCode continues unchanged on its own
branch. Integration of shared runtime infrastructure belongs to the parent orchestrator afterward.

## Implementation

- Extend the bundled Pi extension without replacing the prompt or importing private AgentSession
  or UI internals. Keep MCP, transcript switches, idle markers and user extensions working.
- Add a bounded authenticated local bridge with reconnect and disposal tied to Pi's supported
  extension lifecycle. A daemon disconnect must not terminate Pi or its work. Secrets stay in
  participant-owned files; never put them in logs or argv.
- Expose snapshot, observation and settings operations. Snapshots identify the exact current
  session, effective model/thinking and execution state. Use agent_settled for outer idle;
  agent_end alone is insufficient across retries and compaction. Session switches invalidate
  pending operations for the previous session.
- Use public getThinkingLevel and setThinkingLevel only after exact session, idle and no-pending
  guards. Its default `persist=false` makes the change transcript-local rather than a global
  default. Public setModel awaits provider auth before mutation and has no supported atomic
  expected-session guard, so model updates (including mixed model/thinking requests) remain
  proof-gated until upstream adds one. Report actual thinking readback after native clamping; do
  not claim an unconfirmed update succeeded or silently roll back a later human choice.
- Correlate every settings operation by operation ID and exact session. Lost responses remain
  uncertain until readback; never automatically replay mutations. Native-only settings can become
  unavailable on bridge failure while existing legacy controls continue working.
- Keep durable JSONL observation authoritative for recovery and use live events for timely
  enrichment. Deduplicate usage/content and preserve existing job completion and identity rules.
- Initial native SEND, STEER and INTERRUPT remain disabled. Their future enablement requires proof
  of tagged admission, public signal/run correlation, retry/compaction completion and guarded
  cancellation. Do not misclassify absence of a vendor turn ID as proof they are impossible.
- Target Pi >=0.84.4,<0.85.0 with version and public API capability probes. Failure selects existing
  behavior. Preserve the existing yolo-only launch contract.

## Independent integration boundary

Own the Pi plugin, Pi tests/fixtures and this plan/documentation. Do not edit OpenCode's worktree or
shared runtime/contracts/daemon files that its worker currently owns. Implement the plugin bridge
and runtime logic against an injected peer with request(method, params, timeout), notifications()
and close semantics; use existing public runtime values for snapshots, settings and receipts.

The bridge uses bounded NDJSON. Extension hello contains type=hello, protocol=theater-frontend-v1
and the participant token. Notifications use event/snapshot/history frames. Duplex requests use
type=request, a string id, method and params; responses use type=response, the same id and either
result or error. Pi methods are pi.snapshot and pi.settings.update; settings params carry
operation_id and native_session_id. Snapshots and events carry a bridge epoch; snapshots also carry
monotonic revision and event-sequence watermarks, so delayed history cannot roll a live session
backward. The parent owns adapting this additive duplex seam to the
shared frontend host after both branches are ready. Passive OpenCode clients remain compatible.

Build and test the independent implementation now; do not wait for or cherry-pick an uncommitted
OpenCode foundation. Keep final activation behind the real integration boundary rather than
checking in imports of nonexistent public APIs. Report the precise remaining glue at handoff.

## Validation and delivery

Run focused Pi/bridge tests, lint and typing for changed code. Cover session switch/reload, duplicate
extension loading, model proof-gating, thinking clamping/readback, busy and wrong-session refusal,
disconnect,
retry/compaction, and preservation of legacy controls and isolated transcript resume. Use isolated
resources for stock-Pi proof; do not change the production daemon or existing participant panes.
The parent runs integration/full regression checks and reviews the exact resulting commit with an
independent Pi reviewer using mistral/zai-glm-5-3 at max. Unrun live proof is not a passed gate.

Commit on feature/pi-harness-rework. Do not push, merge to main, spawn workers or remove worktrees.
