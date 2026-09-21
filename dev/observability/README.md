# Local Theater observability

An optional development stack, independent of Theater's runtime packages.
The pinned `grafana/otel-lgtm` image contains a collector, Grafana, Prometheus,
Tempo, Loki, and an unused Pyroscope backend. No cloud exporter is configured.

## Start and connect

From the Theater repository root, with Docker running:

```sh
docker compose -f dev/observability/compose.yaml up -d --wait
uv sync --extra observability
```

Merge these keys into `$THEATER_HOME/config.toml` (normally
`~/.theater/config.toml`); do not create a second `[observability]` table:

```toml
[observability]
otlp_enabled = true
otlp_protocol = "http"
otlp_endpoint = "http://127.0.0.1:4318"
agent_log_content = false
```

Then apply the settings:

```sh
uv run theater restart
uv run theater providers list
uv run python dev/observability/smoke.py
```

The smoke check makes four read-only `list_models` MCP calls through a fresh
stdio server. It verifies that process's histogram and logs plus the actual
MCP → RPC client → daemon parent/child trace. It neither registers a participant
nor sends input to an agent. It is a connectivity check, not a latency benchmark.

Check storage separately after at least 30 minutes of telemetry:

```sh
uv run python dev/observability/health.py
```

This read-only check requires historical search **and** trace-ID retrieval beyond
Tempo's live-store window. An empty result is inconclusive, never a healthy result.
A green container healthcheck and recent traces do not prove historical indexing.
If this check fails on an established stack, inspect Tempo logs for tenant-index
errors. Preserve a backup before quarantining individually verified incomplete
blocks; never reset the data volume as a repair. The check does not repair data.

