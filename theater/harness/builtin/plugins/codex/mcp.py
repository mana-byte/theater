"""Codex MCP configuration rendering."""

from __future__ import annotations

import json

from theater.harness.contracts.launch import McpRenderContext, McpRenderOverlay


def render_mcp_servers(context: McpRenderContext) -> McpRenderOverlay:
    """Append Codex's TOML configuration overrides for each stdio endpoint."""
    argv: list[str] = []
    for server in context.servers:
        prefix = f"mcp_servers.{server.name}"
        argv += [
            "-c",
            f"{prefix}.command={json.dumps(server.command)}",
            "-c",
            f"{prefix}.args={json.dumps(list(server.args))}",
        ]
        if server.env:
            argv += ["-c", f"{prefix}.env={json.dumps(dict(server.env))}"]
    insert_at = 3 if context.plan.argv[1:2] == ["fork"] else 1
    return McpRenderOverlay(argv=argv, argv_insert_at=insert_at)
