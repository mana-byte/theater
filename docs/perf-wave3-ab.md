# Wave 3 A/B measurement (2026-10-10, protocol-compliant)

Per docs/perf-optimization-plan.md measurement protocol: same host, same session,
alternating runs, 2 of each side, pre-optimization code `34625699` vs post-Wave-3
HEAD. Load average recorded before/after every run (see /tmp/ab-results/log.txt
provenance at measurement time; raw reports in ab-results).

## CPU mean % (one core = 100%)

| scenario | role | baseline mean | HEAD mean | delta |
|---|---|---:|---:|---:|
| empty | daemon | 1.72 | 0.48 | -1.23 |
| empty | bridge | 0.45 | 0.35 | -0.09 |
| empty | regie | 4.39 | 2.57 | -1.82 |
| empty | tmux | 0.36 | 0.28 | -0.08 |
| idle10 | daemon | 7.01 | 4.92 | -2.08 |
| idle10 | bridge | 3.41 | 2.78 | -0.63 |
| idle10 | regie | 3.97 | 2.25 | -1.71 |
| idle10 | tmux | 0.64 | 0.53 | -0.10 |
| idle10+working5 | daemon | 14.09 | 13.91 | -0.18 |
| idle10+working5 | bridge | 5.45 | 4.88 | -0.57 |
| idle10+working5 | regie | 7.53 | 4.29 | -3.24 |
| idle10+working5 | tmux | 1.32 | 1.23 | -0.09 |

Every role in every scenario is equal or better; controls (tmux cpu/fork) matched.

## Working-load pair at matched event rate

head-run2 12,282 events/h vs base-run2 13,844 (-11.3%, within the ±15% protocol):
daemon 13.38 vs 13.94, regie 4.47 vs 7.37, bridge 4.91 vs 5.39.
head-run1 ran at 31,965 events/h (no valid baseline pair at that rate); its
p95 4,848 ms repeats the high-load tail-scale watch item.

## Latency (transcript_to_bus, includes harness write delay)

- Matched-rate pair: HEAD p50 315.5 / p95 572.9 ms vs baseline p50 309.7 / p95 526.7 ms.
- A prior session measured HEAD p95 443.3 at 14,311 events/h (docs/perf-profile-head.md).
- Verdict: inconclusive within observed run-to-run spread (443-573 at ~14k events/h);
  the <=500 ms gate is not consistently met. Needs a latency noise band or more runs.

## Gating criteria status

- [x] Idle CPU (same-host, all roles): PASS — deltas -0.08 to -2.08, all negative.
- [x] Working CPU at matched rate: PASS — daemon -0.56, regie -2.90.
- [ ] transcript_to_bus p95 <= 500 ms: inconclusive (573 matched pair; 443 prior session).
- [x] Event amplification and persistence: met (see prior reports).
- [ ] tmux forks < 0.5/s idle: unchanged; depends on the fs-wakeup wiring follow-up and Wave 4.
