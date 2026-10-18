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
| `support/` | Shared helpers (and `test_tmux_guard.py`); import as `from tests.support.x import ...` |
| `fixtures/` | Data only: recorded transcripts, schemas, compat evidence |
| `native/` | Qualification helpers imported by `scripts/harness_compat` |
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
| `harness/<name>/` | Per-harness plugin behaviour: claude, codex (`runtime/`), opencode (`server/`, `v2/`, `storage/`, `frontend/`), pi, vibe (`unified_store/`) |
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
| `integration/<vendor>/` | Real-binary, opt-in proofs (claude, codex, opencode, pi); skip unless their env vars are set |
| `compat/` | Harness-compat pipeline tests |

Régie tests live in `packages/regie/tests`, grouped like `packages/regie/src/regie`
(`ambience/`, `app/`, `bridge/`, `controllers/`, `dashboard/`, `motions/`, `render/`, `tmux/`,
`trajectory/`), with helpers in `packages/regie/tests/support`.

## Rules

- File name = subject. No history, wave or release names (`rc10_*`, `stack_d`) and no
  redundant directory prefix (`daemon/controls/test_send.py`, not `test_controls_send.py`).
- Test directories get no `__init__.py` (only `support/`, `native/` are packages): pytest
  runs with `--import-mode=importlib`, so the same basename may appear in different directories.
- `tests/native` and `tests/fixtures` stay where they are because `scripts/harness_compat`
  imports and digests them.
- Every module opens with a one-line docstring stating its subject.
