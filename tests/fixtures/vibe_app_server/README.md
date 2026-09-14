# Vibe app-server Phase 0 proof record

Result: **FAILED — topology blocker persists.** No native Vibe runtime is
implemented; the shipped plugin keeps transcript observation and the legacy
Escape interrupt. This directory pins the evidence so a future upstream change
can be detected and re-verified deliberately.

## Pinned release

- Product: mistral-vibe, version `2.25.1`
- Commit: `2817f3df81ae05d49ba9538262edb1d5a18fa006`
- Public executable: `vibe-app-server` (`vibe.app_server.stdio:main`)
- Inspected checkout: `/Users/manaiki.laut/Desktop/coding_clis/mistral-vibe`

## Go criteria (all three absent)

- **Observer/control role:** none. The method catalogue has no subscribe,
  observe, or control-observer method, and ADR 0009 (line 143) states the
  protocol "does not currently model several simultaneous attached observers of
  one runtime".
- **Stock-TUI extension:** none. The stock Textual UI creates the app server
  in-process over a private memory transport pair (`vibe/app_server/local.py`)
  and is the sole attached client. ADR 0007's extension mechanisms are
  backend-owned; none exposes an external control channel into the TUI.
- **Shared broker:** none. Vibe owns no multiplexer and documents no request,
  callback, or reconnect semantics for one.

## Topology

```text
   stock Textual UI (the pane)            Theater (would-be second client)
        │                                          │
        │ in-process memory transport pair         │  no public attachment surface
        ▼                                          ▼
   AppServer instance  ── one attached client ──►  blocked
        │
        ▼
   root runtime + child-session registry
```

- `vibe-app-server` stdio serves exactly one connection per process.
- With the stock default backend, a second `session/start` on the same
  connection is refused with `conflict` "A session is already attached"; where
  the Unified Harness is rolled out, `session/start` replaces the attached
  backend with a new session id instead — either way exactly one attached
  client, never a multiplexed observer.
- A second `vibe-app-server` process resuming a session that is live in
  another process gets `conflict` / `harnessCode: "session_busy"`.
- Server-to-client `callback/call` and `clientTool/*` requests go to the one
  attached client whose capabilities were captured at `initialize`; approvals
  and filesystem/terminal tools cannot be shared with an observer.

## Initialization exchange (captured)

See `handshake.json`. Wire aliases are camelCase, `params` is required even on
the `initialized` notification, `initialize` is once-only, and the response
carries `serverInfo` only (no negotiated protocol version field).

## Live observations

`topology.json` records each over-the-wire fact, including the exact-turn
guards (`stale_turn` with `activeTurnId` when a turn is active, `conflict`
"No active turn" when idle), `turn/start` idempotency receipts, queue
idempotency conflicts, `session_busy` cross-process exclusivity, and that
`session/turn/queue/steer` is `not_implemented` by the installed Unified
Harness. `capability_schemas.json` pins the typed schemas a future adapter
would map Theater capabilities onto (captured from the pinned release's
`vibe.app_server.protocol`).

## Re-running the proof

Offline conformance always runs: `uv run pytest tests/test_vibe_native_topology_proof.py`.
The executable proof is opt-in and drives the pinned checkout against an
isolated `VIBE_HOME`:

```sh
THEATER_VIBE_NATIVE_PROOF=1 \
    uv run pytest tests/test_vibe_native_topology_proof.py -v
```

It self-skips unless `THEATER_VIBE_CHECKOUT` (default
`/Users/manaiki.laut/Desktop/coding_clis/mistral-vibe`) holds a checkout whose
version matches the pin; drift must be resolved by re-capturing these
fixtures, not by weakening assertions. Implement the native runtime only
after a real release passes the plan's go criteria — never via a proxy,
headless default, or weakened gates.
