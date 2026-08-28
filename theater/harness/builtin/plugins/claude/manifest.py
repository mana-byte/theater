"""Claude Code package manifest."""

from dataclasses import replace
from functools import partial
from pathlib import Path

from theater.harness.contracts.manifest import MANIFEST_API_VERSION, HarnessManifest

from .launch import LAUNCH, _resume_launch_overlay, _resume_preflight
from .observation import OBSERVATION, observation_for

MANIFEST = HarnessManifest(
    api_version=MANIFEST_API_VERSION,
    binary="claude",
    binaries=frozenset({".claude-wrapped", "claude-wrapped"}),
    icon="✻",
    aliases=("claude_code", "claude-code", "Claude", "ClaudeCode"),
    launch=LAUNCH,
    observation=OBSERVATION,
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
