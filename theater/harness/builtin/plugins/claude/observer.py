"""Claude observer composition."""

from __future__ import annotations

from pathlib import Path

from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.contracts.source import Source
from theater.harness.observation import TranscriptObserver
from theater.trajectory.capabilities import TrajectoryCapabilities, TrajectoryFeature

from .identity import ClaudeIdentity
from .parser import ClaudeParser
from .screen import ClaudeScreen
from .timing import _ClaudeCausalRecord, _ClaudeRequestClock
from .trajectory import ClaudeTrajectory


class ClaudeCodeObserver(
    ClaudeIdentity,
    ClaudeParser,
    ClaudeTrajectory,
    ClaudeScreen,
    TranscriptObserver,
):
    """Observe Claude records while deduplicating repeated native turn boundaries."""

    trajectory_capabilities = TrajectoryCapabilities(
        supported=frozenset(
            {
                TrajectoryFeature.REQUESTS,
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
        self._main_turn_id: str | None = None

    def open_source_context(self, context: ParticipantObservationContext) -> Source:
        return self.open_source_for(
            participant_id=context.participant_id,
            cwd=context.cwd,
            session_id=context.session_id,
            after=context.after,
            session_provenance=context.session_provenance,
            known_location=context.known_location,
        )
