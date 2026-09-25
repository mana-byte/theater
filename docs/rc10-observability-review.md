# RC10 observability and behaviour review — 21 September 2026

## Scope and confidence

Initial read-only inspection of the local Grafana stack, daemon/Régie logs, and
code, followed by approved fixes. The staged-kill workflow, notification
presentation, Pi live observation, and three error classifications are fixed in
the checkout. Historical Tempo indexing was recovered without a restart. No live
participant controls or daemon/bridge restarts were performed during this review
or its follow-up. Outstanding items remain explicitly marked below.

The metric window is 10:08–18:08 Europe/Paris. Counts use Prometheus `increase`
and are approximate; they cover the available instances, not one frontend.
Sparse lifecycle samples are examples, not reliable performance baselines.
Additional logs through 18:30 were inspected. Historical trace coverage was
broken during that inspection, so its empty error searches do **not** establish
an error-free daemon. Historical retrieval was verified after recovery at 18:41.

## Implemented in this review

- Régie unstages only the selected participant's matching terminal identity before
  requesting termination. Release is serialized with staging; failure refuses the
  request. No daemon presence exemption or agent self-kill permission was added.
- Routine successful actions and background catalog/bus refresh failures no longer
  produce toasts. Stale-state refresh errors stay in logs; initial state failure
  remains visible. Action failures and uncertain outcomes still notify, once per
  changed outcome instead of twice through competing render paths.
- Verification: 534 Régie tests and 161 daemon/control/presence tests passed,
  including isolated real-tmux presentation checks. Scoped lint/format checks and
  type checking of Theater plus Régie passed. Reopen the Régie UI to load these
  changes; they do not require a daemon or bridge restart.

RC9 also required human absence for kills and prohibited agent self-kill. The
staged-pane fix restores a usable operator workflow without claiming that RC9
had a blanket operator exemption. Another viewer or unknown presence still blocks
termination, across harnesses.

## Approved follow-up results

- Tempo's verified empty block was backed up and quarantined, recoverably. Its
  next tenant-index poll succeeded at 18:41:46 Europe/Paris; backend block count
  recovered to 363. Three previously unreadable historical trace IDs returned
  HTTP 200, with 23, 1, and 1 spans respectively. No trace payload was deleted.
- Pi live observation now distinguishes an 8 MiB raw-record limit from a 256 KiB
  projected-record limit. Projection removes images and opaque signatures while
  retaining tool identities/results, lifecycle markers, timestamps, and usage.
  Text previews retain both ends with explicit omission markers (64 KiB per
  field, 128 KiB content budget per record, excluding structural overhead).
- JSONL framing preserves skipped-record order and original byte offsets;
  checkpoints never use projected sizes. Large partial records and queued batches
  request immediate catch-up instead of waiting another polling interval.
  Live parsing runs off-loop with private immutable input state, so cancelled
  workers cannot advance the live observer. Original transcripts and full-history
  parsing are unchanged; previously skipped accounting is not backfilled.
- Normal cancellation, an empty `ps` no-match result, and explicitly expected
  branch absence no longer mark spans ERROR. Cancellation remains its own outcome;
  diagnostic output, timeouts, and unexpected git failures remain errors.
- `uv run python dev/observability/health.py` checks historical search and direct
  retrieval outside the live-store window. It is read-only and passes against the
  recovered stack. Empty history is inconclusive/failure, never healthy. This is
  an on-demand check, not an automatic alert or a corruption-prevention mechanism.

At that handoff, the Pi and error-classification fixes required a daemon restart
to activate; services were left running to preserve active work. The full suite passed (5,035 tests,
22 skipped), followed by 250 passing affected tests after final refinements.
Global Ruff lint/format checks, mypy (697 source files), and `git diff --check`
passed. An additional in-memory check verified framing and offsets across 1,000
deterministic chunk/record-size combinations without touching live transcripts.

## Spawn-latency follow-up

The latest correlated OpenCode spawn at 19:03 took 1,723 ms to be observed complete
by Régie and another 204 ms to reach its final render marker (1,927 ms total).
The paired kill took 287 ms plus 110 ms (397 ms total). Across three logged
OpenCode spawns, totals were 1.80–2.06 seconds; five kills took 338–525 ms.
These action timers exclude directory input and pre-request staged-pane release.
The final reconciliation marker is not necessarily the first visible row change.

The spawn trace separates 82 ms of Git workspace inspection, 497 ms of native
qualification, approximately 928 ms before terminal creation in native startup
and session setup, and 145 ms in terminal creation. The kill trace shows 66 ms
presence admission, 125 ms provider termination, 67 ms native shutdown, and 13 ms
retirement. These samples do not establish a baseline for other harnesses.

Approved changes in the checkout:

