from theater.mcp_plugins import (
    MANIFEST_API_VERSION,
    McpConfigField,
    McpConfigKind,
    McpConfigSchema,
    McpLaunchManifest,
    McpServerManifest,
    PluginCapability,
)

from .wrapper import plan

MANIFEST = McpServerManifest(
    api_version=MANIFEST_API_VERSION,
    description="Compatibility fixture sidecar",
    capabilities=frozenset({PluginCapability.PARTICIPANTS_READ}),
    launch=McpLaunchManifest(planner=plan),
    config=McpConfigSchema({"endpoint": McpConfigField(McpConfigKind.STRING, required=True)}),
)
