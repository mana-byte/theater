"""Pure prompt and control facts shared by OpenCode's distinct runtimes."""

from __future__ import annotations

from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ConnectionHealth,
    RuntimeCapabilities,
    RuntimeCapability,
)

from .http import OpenCodeHttpError

_PROMPT_MAX_CHARS = 60_000
_PROMPT_MAX_BYTES = 60_000


def valid_prompt(prompt: object) -> bool:
    return (
        isinstance(prompt, str)
        and bool(prompt.strip())
        and len(prompt) <= _PROMPT_MAX_CHARS
        and len(prompt.encode("utf-8")) <= _PROMPT_MAX_BYTES
    )


def send_capabilities(health: ConnectionHealth, has_session: bool) -> RuntimeCapabilities:
    reasons: dict[RuntimeCapability, CapabilityUnavailableReason] = dict.fromkeys(
        (
            RuntimeCapability.INTERRUPT,
            RuntimeCapability.STEER,
            RuntimeCapability.SETTINGS_UPDATE,
        ),
        CapabilityUnavailableReason.THEATER_POLICY,
    )
    controls = (RuntimeCapability.SEND, RuntimeCapability.QUEUE_FOLLOWUP)
    if health is ConnectionHealth.CONNECTED and has_session:
        return RuntimeCapabilities(available=frozenset(controls), unavailable_reasons=reasons)
    reason = (
        CapabilityUnavailableReason.GATED_BY_BACKEND
        if health in {ConnectionHealth.UNOPENED, ConnectionHealth.DISCONNECTED}
        else CapabilityUnavailableReason.SESSION_STATE
    )
    reasons.update(dict.fromkeys(controls, reason))
    return RuntimeCapabilities(available=frozenset(), unavailable_reasons=reasons)


def server_http_refusal(exc: OpenCodeHttpError, what: str) -> tuple[str, str] | None:
    if not exc.written:
        return "server_refused", f"the OpenCode server refused the {what} before delivery ({exc})"
    status = exc.status
    if status is not None and 400 <= status < 500:
        code = "invalid_session" if status == 404 else "server_refused"
        return code, f"the OpenCode server refused the {what} with HTTP {status} ({exc.reason})"
    return None