- OpenCode's independent `--version` and `serve --help` probes run concurrently
  in a scoped two-worker pool. Both remain mandatory, fresh, and bounded by their
  existing per-command timeout. Qualification still requires successful exits,
  the pinned release range, and both required flags. The pool joins both probes
  even when one fails; launch admission does not reuse the display cache.
- Three read-only benchmark pairs against OpenCode 1.18.29 measured sequential
  probes at 522–542 ms and the implemented concurrent probe at 278–285 ms: a
  median saving of 254 ms. This is a probe benchmark, not a new end-to-end spawn
  measurement. No participant was created or controlled for the benchmark.
- Detached-native startup instrumentation separates backend launch, endpoint readiness,
  runtime creation, exact-session binding, and frontend planning, without
  reordering any launch step. Terminal creation is now `spawn.terminal` in
  logs/traces, distinct from the enclosing `spawn.launch`; its metric name is
  retained for compatibility.
- Régie logs snapshot, unmanaged-pane discovery, projection, and the interval
  until its after-refresh callback independently. No UI ordering, safety checks,
  snapshot policy, or presentation behavior was changed in this follow-up.

Verification: all 175 affected tests passed, including native-launch cancellation
and cleanup, compatibility failures, and UI reconciliation. Global Ruff lint and
format checks, mypy (698 source files), and `git diff --check` passed. The full
repository suite was not rerun for this follow-up.

The daemon changes were activated with an approved restart on September 21 at
19:41 Europe/Paris. All three live participants retained their session IDs and
addressability; terminal pane IDs and processes were unchanged. The existing
bridge reconnected without restarting, and Régie recovered its live tree after
two transient connection-loss warnings. Reopen Régie to load its new local phase
logs. Remaining native-startup and UI optimization should use those measured
phases; end-to-end gains have not yet been measured after activation.

## Prioritized improvements and remaining backlog

| Priority | Improvement | Evidence and next step |
| --- | --- | --- |
| P0 | Recover historical trace indexing | Recovered: the verified empty block is backed up and quarantined; historical search and lookup work again. The original incomplete-write cause remains unknown. |
| P1 | Preserve valid bulky Pi records | Fixed in the checkout: bounded raw framing and semantic projection retain observation metadata, original checkpoints, and immediate catch-up. No historical backfill was performed. |
| P1 | Separate expected outcomes from failures | Fixed for disappeared processes, expected branch absence, and cancellation. Recoverable resnapshot classification remains outstanding; retain real failures and explicit refusal reasons. |
| P1 | Reduce idle frontend polling | Approximately 53,002 bus reads and 21,206 state-follow calls. Régie asks state follow with `wait_seconds=0`; animation and visible-bus consumers have separate polls. Use a dedicated wait-capable state connection and shared bounded event ingestion without conflating independent cursors. |
| P1 | Reduce completion-to-render delay | Phase instrumentation added for snapshot, unmanaged discovery, projection, and after-refresh scheduling. The UI path is unchanged; collect these phases before optimizing reconciliation. |
| P2 | Reduce repeated process discovery | Fixed: exact Codex process proof reuses its costly `lsof` result while PID plus start identity hold; single-PID checks retain cheap `ps -p`, and table snapshots remain opt-in for real multi-PID sweeps. |
| P2 | Optimize spawn critical path | OpenCode's fresh compatibility probes now run concurrently: 254 ms median saving in a three-pair read-only benchmark. Native-startup subphases are instrumented; complete-launch versus terminal-create spans are separately named. End-to-end gains remain to be measured after activation. |
| P2 | Optimize kill provider work | Approximate phase means: presence 72 ms, provider 122 ms, retirement 14 ms; lock wait is negligible in this small sample. Coalesce safe inventory reads while retaining final presence and occupant checks at execution. |
| P2 | Investigate post-wake scheduling spikes | Loop-lag p99 is 36.8 ms, but an 18:00:59 wake was delayed 942 ms with essentially no wall/monotonic gap. Correlate scheduling and worker queues before assigning blame to synchronous code. |
| P2 | Avoid obsolete observer rebuilds | Logs reopen opencode observation after explicit kill. Live-unregistration schedules a restart; `_start_watch` lacks a dead-status check. Verify teardown races and final-evidence draining before suppressing obsolete restarts. Reducer status settlement already refuses dead participants. |
| P2 | Correct readiness timing on observer restarts | `_start_watch` records time since participant creation on every restart. A teardown-time rebuild produced a 36.35-second `observer.watch` sample; that is not 36 seconds of startup work. Separate initial readiness from reattachment timing. |
| P2 | Tighten usage/state projection work | Usage-by-harness p95 is 145 ms but only about five calls; state-follow p95 is 45 ms with a much larger sample. Profile DB reads, projection, serialization, and validation before changing caching or indexes. |
| P2 | Make observability health meaningful | Read-only persistent-history check added and verified. Automatic indexing/export alerts, wait-vs-work views, and explicit process/instrumentation coverage remain outstanding. |

