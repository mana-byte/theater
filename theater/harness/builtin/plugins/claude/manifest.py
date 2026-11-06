"""Claude Code package manifest."""

from dataclasses import replace
from functools import partial
from pathlib import Path

from theater.harness.contracts.channels import (
    ChannelCapability,
    ChannelDeclaration,
    ChannelKind,
    SignalKind,
    SignalOwnership,
)
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
from theater.harness.contracts.runtime import (
    LiveChannelDeclaration,
    RuntimeCapability,
    RuntimeHost,
    RuntimeManifest,
)

from .compatibility import (
    claude_messaging_range_text,
    probe_claude_frontend,
)
from .constants import (
    CLAUDE_MCP_AUTO_BACKGROUND_DEFAULT_MS,
    CLAUDE_MCP_AUTO_BACKGROUND_ENV,
    CLAUDE_MCP_AUTO_BACKGROUND_MARGIN_SECONDS,
)
from .frontend import install_claude_frontend
from .launch import LAUNCH, _resume_launch_overlay, _resume_preflight
from .observation import OBSERVATION, observation_for
from .runtime import claude_runtime_factory
from .runtime_protocol import CHANNEL_ID

MANIFEST = HarnessManifest(
    api_version=MANIFEST_API_VERSION,
    binary="claude",
    binaries=frozenset({".claude-wrapped", "claude-wrapped"}),
    icon="✻",
    aliases=("claude_code", "claude-code", "Claude", "ClaudeCode"),
    launch=LAUNCH,
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
    runtime=RuntimeManifest(
        probe=probe_claude_frontend,
        plan=None,
        factory=claude_runtime_factory,
        host=RuntimeHost.FRONTEND,
        frontend_installer=install_claude_frontend,
        channel=LiveChannelDeclaration(
            channel=ChannelDeclaration(
                id=CHANNEL_ID,
                kind=ChannelKind.LIVE,
                capabilities=(ChannelCapability(SignalKind.LIFECYCLE, SignalOwnership.ENRICHMENT),),
            ),
            drives_job_completion=True,
        ),
        unavailable_capabilities=frozenset(
            {RuntimeCapability.STEER, RuntimeCapability.SETTINGS_UPDATE}
        ),
    ),
    native_compatibility=NativeCompatibilityManifest(
        qualified_range=claude_messaging_range_text(),
        probe=probe_claude_frontend,
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
