"""Static declaration for the shipped Beads MCP sidecar."""

from __future__ import annotations

import sys

from theater.mcp_plugins import (
    MANIFEST_API_VERSION,
    McpConfigField,
    McpConfigKind,
    McpConfigSchema,
    McpLaunchContext,
    McpLaunchManifest,
    McpLaunchPlan,
    McpServerManifest,
    PluginCapability,
)


def plan(context: McpLaunchContext) -> McpLaunchPlan:
    argv = [
        "-m",
        "theater.mcp_plugins.builtin.beads.server",
        "--state-dir",
        str(context.state_dir),
        "--board-ttl-seconds",
        str(context.config["board_ttl_seconds"]),
    ]
    if context.config.get("bd_path") is not None:
        argv.extend(("--bd-path", str(context.config["bd_path"])))
    return McpLaunchPlan(command=sys.executable, argv=tuple(argv))


MANIFEST = McpServerManifest(
    api_version=MANIFEST_API_VERSION,
    description="Play-scoped Beads task boards",
    capabilities=frozenset({PluginCapability.PLAYS_READ, PluginCapability.JOBS_READ}),
    launch=McpLaunchManifest(planner=plan),
    config=McpConfigSchema(
        {
            "board_ttl_seconds": McpConfigField(McpConfigKind.INTEGER, default=604800, minimum=0),
            "bd_path": McpConfigField(McpConfigKind.PATH),
        }
    ),
    plays=("hierarchy",),
    play_permissions=frozenset({"read", "write"}),
)
