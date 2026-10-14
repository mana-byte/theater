"""Constants for the Pi frontend runtime."""

import re

PI_FRONTEND_QUALIFIED_FLOOR = (0, 80, 8)
PI_FRONTEND_QUALIFIED_MAX = (1, 2, 0)
# Broken published artifacts inside [FLOOR, MAX]: 0.85.0 crashes at import
# (no ModelRuntime/agent_settled); 1.0.1-1.1.0 import a codemode symbol that
# @earendil-works/pi-codemode 1.2.0 removed, so codemode fails to load.
PI_FRONTEND_QUALIFIED_EXCLUDED = (
    (0, 85, 0),
    (1, 0, 1),
    (1, 0, 2),
    (1, 0, 3),
    (1, 0, 4),
    (1, 1, 0),
)


def pi_version_text(version: tuple[int, int, int]) -> str:
    return ".".join(map(str, version))


_EXCLUDED_TEXT = ", ".join(pi_version_text(entry) for entry in PI_FRONTEND_QUALIFIED_EXCLUDED)
PI_FRONTEND_QUALIFIED_RANGE = (
    f">={pi_version_text(PI_FRONTEND_QUALIFIED_FLOOR)},"
    f"<={pi_version_text(PI_FRONTEND_QUALIFIED_MAX)}"
    f" (excluding {_EXCLUDED_TEXT})"
)
PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY = (
    f"pi-extension-{pi_version_text(PI_FRONTEND_QUALIFIED_FLOOR)}-"
    f"{pi_version_text(PI_FRONTEND_QUALIFIED_MAX)}-supported"
)
PI_FRONTEND_RUNTIME_PROBE_TIMEOUT_SECONDS = 5.0
PI_FRONTEND_CONTROL_TIMEOUT_SECONDS = 10.0
PI_FRONTEND_DIAGNOSTICS_MAX = 8
PI_FRONTEND_HISTORY_MAX = 64
PI_FRONTEND_CHANNEL_ID = "pi-frontend-live"
PI_FRONTEND_SEND_PROMPT_MAX_CHARS = 100_000

_VERSION_TOKEN = re.compile(
    r"(?<![\w.])(?P<version>(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+))(?![\w.-])"
)
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
