from theater.mcp_plugins import (
    MANIFEST_API_VERSION,
    McpConfigField,
    McpConfigKind,
    McpConfigSchema,
    McpLaunchManifest,
    McpServerManifest,
    PluginCapability,
)

from .server import plan

MANIFEST = McpServerManifest(
    api_version=MANIFEST_API_VERSION,
    description="Native fixture sidecar",
    capabilities=frozenset({PluginCapability.PARTICIPANTS_READ}),
    launch=McpLaunchManifest(planner=plan),
    config=McpConfigSchema({"label": McpConfigField(McpConfigKind.STRING, default="native")}),
)
