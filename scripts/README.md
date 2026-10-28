# Development scripts

Repository tooling for developers; these scripts are not shipped runtime code. Harness release
qualification lives in [`harness_compat/`](../harness_compat/README.md).

| Directory | Purpose |
| --- | --- |
| `bench/` | Performance, latency and MCP surface measurements. |

## Benchmarks

`perf_baseline.py` measures daemon, terminal-provider and Régie CPU/latency.
`regie_spawn_latency.py` measures UI spawn latency; the other scripts measure individual costs.
`perf_synthetic_writer.py` and `perf_synth_plugin/` provide the synthetic workload.

Run from the repository root with the development environment installed. Stack benchmarks need tmux;
the CPU baseline also needs psutil:

```sh
uv run --all-packages --with psutil python scripts/bench/perf_baseline.py \
  --synthetic 0.73 --out /tmp/theater-perf.md
uv run --all-packages python scripts/bench/regie_spawn_latency.py --tree . --runs 20
```

Benchmarks print measurements or write reports to their selected output paths. Each script's
docstring and `--help` explain its options and how to compare checkouts.
