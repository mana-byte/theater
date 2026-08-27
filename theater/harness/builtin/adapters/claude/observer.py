"""Claude observer composition."""

from __future__ import annotations

from pathlib import Path

from theater.harness.observation import TranscriptObserver
from theater.trajectory.capabilities import TrajectoryCapabilities, TrajectoryFeature

from .identity import ClaudeIdentity
from .parser import ClaudeParser
from .screen import ClaudeScreen
from .trajectory import ClaudeTrajectory, _ClaudeCausalRecord, _ClaudeRequestClock


class ClaudeCodeObserver(
    ClaudeIdentity,
    ClaudeParser,
    ClaudeTrajectory,
    ClaudeScreen,
    TranscriptObserver,
):
    trajectory_capabilities = TrajectoryCapabilities(
        supported=frozenset(
            {
                TrajectoryFeature.REQUESTS,
                TrajectoryFeature.MODELS,
                TrajectoryFeature.MODELS,
                TrajectoryFeature.TOOLS,
                TrajectoryFeature.USAGE,
                TrajectoryFeature.TIMING,
                TrajectoryFeature.REASONING,
                TrajectoryFeature.CONTEXT,
                TrajectoryFeature.LIVE_UPDATES,
            }
        ),
        unsupported=frozenset({TrajectoryFeature.RETRIES}),
    )

    def __init__(self, root: Path | None = None):
        self.root = root or Path.home() / ".claude" / "projects"
        self._mcp_calls: dict[str, tuple[str, str]] = {}
        self._causal_records: dict[str, _ClaudeCausalRecord] = {}
        self._request_clocks: dict[str, _ClaudeRequestClock] = {}
