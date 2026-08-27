"""Vibe observer composition and source wiring."""

from __future__ import annotations

from pathlib import Path

from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.contracts.source import Source
from theater.harness.observation import TranscriptObserver
from theater.provenance import TranscriptProvenance
from theater.trajectory.capabilities import TrajectoryCapabilities, TrajectoryFeature

from .identity import VibeIdentityMixin
from .isolation import _canonical, validate_isolated_domain
from .parser import VibeParserMixin
from .screen import VibeScreenMixin
from .source import _open_vibe_source
from .trajectory import VibeTrajectoryMixin


class VibeObserver(
    VibeIdentityMixin,
    VibeParserMixin,
    VibeTrajectoryMixin,
    VibeScreenMixin,
    TranscriptObserver,
):
    """Read Vibe messages and meta usage from isolated or shared roots."""

    trajectory_capabilities = TrajectoryCapabilities(
        supported=frozenset(
            {
                TrajectoryFeature.MODELS,
                TrajectoryFeature.TOOLS,
                TrajectoryFeature.USAGE,
                TrajectoryFeature.TIMING,
                TrajectoryFeature.REASONING,
                TrajectoryFeature.CONTEXT,
                TrajectoryFeature.LIVE_UPDATES,
            }
        ),
        unsupported=frozenset(
            {
                TrajectoryFeature.REQUESTS,
                TrajectoryFeature.RETRIES,
            }
        ),
    )

    def __init__(
        self,
        root: Path | None = None,
        correlation_root: Path | None = None,
        *,
        isolated: bool = False,
    ):
        self.root = root or Path.home() / ".vibe" / "logs" / "session"
        self.correlation_root = correlation_root
        self.isolated = isolated
        self.relocate_by_cwd = True
        self._cwd: str | None = None
        self._active_turn_id: str | None = None
        self._last_turn_id: str | None = None

    def open_source(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
        session_provenance: str | TranscriptProvenance | None = None,
        known_location: str | None = None,
    ):
        """Give every source its own parser state, including its cwd."""
        return _open_vibe_source(
            self,
            cwd=cwd,
            session_id=session_id,
            after=after,
            session_provenance=session_provenance,
            known_location=known_location,
        )

    def open_source_for(
        self,
        *,
        participant_id: str,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
        session_provenance: str | TranscriptProvenance | None = None,
        known_location: str | None = None,
        transcript_domain: str | None = None,
    ):
        if transcript_domain is not None:
            domain = _canonical(Path(transcript_domain))
            if validate_isolated_domain(domain) is not None:
                reader = VibeObserver(
                    root=domain,
                    correlation_root=self.correlation_root,
                    isolated=True,
                )
                return reader.open_source(
                    cwd=cwd,
                    session_id=session_id,
                    after=after,
                    session_provenance=session_provenance,
                    known_location=known_location,
                )
            reader = VibeObserver(
                root=domain,
                correlation_root=self.correlation_root,
                isolated=False,
            )
            return reader.open_source(
                cwd=cwd,
                session_id=session_id,
                after=after,
                session_provenance=session_provenance,
                known_location=known_location,
            )
        participant_root = _canonical(self.participant_root(participant_id))
        if validate_isolated_domain(participant_root, participant_id=participant_id) is not None:
            reader = VibeObserver(
                root=participant_root,
                correlation_root=self.correlation_root,
                isolated=True,
            )
            return reader.open_source(
                cwd=cwd,
                session_id=session_id,
                after=after,
                session_provenance=session_provenance,
                known_location=known_location,
            )
        return self.open_source(
            cwd=cwd,
            session_id=session_id,
            after=after,
            session_provenance=session_provenance,
            known_location=known_location,
        )

    def open_source_context(self, context: ParticipantObservationContext) -> Source:
        return self.open_source_for(
            participant_id=context.participant_id,
            cwd=context.cwd,
            session_id=context.session_id,
            after=context.after,
            session_provenance=context.session_provenance,
            known_location=context.known_location,
            transcript_domain=context.transcript_domain,
        )
