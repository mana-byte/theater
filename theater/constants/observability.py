"""Timing thresholds, observability defaults, and fixed limits."""

from __future__ import annotations

#: Default slow threshold for a generic operation; below this, timing logs DEBUG.
DEFAULT_SLOW_MS = 250.0

#: Slow threshold for a tmux command subprocess.
TMUX_MS = 100.0

#: Slow threshold for a git command subprocess.
GIT_MS = 200.0

#: Slow threshold for a proc ps/lsof subprocess.
PROC_MS = 50.0

#: Slow threshold for a worker task.
WORKERS_MS = 500.0

#: Past this, a readiness lag is not a spawn measurement: re-watched on restart.
READY_LAG_MAX_S = 60.0

#: Event-loop lag monitor sample interval (seconds).
LAG_INTERVAL_S = 0.5

#: Event-loop lag warning threshold (seconds).
LAG_WARN_S = 0.25

#: Supported OTLP transport protocols.
OTLP_PROTOCOL_GRPC = "grpc"
OTLP_PROTOCOL_HTTP = "http"
OTLP_PROTOCOLS = (OTLP_PROTOCOL_GRPC, OTLP_PROTOCOL_HTTP)

#: Default OTLP transport protocol.
DEFAULT_OTLP_PROTOCOL = OTLP_PROTOCOL_GRPC

#: Process roles attached to OTel resources.
PROCESS_ROLE_DAEMON = "daemon"
PROCESS_ROLE_MCP = "mcp"
PROCESS_ROLE_REGIE = "regie"
PROCESS_ROLES = (PROCESS_ROLE_DAEMON, PROCESS_ROLE_MCP, PROCESS_ROLE_REGIE)

#: Default service name for OTel resource attributes.
DEFAULT_SERVICE_NAME = "theater"

#: Default metric export / processor schedule interval (milliseconds).
DEFAULT_EXPORT_INTERVAL_MS = 5000

#: Default gauge sample interval (seconds).
DEFAULT_GAUGE_INTERVAL_S = 5.0

#: Default rotating log file size (bytes).
DEFAULT_LOG_MAX_BYTES = 10_485_760

#: Default rotating log backup count.
DEFAULT_LOG_BACKUP_COUNT = 3

#: Minimum log file size (bytes).
MIN_LOG_MAX_BYTES = 1024

#: Minimum export interval (milliseconds).
MIN_EXPORT_INTERVAL_MS = 100

#: Total raw stderr generations retained, including current.
STDERR_GENERATIONS = 3

#: Inactive régie pane generations retained in addition to protected groups.
REGIE_GENERATIONS = 3

#: Stderr token: number of random bytes; secrets.token_hex doubles to hex chars.
STDERR_TOKEN_BYTES = 6

#: Stderr token: expected hex character count.
STDERR_TOKEN_HEX_LEN = STDERR_TOKEN_BYTES * 2

#: Stderr token: collision retries before giving up.
STDERR_TOKEN_RETRIES = 3

#: Exporter/flush timeout (seconds).
EXPORT_TIMEOUT_S = 10.0

#: Span/log batch processor queue size.
BATCH_QUEUE_SIZE = 2048

#: Export batch size for span/log processors.
EXPORT_BATCH_SIZE = 512

#: Exponential histogram max bucket count.
HISTOGRAM_MAX_SIZE = 160

#: Exponential histogram max scale.
HISTOGRAM_MAX_SCALE = 20

#: Maximum exported error.type attribute length.
MAX_ERROR_TYPE_LEN = 128

#: Agent request duration histogram name.
AGENT_REQUEST_DURATION_METRIC = "theater.agent.request.duration"

#: Agent request time-to-first-token histogram name.
AGENT_REQUEST_TTFT_METRIC = "theater.agent.request.ttft"

#: Agent token counter name.
AGENT_TOKENS_METRIC = "theater.agent.tokens"

#: Agent cost counter name.
AGENT_COST_METRIC = "theater.agent.cost"

#: Agent tool duration histogram name.
AGENT_TOOL_DURATION_METRIC = "theater.agent.tool.duration"

#: Agent failure counter name.
AGENT_FAILURES_METRIC = "theater.agent.failures"