Open [the Theater dashboard](http://127.0.0.1:3000/d/theater-local).
Viewing requires no login; local administration uses the image's development
credentials, `admin` / `admin`. All published ports bind to `127.0.0.1`:
Grafana 3000, OTLP gRPC 4317, and OTLP HTTP 4318. Do not expose these unauthenticated
development endpoints through a tunnel or public interface. Host processes and
containers on the Compose network can access them.

## What is covered

- Daemon RPC handlers, event-loop lag, lifecycle stages, cached state gauges,
  and accepted agent telemetry start exporting after the daemon restart.
- MCP dispatch and client timings start exporting when each harness starts a
  **new Theater MCP process**. Existing MCP processes keep their old config;
  a daemon restart does not update them. No agent panes need to be killed.
- Fast timing logs remain opt-in (`mcp_timing = true` or `theater mcp --timing`);
  metrics and trace timings do not require verbose logs. The smoke process uses
  `--timing` to verify log transport without changing the global setting.
- Régie currently has local rotating logs and API-only trace instrumentation,
  but no exporter lifecycle. This stack does not give complete UI or
  harness-internal timings. Theater-observed agent telemetry is not a substitute
  for instrumentation inside a harness.

The dashboard uses native Prometheus histograms, in milliseconds. Daemon,
client, and MCP durations are nested: do not add them. MCP dispatch excludes
harness scheduling, history persistence, UI work, and stdio delivery.
`jobs.await` has a separate metric because waiting is intentional.

Detached-native spawn phases now separate `native_backend` (including stdout endpoint
discovery), `native_endpoint` (Unix readiness only), `native_runtime`,
`native_session` (open/bind/live registration), and `native_frontend_plan`.
They remain nested under the full `spawn.launch` lifecycle stage. Terminal
creation is named `spawn.terminal` in logs/traces; its historical metric name
`theater.spawn.launch.duration` is retained. Do not average or add the full
launch and terminal-create durations as if they were independent operations.

Régie's local action logs separate `snapshot`, `unmanaged`, and `projection`,
correlated by operation ID. The final `rendered` marker includes
`after_projection_ms`: elapsed time from projection completion to Textual's
after-refresh callback, including scheduling, not a pure paint/CPU measurement.
The UI reconciliation order is unchanged. These markers require reopening
Régie; native-stage instrumentation requires a daemon restart.

Régie's local `startup.*` logs also separate initial read phases from
`first_frame`, `participants_ready`, and `ready` after-refresh milestones.
Milestones start at app construction; imports, CLI preflight, and reveal-animation
completion are outside their scope. Parallel phases overlap: do not sum them.
`ready` includes recoverable read failures and does not assert service health.
These local logs are not exported by the observability stack.

In **Explore → Tempo**, search `{ resource.service.name = "theater" }` or open
the smoke check's trace ID. On a client span, inspect `theater.lock_wait_ms`,
`theater.connect_ms`, and `theater.roundtrip_ms`; `theater.call_id` correlates
the call. Logs and traces link through their trace ID. These identifiers are
not metric labels. The SDK supplies a process instance ID to keep simultaneous
MCP counters separate.

New MCP processes use at most four independent private RPC connections. The
`rpc.pool_wait` span and `theater.rpc.pool.wait.duration` measure admission when
all are occupied; each connection still admits one exchange, with no automatic
replay. Public frontend and provider channels remain connection-scoped.

Tempo's trace duration is the envelope of **all** spans, not one tool's duration.
For caller latency, inspect the `tools/call <tool>` span itself. Background runtime health
monitors now start independent traces; old traces can contain later reconnects.
Theater timing spans expose `theater.duration_ms` (monotonic),
`theater.wall_duration_ms`, and `theater.clock_gap_ms`. Large divergence sets
`theater.clock_discontinuity`: suspend or a clock adjustment is possible, not
proven synchronous blocking. Raw OTel timestamps stay unchanged. macOS monotonic
timing excludes sleep; other platforms may include it, so compare system sleep
history too. Event-loop warnings report delayed wakeups without asserting a cause.

`theater.gc.phase.duration` separates journal, checkpoint, and other retention
phases; journal spans include scanned/deleted row counts. Journal selection is
bounded and checks the oldest row first. Background checkpoints run PASSIVE on
a worker-owned connection, never wait for readers, and report WAL frame progress;
they do not truncate the file. `theater.usage.summary.duration` separates exact
cache hits from aggregate scans. Inserts, crossed timestamp boundaries, and
timezone changes invalidate summary reuse.

Signals export asynchronously (normally every five seconds). Tempo's search
index can lag direct trace-ID lookup. Rate/percentile panels need at least two
exports; empty panels mean no samples, not zero latency. Lifecycle panels fill
when real operations occur. A renamed `service_name` requires adapting the
dashboard's `service_name="theater"` filters.

Prompt/result content remains disabled in agent logs. Telemetry still contains
operational metadata, identifiers, and some diagnostic paths; treat local
storage as private. The upstream image can download Grafana plugins on startup.

## Resources and shutdown

The container is limited to two CPUs, 3 GiB RAM, and 512 processes. The collector
has a 256 MiB memory limiter and bounded export queues. Docker stdout logs rotate
at 10 MiB × three files. Metrics, traces, and logs have 24-hour retention;
Prometheus also has a 512 MB block-retention target. Retention cleanup is
asynchronous, and WALs/active blocks add overhead: this is **not a hard disk
quota**. Named volumes persist data, Grafana settings, and Tempo's runtime
trace files across container recreation.

```sh
docker compose -f dev/observability/compose.yaml ps
docker compose -f dev/observability/compose.yaml logs --tail 80
docker compose -f dev/observability/compose.yaml exec -T lgtm du -sh /data
docker compose -f dev/observability/compose.yaml exec -T lgtm du -sh /var/tempo
docker stats --no-stream theater-observability-lgtm-1
```

To stop collecting, set `otlp_enabled = false`, restart Theater, and let existing
MCP processes pick up that setting on their next harness-managed start. Then:

```sh
docker compose -f dev/observability/compose.yaml stop
```

`up -d --wait` resumes the stack. `down` removes only this project's container
and network and retains the data volumes; adding `--volumes` **deletes all local
telemetry and Grafana settings**. Stopping the collector first is safe for
orchestration, but still-enabled exporters retry, log failures, and eventually
drop telemetry. The stack has no access to Theater's SQLite database or terminals.
