"""The compat-suite probe sidecar: one MCP tool whose marker file proves a call ran."""

from __future__ import annotations

import sys
from pathlib import Path

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
    server = Path(__file__).with_name("server.py")
    marker_dir = str(context.config["marker_dir"])
    return McpLaunchPlan(command=sys.executable, argv=(str(server), "--marker-dir", marker_dir))


MANIFEST = McpServerManifest(
    api_version=MANIFEST_API_VERSION,
    description="Compat-suite probe: writes a marker file when its tool is called",
    capabilities=frozenset({PluginCapability.CATALOG_READ}),
    launch=McpLaunchManifest(planner=plan),
    config=McpConfigSchema({"marker_dir": McpConfigField(McpConfigKind.PATH, required=True)}),
)