P0 is an observability integrity issue, not evidence of lost participant work.
Pi's source files also remain intact; the loss is in Theater's projected observations.

## Historical-trace failure details

Tempo is configured for 24-hour retention. At 18:28, an eight-hour error query
returned no traces and inspected zero backend blocks. Previously retrieved morning
trace IDs returned HTTP 404. Recent traces had still been visible through live
storage, explaining the misleadingly short apparent retention.

The recurring poll error is:

```text
failed to poll tenant blocks: failed reading unknown blocks: unexpected end of JSON input
```

The exact affected directory inside the local stack container is:

```text
/data/tempo/blocks/single-tenant/f9460128-f2e4-4214-9ea2-942df2b5d34c
```

Its `meta.json`, `data.parquet`, `index`, and `bloom-0` were all zero bytes and
unchanged for hours. Their matching backup inventory was verified before moving
the exact block. Recoverable copies remain inside the stack container's data
volume:

```text
/data/theater-tempo-recovery-w3bpJs/backup
/data/theater-tempo-recovery-w3bpJs/quarantined
```

Other blocks were left untouched. No deletion or restart was needed. The original
cause of the incomplete write is not established. Known historical trace IDs
`5f3d6a718ec2faef36a53f3e80c4d8a3`, `87e7569e3648562412bd69b000231131`, and
`84b1c9a176920bf8c8fe4036b4180a90` became readable again. Continue checking
historical storage independently; a green container healthcheck is insufficient.

Collector counters also show about 3,041 failed metric-point export attempts and
13 failed log-record attempts in the metric window. Retries may recover these;
they are not proof of that many permanently lost records. Trace receiver refusals
were zero in the available series. Add exporter retry/drop visibility separately
from Tempo's indexing-health checks.

## Error classification and source ownership

- Fixed — `proc.py`: the 18:04:55 `proc.ps-comm` error queried the PID of a participant
  being terminated at the same time. `comm()` deliberately treats disappeared
  processes as normal, but the timed subprocess exception still marks the span
  erroneous. Only exit 1 without output or diagnostics is now treated as absence;
  timeouts, missing binaries, and other inspection failures remain errors.
- Fixed — `daemon/worktrees/repository.py`: `_git` marked every nonzero result erroneous.
  The unique-worktree creation path deliberately probes a not-yet-existing branch.
  That call site now uses `show-ref --verify --quiet` with explicitly expected
  codes 0 and 1; other git failures are not suppressed.
- Fixed — `observability/engine.py`: cancellation keeps `result=cancelled` without
  marking the span ERROR. Genuine errors retain their status.
- Pending — `frontend/state_sync.py`: `resnapshot_required` is automatically recoverable.
  Separate routine recovery from malformed requests and persistent disconnects.
- Pi agent-tool failures, such as a PDF validation command exiting 1, belong to
  the agent trajectory. They must remain visible without counting as Theater
  daemon failures in dashboards.

Pi examples include valid records around 304/309 KB containing image results and
a roughly 69 KB successful text tool result. Errors recurred at 17:23:31 and
17:24:05. Read-only replay of those examples through the new projection/parser
retained their events and facts. Tests cover bulky records, malformed oversized
input, tool-result correlation, usage, turn completion, worker cancellation,
catch-up, and source offset/checkpoint correctness across rollback and restart.

## Representative RPC measurements

| RPC | Approx. calls | Mean | p95 |
| --- | ---: | ---: | ---: |
| bus tail | 53,002 | 1.0 ms | 1.8 ms |
| state follow | 21,206 | 23.3 ms | 44.9 ms |
| plugin call | 3,522 | 1.6 ms | 2.9 ms |
| usage summary | 2,119 | 8.3 ms | 65.7 ms |
| provider report | 1,136 | 10.3 ms | 23.1 ms |
| state snapshot | 8 | 36.9 ms | 87.8 ms |
| usage by harness | 5 | 67.4 ms | 145.4 ms |
| read transcript | 5 | 8.0 ms | 20.2 ms |

These do not support a blanket 50 ms daemon-processing floor. Trajectory follow
averages 16.1 seconds because it intentionally waits for updates. Termination's
4.5 ms admission is likewise not its end-to-end latency. Nested lifecycle stages
must not be summed without checking their parent/child relationships.

The 17:47 and 18:00 clock-gap warnings include roughly 16- and 13-minute wall/monotonic
discontinuities, consistent with suspend or clock changes rather than sustained
event-loop blocking. Keep those separate from genuine wake-delay samples.

Worker timing currently wraps submission through completion, including executor
queueing. Add queue-wait versus execution measurements before interpreting worker
durations as CPU work. Régie's render logs already have operation IDs; export them
through a separate process-owned observability lifecycle rather than importing
daemon internals. Keep harness parsing, daemon policy, provider effects, and UI
presentation in their existing owning packages.
