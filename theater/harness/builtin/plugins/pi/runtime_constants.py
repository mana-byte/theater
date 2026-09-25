"""Constants for the Pi frontend runtime."""

import re

PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY = "pi-extension-0.84.x-supported"
PI_FRONTEND_RUNTIME_PROBE_TIMEOUT_SECONDS = 5.0
PI_FRONTEND_CONTROL_TIMEOUT_SECONDS = 10.0
PI_FRONTEND_DIAGNOSTICS_MAX = 8
PI_FRONTEND_HISTORY_MAX = 64
PI_FRONTEND_CHANNEL_ID = "pi-frontend-live"
PI_FRONTEND_SEND_PROMPT_MAX_CHARS = 100_000

_VERSION_TOKEN = re.compile(r"(?<![\w.])(?P<version>0\.(?P<minor>\d+)\.(?P<patch>\d+))(?![\w.-])")
_REJECTED_SETTINGS_ERRORS = frozenset(
    {
        "busy",
        "invalid_request",
        "model_update_proof_gated",
        "model_unavailable",
        "not_ready",
        "unsupported_thinking",
        "wrong_session",
    }
)
# A refused admission never delivered the prompt.  session_changed is
# UNKNOWN too: the bridge also returns it after delivery.
_REJECTED_SEND_ERRORS = frozenset(
    {
        "busy",
        "invalid_request",
        "not_ready",
        "operation_capacity",
        "operation_in_progress",
        "prompt_too_large",
        "wrong_session",
    }
)

# Bridge codes that are provably emitted before the single abort mutation.
# Everything else (timeout, malformed frame, transport loss, post-call drift)
# decodes UNKNOWN and is never retried or replayed through tmux.
_REJECTED_INTERRUPT_ERRORS = frozenset(
    {
        "invalid_request",
        "not_ready",
        "no_active_run",
        "not_cancellable",
        "operation_capacity",
        "operation_in_progress",
        "stale_bridge",
        "stale_turn",
        "wrong_session",
    }
)
