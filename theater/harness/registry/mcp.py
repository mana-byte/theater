"""Core participant-scoped MCP endpoint construction."""

from __future__ import annotations

from typing import Literal

from theater.constants.harness import HARNESS_MCP_SERVER_NAME, HARNESS_MCP_WAIT_SERVER_NAME
from theater.harness.contracts.launch import theater_binary
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


__all__ = ["ErrorDelivery", "mcp_error_delivery", "theater_mcp_servers"]
