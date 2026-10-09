# Theater CPU Optimization — Implementation Plan

**Status:** Approved
**Date:** 2026-10-09
**Debate:** PerfOpus (opencode/opus-5.5) structured debate
**Verdict:** Rust rewrite PREMATURE. Fix architecture in 4 waves.

## Rejected

| Optimization | Reason | Risk |
|---|---|---|
| Full Rust rewrite | Premature, 12-24 months, re-implements fail-closed invariants | critical |
| Replace Textual | Discards 21.5K lines + UI test rig | high |
| Slow animation-bus poll (0.4→1.0s) | Adds latency, breaks goal | low |
| Raise poll intervals globally | Trades latency for CPU | medium |
| Remove transient UNKNOWN from presence | Violates fail-closed invariant | critical |
| Screen-content heuristics for presence | Forbidden by AGENTS.md | high |

## Wave 1: Safe Quick Wins (1 week)

| # | Optimization | Owner | Files | Effort | CPU Reduction |
|---|---|---|---|---|---|
| 1 | Measurement baseline script | shared | scripts/perf_baseline.py (new) | small | n/a |
| 2 | SQLite pragma tuning | daemon | persistence/database.py | trivial | -3-6% |
| 3 | Presence revision bump only on change | daemon | presence/monitor.py | trivial | -2-4% |
| 4 | Display-only screen captures reuse focus | shared | presence/monitor.py, screen_reading.py, focus_monitor.py | small | -8-12% |
| 5 | Back off WORKING screen checks | daemon | observation/reducer.py, constants/observation.py | trivial | -3-6% |
| 6 | Filter dead bindings from reconnect | daemon | terminals/service.py | small | -2-5% |
| 7 | Silence stable heartbeat events | daemon | terminals/service.py:156-300 | small | -3-5% |
| 8 | Skip unchanged Régie tree renders | regie | app_parts/projection.py, widgets/tree.py | small | -10-15% |
| 9 | Cache ambience widget references | regie | app_parts/ambience.py | trivial | -10-15% |
| 10 | Shared animation clock | regie | widgets/leaf.py, separator.py, tree.py | small | -20-30% |
| 11 | Copy follow projection once per batch | shared | frontend/state_sync.py | trivial | -3-5% |

## Wave 2: Event Amplification + Write Batching (1.5-2 weeks)

| # | Optimization | Owner | Risk |
|---|---|---|---|
| 1 | Scoped presence invalidation + coalesced publication | shared | high |
| 2 | One write transaction per observation batch | daemon | medium |
| 3 | Kind-filtered long-poll for diagnostic bus | shared | low |

## Wave 3: Polling→Push + Read Caching (2 weeks)

| # | Optimization | Owner | Effort | Risk |
|---|---|---|---|---|
| 0 | Profiling tooling: preserve scenario-specific raw profiles and the full sample denominator; report inclusive watch-loop cost separately from leaf costs | shared | small | low |
| 1 | Cache schema validators by schema id; construct and check each schema once (catalog.py:41,55,64) — targets the measured 18% idle jsonschema share | daemon | small | low |
| 2 | Eliminate redundant reads before any cache: `transcript_identity_lost` returns early when pid is not in `_identity_lost` (failures.py:180) — one SELECT per ordinary poll removed | daemon | small | low |
| 3 | Transcript-discovery scan reduction: reuse DirEntry/stat results within scans, coalesce overlapping scans of the same domain; candidate-metadata index later; never count a cached observation as two independent identity-loss confirmations | daemon | medium | medium |
| 4 | Filesystem-notification wakeups (revised scope): kqueue EVFILT_VNODE adapter for transcript files + directories, composed with WakeupSignal clear-before-read; notifications mark sources dirty, never infer absence; bounded polling fallback for non-notifying sources | daemon | medium-large | medium-high |
| 5 | Supervisor reconcile: dirty-participant reconciliation + bounded repair scan, only if still material after 1-4 (full dirty-signal wiring: lifecycle/binding/provider changes, unpublished writers) | daemon | medium | medium |
| 6 | Régie animation investigation (only if the <2% aspirational target is pursued): ambience renderer allocates a width×height grid per tick (ambience/render.py:13); benchmark sparse rendering and redundant-update suppression | regie | medium | low |

**Dropped from the original Wave 3:** orjson on NDJSON transport — no measured encode cost justifies a shared wire-format compatibility change; reconsider only after serialization profiling.