#: Agent terminal request counter name.
AGENT_REQUESTS_METRIC = "theater.agent.requests"

#: Agent terminal tool-call counter name.
AGENT_TOOL_CALLS_METRIC = "theater.agent.tool.calls"

#: Structured log event name for canonical agent trajectory records.
AGENT_TRAJECTORY_LOG_EVENT = "theater.agent.trajectory.record"

#: Completed model request trace span name.
AGENT_REQUEST_SPAN = "agent.request"

#: Completed tool operation trace span name.
AGENT_TOOL_SPAN = "agent.tool"

#: Maximum UTF-8 bytes exported in an opt-in canonical agent log body.
AGENT_LOG_BODY_MAX_BYTES = 16_384

#: Maximum UTF-8 bytes retained in an agent metric label.
AGENT_TELEMETRY_LABEL_MAX_BYTES = 120

#: Maximum distinct model labels admitted in one process.
AGENT_TELEMETRY_MODEL_CARDINALITY_LIMIT = 100

#: Maximum distinct tool labels admitted in one process.
AGENT_TELEMETRY_TOOL_CARDINALITY_LIMIT = 100

#: Maximum participant deduplication states retained in one process.
AGENT_TELEMETRY_PARTICIPANT_STATE_LIMIT = 128

#: Maximum emitted signal keys retained for one participant source epoch.
AGENT_TELEMETRY_EMITTED_SIGNAL_LIMIT = 256

#: Maximum record revisions retained for agent trajectory log deduplication.
AGENT_TELEMETRY_LOG_REVISION_LIMIT = 256

#: Maximum canonical metadata records retained for one participant source epoch.
AGENT_TELEMETRY_RECORD_SNAPSHOT_LIMIT = 256

#: Maximum emitted request and tool contexts retained for one source epoch.
AGENT_TELEMETRY_SPAN_CONTEXT_LIMIT = 256

#: Label for an absent or invalid agent telemetry value.
AGENT_TELEMETRY_UNKNOWN_LABEL = "unknown"

#: Label for a value first seen after its cardinality limit.
AGENT_TELEMETRY_OTHER_LABEL = "other"

#: Token counter label for prompt input tokens.
AGENT_TOKEN_KIND_INPUT = "input"

#: Token counter label for generated output tokens.
AGENT_TOKEN_KIND_OUTPUT = "output"

#: Token counter label for reasoning output tokens.
AGENT_TOKEN_KIND_REASONING = "reasoning"

#: Token counter label for cache-read input tokens.
AGENT_TOKEN_KIND_CACHE_READ = "cache_read"

#: Token counter label for cache-write input tokens.
AGENT_TOKEN_KIND_CACHE_WRITE = "cache_write"

#: Request or tool result label for successful completion.
AGENT_RESULT_SUCCESS = "success"

#: Request or tool result label for an error terminal state.
AGENT_RESULT_ERROR = "error"

#: Request or tool result label for a timeout terminal state.
AGENT_RESULT_TIMEOUT = "timeout"

#: Request or tool result label for a cancelled terminal state.
AGENT_RESULT_CANCELLED = "cancelled"

#: Request or tool result label for an interrupted terminal state.
AGENT_RESULT_INTERRUPTED = "interrupted"

#: Observable gauge for non-dead participants.
PARTICIPANTS_LIVE_GAUGE = "theater.participants.live"

#: Observable gauge for physically addressable participants.
PARTICIPANTS_ADDRESSABLE_GAUGE = "theater.participants.addressable"

#: Observable gauge for running jobs.
JOBS_ACTIVE_GAUGE = "theater.jobs.active"

#: Gauge names registered against the shared cache.
GAUGE_NAMES = (
    PARTICIPANTS_LIVE_GAUGE,
    PARTICIPANTS_ADDRESSABLE_GAUGE,
    JOBS_ACTIVE_GAUGE,
)

#: Derived gRPC collector endpoint when none is configured.
DERIVED_GRPC_ENDPOINT = "http://localhost:4317"

#: Derived HTTP collector endpoint when none is configured.
DERIVED_HTTP_ENDPOINT = "http://localhost:4318"
