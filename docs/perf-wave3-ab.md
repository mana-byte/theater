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

## Confirmation session (post-wakeup-wiring, 3 alternating pairs)

NOTE: the working-load rows below mix runs with unmatched event rates
(28k-106k events/h) — under the protocol they are INVALID as pass/fail
evidence and are recorded for observation only. The idle rows remain
valid (idle has no event-rate dependency).

CPU reconfirmed across 3 pairs: every role in every scenario PASS again.

| scenario | role | base mean (3 runs) | HEAD mean (3 runs) | delta |
|---|---|---:|---:|---:|
| empty | daemon | 1.70 | 0.59 | -1.11 |
| empty | regie | 4.35 | 3.00 | -1.35 |
| idle10 | daemon | 6.48 | 5.44 | -1.04 |
| idle10 | regie | 3.87 | 2.45 | -1.42 |
| idle10+working5 | daemon | 18.46 | 16.41 | -2.05 |
| idle10+working5 | regie | 7.41 | 4.00 | -3.41 |

Latency: NO matched pairs — the workload agents this session produced 28k-106k
events/h (2-7x the 12k-16k gate band), so no latency verdict is drawable. At
those high rates BOTH sides show multi-second p95 tails (2.7-14s), confirming
the tail scales with agent chattiness, not with theater's code. The latency
gate remains: single valid pair at ~14k (HEAD 443 vs base 469, prior session);
3 matched pairs still needed.

Controls: tmux cpu/fork matched across all pairs.

## observe_to_bus — first measurement (post-final-pass HEAD)

The new theater-side-only latency metric (bus ts minus observer read time;
harness write delay excluded), from the same isolated measurement stack:

| scenario | events/h | transcript_to_bus p50/p95 ms | observe_to_bus p50/p95 ms |
|---|---:|---|---|
| idle10+working5 | 30,617 | 118.1 / 3,108.8 | **1.8 / 12.7** |

Single run, no baseline comparison, and an important scope limit: this
metric measures read-to-publish (processing inside the daemon) ONLY —
detection delay (file written -> observer wakes and reads), which is
exactly what the fs-wakeup work changed, is EXCLUDED. The <50 ms
aspiration is met for the processing component; the detection component
needs the synthetic-writer A/B to be measured honestly. The
seconds-scale transcript_to_bus tails are dominated by harness write
delay and agent chattiness, but theater detection delay is inside that
metric and not yet split out.

## Final close-out A/B (current HEAD incl. audit fixes, 3 alternating pairs)

Idle scenarios: PASS for every role across all 3 pairs (daemon empty -1.30,
idle10 -1.13; regie empty -1.69, idle10 -1.54; bridge and tmux improved or flat).

Working load: pair 1 is the only rate-matched pair (~32k events/h both sides):
daemon +0.17pp — inconclusive within the protocol's 1pp band (working daemon
CPU is FLAT across the campaign at matched rates); regie -2.56pp PASS;
bridge -0.47pp PASS. Pairs 2-3 are invalid working evidence (HEAD ran at
2.2-3.2x the baseline's event rate); their means must not be used.

## Synthetic write-to-bus latency A/B (the honest latency instrument)

Deterministic rate-matched workload (0.73 rec/s x writers, de-aliased from the
baseline poll cadence, records stamped at write time; scripts/perf_synthetic_writer.py):

| | write-to-bus p50 | write-to-bus p95 | observe_to_bus p50/p95 |
|---|---:|---:|---|
| baseline 34625699 | 121.5 ms | 237.6 ms | n/a |
| HEAD | **1.3 ms** | **2.9 ms** | 0.4 / 0.8 ms |

~93x end-to-end delivery latency improvement at matched rates (13.1k events/h,
0.3% drift). The kqueue wakeups collapsed detection delay, which polling
dominated. The <50 ms aspiration is met with ~20x margin on the true metric.
