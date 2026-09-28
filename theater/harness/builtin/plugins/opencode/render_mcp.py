"""OpenCode MCP configuration rendering."""

from __future__ import annotations

import json

from theater.harness.contracts.launch import McpRenderContext, McpRenderOverlay

from .dialect import v2_database_for_domain


def render_mcp_servers(context: McpRenderContext) -> McpRenderOverlay:
    """Merge local stdio endpoints into OpenCode's generated configuration."""
    try:
        config = json.loads(context.plan.files[context.config_path])
    except (KeyError, TypeError, ValueError) as exc:
        raise TypeError("OpenCode MCP renderer requires its generated configuration file") from exc
    if not isinstance(config, dict):
        raise TypeError("OpenCode MCP renderer requires a JSON object configuration")
    v2 = v2_database_for_domain(context.plan.transcript_domain) is not None
    servers = {}
    for server in context.servers:
        endpoint: dict[str, object] = {"type": "local"}
        if not v2:
            endpoint["enabled"] = True
        endpoint["command"] = [server.command, *server.args]
        # 2.x wraps MCP tools in Code Mode unless told otherwise; only its native
        # `mcp.servers` shape can say so (1.x's `enabled` became `disabled` there).
        if v2:
            endpoint["codemode"] = False
        if server.env:
            endpoint["environment"] = dict(server.env)
        servers[server.name] = endpoint
    config["mcp"] = {"servers": servers} if v2 else servers
    return McpRenderOverlay(files={context.config_path: json.dumps(config, indent=2)})
