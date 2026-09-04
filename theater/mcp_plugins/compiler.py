"""Compile validated MCP-server manifests into configured plugin definitions."""

from __future__ import annotations

from collections.abc import Mapping

from theater.mcp_plugins.config import resolve_config
from theater.mcp_plugins.contracts import CompiledMcpPlugin, McpServerManifest
from theater.mcp_plugins.validation import validate_manifest


def compile_manifest(
    name: str,
    manifest: McpServerManifest,
    config: Mapping[str, object] | None = None,
) -> CompiledMcpPlugin:
    """Validate static manifest data and resolve one enabled plugin configuration."""
    validate_manifest(name, manifest)
    resolved = resolve_config(name, manifest.config, config)
    return CompiledMcpPlugin(
        name=name,
        description=manifest.description,
        capabilities=manifest.capabilities,
        config=resolved,
        launch=manifest.launch,
    )


__all__ = ["compile_manifest"]
