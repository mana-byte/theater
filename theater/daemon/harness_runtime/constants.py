"""Generic engine-only constants for the shared harness runtime.

These bounds belong to the WebSocket-over-Unix transport and the detached
backend process owner introduced by the runtime engine. They are deliberately
kept inside this narrowly named package: composition waves may promote them
into ``theater.constants`` once their values are observed against real
backends, but the frozen Wave 1 contract constants are unaffected.
"""

from __future__ import annotations

#: Maximum payload of one WebSocket frame we accept from or send to a backend.
RUNTIME_WS_MAX_FRAME_BYTES = 1_048_576

#: Maximum assembled WebSocket message size, enforced across fragments.
RUNTIME_WS_MAX_MESSAGE_BYTES = 4_194_304

#: Bounded notifications buffer; overflow degrades observation and is surfaced,
#: never silently discarded and never used to invent completion.
RUNTIME_WS_RECEIVE_QUEUE_MAX = 128

#: Maximum requests awaiting a reply on one connection; beyond this a caller
#: fails fast instead of piling unbounded in-flight state onto the backend.
RUNTIME_WS_MAX_OUTSTANDING_REQUESTS = 16

#: How long aclose() waits for the peer's close frame before dropping the
#: transport; closing stays deterministic regardless of a silent backend.
RUNTIME_WS_CLOSE_HANDSHAKE_TIMEOUT_SECONDS = 2.0

#: Host header sent during the HTTP Upgrade handshake.
RUNTIME_WS_HANDSHAKE_HOST = "theater-runtime"

#: Interval between endpoint reachability polls while a detached backend boots.
RUNTIME_ENDPOINT_POLL_INTERVAL_SECONDS = 0.05

#: Grace period between SIGTERM and SIGKILL when tearing a backend down.
RUNTIME_BACKEND_TERMINATE_GRACE_SECONDS = 5.0

#: How long SIGKILL is allowed to take before teardown reports failure.
RUNTIME_BACKEND_KILL_WAIT_SECONDS = 5.0

#: Interval between backend liveness polls during teardown.
RUNTIME_BACKEND_POLL_INTERVAL_SECONDS = 0.05

#: Interval between local snapshot health polls of one installed runtime.
#: The monitor never polls from an exporter thread and never spins: each
#: iteration reads the runtime's own in-memory snapshot and acts only on an
#: explicit DISCONNECTED health.
RUNTIME_RECOVERY_POLL_SECONDS = 0.5

#: Bounded delay before a failed same-runtime recovery attempt is retried.
#: A failed attempt never hot-loops and never fans out tasks: the monitor
#: retries after this delay until the generation changes or the runtime is
#: closed, torn down, or the daemon shuts down.
RUNTIME_RECOVERY_RETRY_SECONDS = 1.0


__all__ = [
    "RUNTIME_BACKEND_KILL_WAIT_SECONDS",
    "RUNTIME_BACKEND_POLL_INTERVAL_SECONDS",
    "RUNTIME_BACKEND_TERMINATE_GRACE_SECONDS",
    "RUNTIME_ENDPOINT_POLL_INTERVAL_SECONDS",
    "RUNTIME_RECOVERY_POLL_SECONDS",
    "RUNTIME_RECOVERY_RETRY_SECONDS",
    "RUNTIME_WS_CLOSE_HANDSHAKE_TIMEOUT_SECONDS",
    "RUNTIME_WS_HANDSHAKE_HOST",
    "RUNTIME_WS_MAX_FRAME_BYTES",
    "RUNTIME_WS_MAX_MESSAGE_BYTES",
    "RUNTIME_WS_MAX_OUTSTANDING_REQUESTS",
    "RUNTIME_WS_RECEIVE_QUEUE_MAX",
]