**Cache-invalidation warning (from scoping review):** journal-only cache invalidation is unsound — explicit participant writes bypass publication (store_parts/participants.py:31) and healthy binding report revisions deliberately avoid journaling (terminals/bindings.py:122). Any future cache must cover every writer.

## Wave 4: tmux Control Mode (conditional, 3-4 weeks)

| # | Optimization | Owner | Risk |
|---|---|---|---|
| 1 | Persistent tmux control-mode client (feature flag) | regie | high |

**Gate:** Only if wave-3 profiling shows tmux forks > 10% CPU.

## Team Structure

| Lead | Harness | Model | Owns |
|---|---|---|---|
| Codex Lead | codex | gpt-5.6-sol xhigh yolo | Daemon persistence, observation, presence, terminal service |
| OpenCode Lead | opencode | opus-5.5 yolo | Régie UI, bridge/tmux, frontend SDK, baseline |

## Shared Contracts (fix before Wave 2)

1. **providers.report**: `presence_invalidated: bool` + optional `invalidated_terminals: [terminal_id]`
2. **diagnostics.bus_tail**: `after_id`, `limit`, optional `kinds: [str]`, `wait_seconds`
3. **Presence admission**: `snapshot_for_binding` and `require_absent` unchanged; journal publication delayed ≤250ms
4. **write_unit**: no nesting; writers accept `connection=`; side effects in `after_commit`
5. **Capture semantics**: `capture_screen(display_only=True)` may reuse focus evidence <500ms old
6. **Perf baseline**: `scripts/perf_baseline.py` + `docs/perf-baseline.md` shared metric names

## Acceptance Criteria

Recalibrated 2026-10-10. The original absolute targets were set against `docs/perf-baseline.md` (commit `34625699`), which was captured on a loaded host. The quiet-host A/B control in `docs/perf-final.md` (section Quiet-host A/B control) shows that the same pre-optimization code uses more CPU on a quiet host than that baseline recorded. The gating CPU criteria are therefore same-host comparisons against the pre-optimization code. The original absolute numbers are kept below as aspirational stretch targets.

### Measurement protocol (applies to every CPU and latency criterion)

1. **Same host, same session.** Measure the pre-optimization code (`34625699`) and the candidate commit back to back with `scripts/perf_baseline.py` on the same host. Alternate them A/B/A/B, with at least 2 runs of each. Never compare against a number captured in a different session.
2. **Same settings.** Use the same `--warmup` (30 s), `--duration` (120 s), harness and agent counts on both sides. The original baseline used a 30 s warmup and later runs used 20 s (header of `docs/perf-baseline.md` vs `docs/perf-final.md`). `empty` runs first after Régie starts, so a short warmup lets startup work leak into its window.
3. **Record host state.** Record the load average (`sysctl -n vm.loadavg`) before and after each run. Use the `tmux` role's CPU per fork as a control: this plan does not change tmux, so if its CPU per fork differs between A and B, the host state differed and the pair is invalid. Example: `empty` tmux was 0.16 % at 1.63 forks/s in `docs/perf-baseline.md` but 0.37 % at 1.65 forks/s in `docs/perf-final.md`.
4. **Comparable event rates for working scenarios.** An `idle10+working5` pair counts only if `bus_events_per_h` match within ±15 %. If they do not, re-run; do not normalise by event count, because the fixed idle cost makes CPU per event depend on load. Observed rates vary a lot between runs: 14 200/h (`docs/perf-baseline.md`), 14 311/h (`docs/perf-profile-head.md`), 43 240/h (`docs/perf-final.md`).
5. **Noise band.** On the same code, single runs vary by up to about 0.9 pp. Examples: HEAD `empty` Régie measured 2.69 / 3.40 / 3.58 % and HEAD `idle10` daemon 5.24 / 5.72 / 6.03 % (`docs/perf-profile-head.md`, `docs/perf-final.md`, `docs/perf-baseline-head.md`). A relative criterion is judged on the mean of the alternating runs. A single pair that differs by less than 1 pp is inconclusive, neither a pass nor a fail.
6. **Report all four roles** (daemon, bridge, regie, tmux) for both sides of every A/B pair.

### Gating criteria

**CPU — same-host relative**

- [ ] Idle: in `empty` and `idle10`, for each of daemon, bridge and regie, the candidate's mean CPU is <= the pre-optimization code's mean CPU on the same host, measured per the protocol above.
  - Evidence so far, one pair (`docs/perf-final.md`, Quiet-host A/B control), pre-optimization vs HEAD: daemon `empty` 1.70 vs 0.71 %, daemon `idle10` 6.04 vs 5.72 %; Régie `empty` 4.26 vs 3.40 %, Régie `idle10` 3.68 vs 2.68 %. Bridge has not been A/B-measured yet. A second alternating pair covering all roles is needed to tick this box.
