"""Core participant-scoped MCP endpoint construction."""

from __future__ import annotations

import math
import os
from typing import Literal

from theater.constants.harness import HARNESS_MCP_SERVER_NAME, HARNESS_MCP_WAIT_SERVER_NAME
from theater.harness.contracts.launch import theater_binary
from theater.harness.contracts.manifest import McpForegroundWait
from theater.mcp_plugins import McpServerSpec

ErrorDelivery = Literal["is_error", "result"]


def theater_mcp_servers(participant_id: str, harness: str) -> tuple[McpServerSpec, McpServerSpec]:
    """Build Theater's isolated control and wait stdio endpoints."""
    executable = theater_binary()
    base = ("mcp", "--id", participant_id, "--harness", harness)
    return (
        McpServerSpec(
            name=HARNESS_MCP_SERVER_NAME,
            command=executable,
            args=(*base, "--toolset", "control"),
        ),
        McpServerSpec(
            name=HARNESS_MCP_WAIT_SERVER_NAME,
            command=executable,
            args=(*base, "--toolset", "wait"),
        ),
    )


def mcp_error_delivery(harness: str) -> ErrorDelivery:
    """The refusal delivery mode the named harness's manifest declares; unknown means is_error."""
    from theater.harness.registry.lookup import HARNESSES, normalize

    found = HARNESSES.get(normalize(harness))
    mode = getattr(found, "mcp_error_delivery", "is_error")
    return "result" if mode == "result" else "is_error"


def _declared_ms(raw: str | None, default_ms: float) -> float:
    """A missing, unparsable or nonfinite env value falls back to the declared default."""
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return default_ms
        if math.isfinite(value):
            return value
    return default_ms


def mcp_foreground_ceiling(harness: str) -> float | None:
    """The await ceiling the named harness's foreground-wait declaration puts on this client."""
    from theater.harness.registry.lookup import HARNESSES, normalize

    wait = getattr(HARNESSES.get(normalize(harness)), "mcp_foreground_wait", None)
    if not isinstance(wait, McpForegroundWait):
        return None  # unknown or undeclared: this client is not known to background calls
    ms = _declared_ms(os.environ.get(wait.env), wait.default_ms)
    if ms <= 0:
        return None  # a valid zero or negative explicitly disables the ceiling
    ceiling = max(ms / 1000 - wait.margin_seconds, 0.0)
    # A malformed declaration degrades to no foreground wait, never an unbound one.
    return ceiling if math.isfinite(ceiling) else 0.0


__all__ = ["ErrorDelivery", "mcp_error_delivery", "mcp_foreground_ceiling", "theater_mcp_servers"]
