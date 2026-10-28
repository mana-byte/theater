# Harness compatibility

Qualification of new upstream CLI releases against Theater's harness plugins: the pipeline, its
live suite and their tests. Not shipped. Only these workflows invoke it; there are no local
qualification targets.

| Workflow | Commands |
| --- | --- |
| `.github/workflows/harness-compat.yml` | `plan`, `run`, `failures` |
| `.github/workflows/harness-compat-verify.yml` | `shards`, `verify` |
| `.github/workflows/harness-compat-canaries.yml` | `canaries` |
| `.github/workflows/harness-compat-attention.yml` | `report` |

| Path | Purpose |
| --- | --- |
| `*.py` | The pipeline, run as `python3 -m harness_compat <command>` |
| `adapters/` | One per shipped plugin, named after its folder: how to discover, install and provision its releases; `_*.py` are shared helpers |
| `conformance/` | Completeness and guard tests; `live/` holds the live suite, `canaries/` the canaries |
| `support/` | The live suite's rig and process stack, obligations, coverage registry, declaration walker, tool and channel oracles, process table, mutations and probe sidecar (`compat_probe/`) |
| `unit/` | Tests of the pipeline |

The pipeline tests every newer stable release through the generic live suite. Passing releases may
advance `max`; red, inconclusive and tooling failures get issues and exclusions. Updates accumulate
in one PR per harness, changing one line in that plugin's `compatibility.json`.

Qualification uses disposable source copies, homes and reports. It retains no generated test data
or passing-version history.

`conformance/live` never runs under `make test`: it is collected only inside the harness-compat
sandbox (`HARNESS_COMPAT_SANDBOX`), so it can never touch a developer's `~/.theater`, CLI homes or
tmux server. Its scenarios are derived from each plugin's manifest (`support/obligations.py`);
`support/coverage.py` must map every manifest declaration to a scenario family or a reasoned
exemption, and `conformance/test_completeness.py` fails when it does not. The tests share
Theater's autouse isolation fixtures (`tests/support/isolation.py`) through `conftest.py`.

See [Harness compatibility](../docs/harness-compat.md) for the full flow.