- [ ] Working load: in `idle10+working5` at comparable event rates, the candidate's daemon CPU and Régie CPU are each <= the pre-optimization code's on the same host.
  - Evidence so far, comparing across sessions at about 14.2k events/h: daemon 13.51 % pre-optimization (`docs/perf-baseline.md`, 14 200/h) vs 13.19 % HEAD (`docs/perf-profile-head.md`, 14 311/h); Régie 6.66 % vs 4.03 %. A same-host pair is still needed.

**Latency**

- [ ] `transcript_to_bus` p95 <= 500 ms, and <= the pre-optimization code's p95 on the same host, measured in `idle10+working5` at 12k-16k bus events/h. The metric definition is unchanged: bus `ts` minus the harness record `ts`. It therefore includes the harness's own write delay (see the Caveats section of each report).
  - Evidence: pre-optimization 469.4 ms at 14 200/h (`docs/perf-baseline.md`); HEAD 443.3 ms at 14 311/h (`docs/perf-profile-head.md`). The 500 ms limit leaves about 13 % headroom above the best observed p95.
- [ ] Watch item, not gating until explained: at 43 240 events/h HEAD's p95 is 8 111.9 ms and p50 is 1 910.9 ms (`docs/perf-final.md`). No pre-optimization measurement exists at that rate. Measure one before deciding whether this is normal load scaling or a regression.

**Event amplification and persistence (still valid)**

- [x] No periodic journal events at idle. `empty`: 0/h, down from 358/h of `provider.updated` (`docs/perf-baseline.md` vs `docs/perf-final.md`). `idle10`: at most 1 event per 120 s window (`job.updated`, 30/h, `docs/perf-final.md`).
- [x] controls_changed < 10/hour per idle agent: 0.0 in every scenario (`docs/perf-baseline.md`, `docs/perf-final.md`).
- [ ] 1 commit per observation batch, and none for an empty batch. Covered by `tests/test_write_unit.py` (`test_nonterminal_observation_batch_commits_once_with_checkpoint`, `test_terminal_batch_commits_once_and_replay_never_completes_the_next_job`) and `tests/test_write_free_batches.py` (`test_empty_batch_opens_no_write_unit`, `test_batch_with_events_opens_exactly_one_unit`). Tick when these pass on the merge commit.
- [ ] Zero false-ABSENT admissions. There is no runtime counter for this, so it is enforced by the fail-closed presence tests (`tests/test_presence_*.py`, `tests/test_presence_scoped_invalidation.py`) plus code review of every presence change.
- [ ] Full test suite, lint, mypy and `alembic check` clean on the merge commit.

**Not yet met or not yet measured**

- [ ] tmux forks < 0.5/s at idle, and 0 with control mode. Not met after Wave 2: `idle10` 9.00/s and `empty` 1.65/s (`docs/perf-final.md`), essentially unchanged from 9.10 and 1.63/s (`docs/perf-baseline.md`). Moved to Wave 3/4 (polling to push, tmux control mode).
- [ ] Animation latency p95 < 50 ms. `scripts/perf_baseline.py` does not measure this yet; add a measurement before it can gate.
- [ ] Awaiting-input detection p95 <= 2 s. Not measured yet.
- [ ] RPC p99 improves by >= 20 % under load. Not measured yet; when measured, use a same-host A/B at comparable event rates.

### Aspirational stretch targets (non-gating)

These are the original absolute numbers. The original reference values (~12 % daemon, ~7 % Régie) do not appear in any committed report and are replaced by the measurements listed here.

- Idle daemon CPU < 3 %. Currently `idle10` 5.72 % HEAD vs 6.04 % pre-optimization on the same host (`docs/perf-final.md`).
- Idle Régie CPU < 2 %. Currently `empty` 3.40 % and `idle10` 2.68 % (`docs/perf-final.md`).
- Régie CPU < 4 % with 5 working agents. Currently 4.05 % (`docs/perf-final.md`) and 4.03 % (`docs/perf-profile-head.md`).
- Theater-side delivery latency p95 < 50 ms. This was the intent of the original transcript-to-bus target. It needs a new metric, `observe_to_bus`: bus `ts` minus the time the observer read the record, which leaves out the harness's write delay. That timestamp is not recorded anywhere yet, so there is no data to justify making this a gating target.
