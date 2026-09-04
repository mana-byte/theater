"""Core participant-scoped MCP endpoint construction."""

from __future__ import annotations

from theater.constants.harness import HARNESS_MCP_SERVER_NAME, HARNESS_MCP_WAIT_SERVER_NAME
from theater.harness.contracts.launch import theater_binary
from theater.mcp_plugins import McpServerSpec


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


__all__ = ["theater_mcp_servers"]
