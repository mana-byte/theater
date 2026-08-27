"""Codex observer composition."""

from __future__ import annotations

from pathlib import Path

from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.contracts.source import Source
from theater.harness.observation import TranscriptObserver
from theater.provenance import TranscriptProvenance, normalize_provenance
from theater.trajectory.capabilities import TrajectoryCapabilities, TrajectoryFeature

from .identity import CodexIdentityMixin
from .parser import CodexParserMixin
from .screen import CodexScreenMixin
from .trajectory import CodexTrajectoryMixin


class CodexObserver(
    CodexIdentityMixin,
    CodexParserMixin,
    CodexTrajectoryMixin,
    CodexScreenMixin,
    TranscriptObserver,
):
    """Observe Codex rollout JSONL files."""

    proves_ownership = True
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

    def __init__(
        self,
        root: Path | None = None,
        pane_pid: int | None = None,
        session_exact: bool = False,
        session_provenance: str | TranscriptProvenance | None = None,
    ):
        self.root = root or Path.home() / ".codex" / "sessions"
        self.pane_pid = pane_pid
        self._last_model: str | None = None
        self._last_provider: str | None = None
        self._last_cwd: str | None = None
        self._active_turn_id: str | None = None
        self._pending_patch_exec: tuple[str, float] | None = None
        self._mcp_calls: dict[str, tuple[str, str]] = {}
        provenance = normalize_provenance(session_provenance)
        self._session_exact = session_exact or provenance is TranscriptProvenance.EXACT
        self._proved: set[Path] = set()

    def open_source_context(self, context: ParticipantObservationContext) -> Source:
        return self.open_source_for(
            participant_id=context.participant_id,
            cwd=context.cwd,
            session_id=context.session_id,
            after=context.after,
            session_provenance=context.session_provenance,
            known_location=context.known_location,
            pane_pid=context.pane_pid,
        )
