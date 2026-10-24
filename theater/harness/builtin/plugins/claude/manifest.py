"""Claude Code package manifest."""

from dataclasses import replace
from functools import partial
from pathlib import Path

from theater.harness.contracts.launch import render_mcp_servers_file
from theater.harness.contracts.manifest import (
    MANIFEST_API_VERSION,
    ControlManifest,
    HarnessManifest,
    InterruptPlan,
    McpForegroundWait,
    McpRenderingManifest,
    NativeCompatibilityManifest,
)

from .compat import COMPAT
from .compatibility import claude_messaging_range_text, probe_claude_native_compatibility
from .constants import (
    CLAUDE_MCP_AUTO_BACKGROUND_DEFAULT_MS,
    CLAUDE_MCP_AUTO_BACKGROUND_ENV,
    CLAUDE_MCP_AUTO_BACKGROUND_MARGIN_SECONDS,
)
from .launch import LAUNCH, _resume_launch_overlay, _resume_preflight
from .observation import OBSERVATION, observation_for

MANIFEST = HarnessManifest(
    api_version=MANIFEST_API_VERSION,
    binary="claude",
    binaries=frozenset({".claude-wrapped", "claude-wrapped"}),
    icon="✻",
    aliases=("claude_code", "claude-code", "Claude", "ClaudeCode"),
    launch=LAUNCH,
    compat=COMPAT,
    observation=OBSERVATION,
    controls=ControlManifest(interrupt=InterruptPlan(keys=("Escape",))),
    mcp=McpRenderingManifest(
        renderer=render_mcp_servers_file,
        foreground_wait=McpForegroundWait(
            env=CLAUDE_MCP_AUTO_BACKGROUND_ENV,
            default_ms=CLAUDE_MCP_AUTO_BACKGROUND_DEFAULT_MS,
            margin_seconds=CLAUDE_MCP_AUTO_BACKGROUND_MARGIN_SECONDS,
        ),
    ),
    native_compatibility=NativeCompatibilityManifest(
        qualified_range=claude_messaging_range_text(),
        probe=probe_claude_native_compatibility,
    ),
)


def manifest_for_root(root: Path) -> HarnessManifest:
    """Build Claude's manifest against an explicit transcript root."""
    return replace(
        MANIFEST,
        launch=replace(
            LAUNCH,
            resume_preflight=partial(_resume_preflight, root=root),
            resume_planner=partial(_resume_launch_overlay, root=root),
        ),
        observation=observation_for(root),
    )
