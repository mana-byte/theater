"""Vibe observer composition and source wiring."""

from __future__ import annotations

from pathlib import Path

from theater.harness.observation import TranscriptObserver
from theater.provenance import TranscriptProvenance
from theater.trajectory.capabilities import TrajectoryCapabilities, TrajectoryFeature

from .identity import VibeIdentityMixin
from .isolation import _canonical, validate_isolated_domain
from .parser import VibeParserMixin
from .screen import VibeScreenMixin
from .source import _VibeSource, _VibeTranscriptSource
from .trajectory import VibeTrajectoryMixin


class VibeObserver(
    VibeIdentityMixin,
    VibeParserMixin,
    VibeTrajectoryMixin,
    VibeScreenMixin,
    TranscriptObserver,
):
    """Read Vibe's messages JSONL and meta usage data.

    Theater cold launches write below a participant-specific root. Resumed
    launches keep the trusted predecessor's root after the daemon validates
    session provenance and the domain marker. Within an isolated root, Vibe
    rotations are exact by construction.
    """

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
        #: Injectable so tests never touch the real ~/.vibe.
        self.root = root or Path.home() / ".vibe" / "logs" / "session"
        self.correlation_root = correlation_root
        self.isolated = isolated
        self.relocate_by_cwd = True
        #: Set in `find_transcript` so `parse` can relativise absolute paths vibe's tool args carry.
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
        reader = VibeObserver(
            root=self.root,
            correlation_root=self.correlation_root,
            isolated=self.isolated,
        )
        reader._cwd = cwd
        inner = _VibeTranscriptSource(
            reader,
            cwd=cwd,
            session_id=session_id,
            after=after,
            allow_refresh=True,
            exact_attachments=reader.isolated,
            session_provenance=session_provenance,
            collision_domain=str(reader.root.resolve()),
            known_location=known_location,
        )
        return _VibeSource(
            inner,
            after=after,
            session_id=session_id,
            known_location=known_location,
            observer=reader,
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
