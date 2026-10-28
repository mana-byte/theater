# Tests

The tree mirrors the source packages: one directory per subject, one file per concern.

## Running

```sh
make test-unit                      # inner loop: everything except real-binary proofs
make test                           # full suite, parallel (tmux + integration included)
make test-serial                    # full suite in one process (flake debugging)
uv run pytest tests/daemon/controls # one area; any directory or file works
```

Never run bare `pytest` for the whole suite; use the Makefile. Lanes are markers
(`pyproject.toml`): `tmux` drives a real tmux server and self-skips without it;
`integration` launches real third-party binaries or builds distributions and is excluded
from `make test-unit`; `requires_node` shells out to node and skips when it is absent.

## Layout

| Directory | Purpose |
|---|---|
| `support/` | Shared helpers (and `test_tmux_guard.py`); import as `from tests.support.x import ...`. `isolation.py` holds the autouse fixtures every root conftest star-imports |
| `fixtures/` | Data only: recorded transcripts and protocol payloads |
| `daemon/controls/` | Send, steer, follow-up and dispatch control state machine |
| `daemon/presence/` | Human-presence classification, monitor and provider access |
| `daemon/handoff/` | `after` handoffs: gates, cards, evidence |
| `daemon/jobs/` | Job manager, awaits, completion |
| `daemon/observation/` | Observer, status policy, quiet timers, turns |
| `daemon/persistence/` | Store, migrations, home layout, write units |
| `daemon/gc/`, `daemon/participants/` | Retention sweeps; registry, lineage, adoption |
| `daemon/recall/` | Path-touch history and segment reader |
| `daemon/spawning/` | Launch planning, resume, spawner |
| `daemon/worktrees/` | Unique and named shared worktrees |
| `daemon/recovery/` | Restart and fault recovery |
| `daemon/lifecycle/`, `daemon/diagnostics/` | Daemon lock, server, shutdown; stats RPCs |
| `daemon/rpc/` | RPC handlers and parameter validation |
| `daemon/operations/` | Control transfer, idempotency, operation journal |
| `daemon/events/` | Event bus, long-poll, journal coverage |
| `daemon/harness_runtime/` | Native runtime backends, frontend requests, websocket transport |
| `daemon/trajectory/` | Trajectory service: snapshots, panel state, live follow |
| `provider/` | Terminal-provider API: fencing, controls, adoption, process |
| `frontend/` | Public SDK, socket, schemas and handshake |
| `harness/core/` | Harness contracts and shared behaviour, split into `channels/`, `composite/`, `launch/`, `manifests/`, `parse/`, `plugins/` |
| `harness/<name>/` | Per-harness plugin behaviour: claude, codex (`runtime/`), opencode (`server/`, `storage/`), pi, vibe (`unified_store/`) |
| `transcripts/` | Transcript sources, identity, paging, recovery; `collision/` and `source/` hold the large suites |
| `trajectory/` | Harness-neutral trajectory records, tools, causality, telemetry |
| `observability/` | Metrics, tracing, logging and signals |
| `mcp/`, `mcp/plugins/` | MCP server tools; MCP-server plugin contracts and Beads |
| `skills/` | Skill and play discovery and loading |
| `cli/`, `config/`, `client/` | CLI commands, configuration, daemon client and protocol |
| `pricing/` | Token cost estimation |
| `boundaries/` | Import and process-boundary rules |
| `packaging/` | Built distributions and artifacts |
| `perf/` | Latency baselines and synthetic loads |
| `integration/<vendor>/` | Distribution and focused SDK integration regressions; not qualification authority |

Régie tests live in `packages/regie/tests`, grouped like `packages/regie/src/regie`
(`ambience/`, `app/`, `bridge/`, `controllers/`, `dashboard/`, `motions/`, `render/`, `tmux/`,
`trajectory/`), with helpers in `packages/regie/tests/support`.

The default-server case in Régie's `tmux/test_provider.py` requires an explicitly disposable
Linux Docker container (`/.dockerenv` and `REGIE_DISPOSABLE_DOCKER=1`), and refuses an existing
default socket. Ordinary runs use a private sentinel socket. Set that flag only inside a
fresh container; the default-server proof creates and destroys that container's default server.

## Rules

- File name = subject. No history, wave or release names (`rc10_*`, `stack_d`) and no
  redundant directory prefix (`daemon/controls/test_send.py`, not `test_controls_send.py`).
- Test directories get no `__init__.py` (only `support/` is a package): pytest
  runs with `--import-mode=importlib`, so the same basename may appear in different directories.
- Shared test code lives in `tests/support`; fixture data describes behavior, never a history of
  CLI releases that passed qualification. Fixtures are synthetic: a capture is stamped with a
  release that leaves the window within days.
- A test never names a CLI release inside or below a plugin's window and never computes "one below
  the floor" from a version digit. Use `VERSION_POLICY.minimum` and `maximum` as they are, a neutral
  extreme (`0.0.1`, `99.0.0`) or the maximum's next patch; the boundaries are proven once, in
  `tests/harness/core/test_version_policy.py`. The release that exposed a bug belongs in the
  commit message.
- Every module opens with a one-line docstring stating its subject.

## Harness compatibility

The live conformance suite, the compatibility pipeline and their tests live in
[`harness_compat/`](../harness_compat/README.md); `make test` collects them too. Nothing under
`tests/` imports that package.
