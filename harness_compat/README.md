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
| `cli.py` | The entry point, run as `python3 -m harness_compat <command>` |
| `contract.py`, `diagnostics.py`, `git.py`, `policy.py` | Shared by `qualification/` and `github/`: types, bounded diagnostic text, git queries, `compatibility.json` and the PR guards |
| `adapters/` | One per shipped plugin, named after its folder: how to discover, install and provision its releases; `_*.py` are shared helpers |
| `mock/` | The mock model server, and how an adapter provisions its CLI against it |
| `qualification/` | One release in a sandbox (`qualify.py`), the sweep over newer releases (`run.py`), the canaries, and the sandbox and process code they share |
| `github/` | What the pipeline reads from and writes to GitHub: issues and the policy PR (`publish.py`), the `report` and `failures` commands, the run summary |
| `conformance/` | Completeness and guard tests; `live/` holds the live suite, `canaries/` the canaries |
| `support/` | The live suite's rig and process stack, obligations, coverage registry, declaration walker, tool and channel oracles, process table, mutations and probe sidecar (`compat_probe/`) |
| `unit/` | Tests of the pipeline |

A module stays at the root only if it is the entry point or both `qualification/` and `github/` use
it. The CLI and every adapter must import on a bare `python3`, without the project's dependencies:
the plan and fan-out jobs import them before anything is installed, and two unit tests check it.

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
