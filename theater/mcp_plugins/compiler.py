"""Compile validated MCP-server manifests into canonical server specifications."""

from __future__ import annotations

from collections.abc import Mapping

from theater.mcp_plugins.config import resolve_config
from theater.mcp_plugins.contracts import McpServerManifest, McpServerSpec
from theater.mcp_plugins.validation import validate_manifest


def compile_manifest(
    name: str,
    manifest: McpServerManifest,
    config: Mapping[str, object] | None = None,
) -> McpServerSpec:
    """Validate static manifest data and resolve one enabled plugin configuration."""
    validate_manifest(name, manifest)
    resolved = resolve_config(name, manifest.config, config)
    return McpServerSpec(
        name=name,
        description=manifest.description,
        capabilities=manifest.capabilities,
        config=resolved,
        launch=manifest.launch,
    )


__all__ = ["compile_manifest"]
