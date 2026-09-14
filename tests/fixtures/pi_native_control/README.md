# Pi native control proof

`pi_core_correlation_proof.mts` runs the real installed stock Pi 0.84.4 SDK
(`AgentSession`, file-backed `SessionManager`, mock assistant stream after
Pi's own agent-session test methodology) and loads the real shipped
`theater_mcp_bridge.ts` through the genuine loader (`DefaultResourceLoader`
`additionalExtensionPaths`, the same temporary CLI scope production's
`--extension` flag feeds). Nothing about Pi is emulated; the bridge connects
to a loopback NDJSON host over a real socket.

Exit codes: `0` ok, `1` failed, `77` skipped (stock Pi 0.84.x unresolvable).

## Proven matrix (Phase A — all green)

- **A1** `sendCustomMessage(msg, {triggerTurn:true})` fire-and-forget makes
  `session.isStreaming` true on the caller's very next statement — no window
  where human input can start a competing run before the guard re-checks.
- **A2** Exactly one durable `custom_message` entry carries
  `details.operation_id`, tree-child of the prior leaf; its entry id is the
  durable per-turn identity.
- **A3** The run's assistant reply is a durable tree-child of the send entry —
  terminal lineage is proven by tree, never by prompt text, timestamps, or
  event ordering.
- **A4** Pi does not deduplicate `details.operation_id` — two sends with the
  same id produce two entries. The bridge must enforce once-only admission
  itself.
- **A5** An unguarded `sendCustomMessage` while streaming is silently absorbed
  as steering (no rejection, no second run) — busy must be refused by the
  admission guard before delivery.
- **A6** `agent.abort()` mid-run leaves a durable assistant message with
  `stopReason: "aborted"` — the exact-turn INTERRUPTED classification.
- **A7** Entry ids and operation details are byte-identical after
  `SessionManager.open` of the same session file — identity survives reload.
- **A8** A fast-settling run is correlated post-hoc from the durable tree
  without observing any event during the run.

## Fail-closed findings (do NOT enable)

- **Steer**: `agent.steer()` only enqueues. The agent loop polls steering at
  fixed boundaries and exits without a final drain, so a steer accepted at
  the last instant strands in the queue and is delivered into a future,
  unrelated run. No synchronous check can distinguish "will drain into this
  run" from "will strand" — STEER stays unavailable.
- **Interrupt**: `ctx.abort()` is void with no expected-run confirmation and
  the request-handler context snapshot goes stale between emissions, so an
  exact-turn abort cannot be acknowledged. INTERRUPT stays legacy-routed
  (Escape), as it is today.
- **Model update**: unproven here (out of scope); settings stay
  reasoning-effort only.

Send and queue-followup native delivery are enabled on this proof; everything
else fails closed exactly as before.
