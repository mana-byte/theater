"""Claude Code MCP configuration rendering."""

from __future__ import annotations

import json

from theater.harness.contracts.launch import McpRenderContext, McpRenderOverlay


def render_mcp_servers(context: McpRenderContext) -> McpRenderOverlay:
    """Write Claude's launch-local stdio MCP configuration."""
    servers = {}
    for server in context.servers:
        endpoint: dict[str, object] = {"command": server.command, "args": list(server.args)}
        if server.env:
            endpoint["env"] = dict(server.env)
        servers[server.name] = endpoint
    return McpRenderOverlay(
        files={context.config_path: json.dumps({"mcpServers": servers}, indent=2) + "\n"}
    )
