"""Vibe MCP environment rendering."""

from __future__ import annotations

import json

from theater.constants.harness import HARNESS_MCP_TOOL_TIMEOUT_SECONDS
from theater.harness.contracts.launch import McpRenderContext, McpRenderOverlay

from .constants import VIBE_MCP_SERVERS_ENV


def render_mcp_servers(context: McpRenderContext) -> McpRenderOverlay:
    """Set Vibe's complete launch-local stdio server catalogue."""
    servers = []
    for server in context.servers:
        endpoint: dict[str, object] = {
            "name": server.name,
            "transport": "stdio",
            "command": server.command,
            "args": list(server.args),
            # Vibe's 60s default cuts off the daemon's full await ceiling.
            "tool_timeout_sec": HARNESS_MCP_TOOL_TIMEOUT_SECONDS,
        }
        if server.env:
            endpoint["env"] = dict(server.env)
        servers.append(endpoint)
    return McpRenderOverlay(env={VIBE_MCP_SERVERS_ENV: json.dumps(servers)})
