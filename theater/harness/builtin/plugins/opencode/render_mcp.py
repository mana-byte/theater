"""OpenCode MCP configuration rendering."""

from __future__ import annotations

import json

from theater.harness.contracts.launch import McpRenderContext, McpRenderOverlay


def render_mcp_servers(context: McpRenderContext) -> McpRenderOverlay:
    """Merge local stdio endpoints into OpenCode's generated configuration."""
    try:
        config = json.loads(context.plan.files[context.config_path])
    except (KeyError, TypeError, ValueError) as exc:
        raise TypeError("OpenCode MCP renderer requires its generated configuration file") from exc
    if not isinstance(config, dict):
        raise TypeError("OpenCode MCP renderer requires a JSON object configuration")
    servers = {}
    for server in context.servers:
        endpoint: dict[str, object] = {
            "type": "local",
            "enabled": True,
            "command": [server.command, *server.args],
        }
        if server.env:
            endpoint["environment"] = dict(server.env)
        servers[server.name] = endpoint
    config["mcp"] = servers
    return McpRenderOverlay(files={context.config_path: json.dumps(config, indent=2)})
