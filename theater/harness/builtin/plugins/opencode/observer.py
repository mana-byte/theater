"""OpenCode observer wiring."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from theater import paths
from theater.harness.contracts.callbacks import (
    OperatorCandidateContext,
    ReceiptValidationContext,
    ScreenContext,
    TranscriptCandidatesContext,
)
from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.observation import ScreenReading
from theater.harness.source import Source, TranscriptCandidate
from theater.provenance import TranscriptProvenance
from theater.trajectory.capabilities import TrajectoryCapabilities, TrajectoryFeature

from .constants import DB_NAME
from .identity import admit_operator_candidate, transcript_candidates, validate_receipt_session_id
from .mcp import catalog_path, plugin_path
from .screen import is_idle_screen, screen_reading
from .source import OpenCodeSource


def data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / "opencode"


def database_path(db: Path | None = None) -> Path:
    """Resolve the database Theater tells OpenCode to write and observes."""
    return (db or data_dir() / DB_NAME).expanduser().resolve()


class OpenCodeObserver:
    has_transcript = True
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

    def __init__(self, db: Path | None = None, correlation_dir: Path | None = None):
        self.db = database_path(db)
        self.correlation_dir = correlation_dir

    def open_source(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Source:
        return OpenCodeSource(self.db, cwd=cwd, session_id=session_id, after=after)

    def open_source_for(
        self,
        *,
        participant_id: str,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
        session_provenance: str | TranscriptProvenance | None = None,
        known_location: str | None = None,
    ) -> Source:
        config_path = (
            self.correlation_dir / f"{participant_id}.json"
            if self.correlation_dir is not None
            else paths.mcp_config_path(participant_id)
        )
        receipt_plugin_path = plugin_path(config_path)
        return OpenCodeSource(
            self.db,
            cwd=cwd,
            session_id=session_id,
            after=after,
            receipt_expected=receipt_plugin_path.exists(),
            session_provenance=session_provenance,
            known_location=known_location,
            mcp_catalog_path=catalog_path(participant_id, self.correlation_dir),
        )

    def open_source_context(self, context: ParticipantObservationContext) -> Source:
        if not context.participant_scoped:
            return self.open_source(
                cwd=context.cwd,
                session_id=context.session_id,
                after=context.after,
            )
        return self.open_source_for(
            participant_id=context.participant_id,
            cwd=context.cwd,
            session_id=context.session_id,
            after=context.after,
            session_provenance=context.session_provenance,
            known_location=context.known_location,
        )

    def is_idle_screen(self, capture: str) -> bool:
        return is_idle_screen(capture)

    def screen_reading(self, capture: str) -> ScreenReading:
        return screen_reading(capture)

    def transcript_candidates(
        self,
        *,
        cwd: str | None,
        domain: str | None = None,
        after: float | None = None,
    ) -> list[TranscriptCandidate]:
        return transcript_candidates(self.db, cwd=cwd, domain=domain, after=after)

    def admit_operator_candidate(
        self,
        *,
        cwd: str | None,
        candidate: str,
        domain: str | None = None,
        after: float | None = None,
    ) -> TranscriptCandidate:
        return admit_operator_candidate(
            self.db,
            cwd=cwd,
            candidate=candidate,
            domain=domain,
            after=after,
        )

    def validate_transcript_receipt(
        self,
        *,
        payload: Mapping[str, object],
        cwd: str | None,
        expected_session_id: str | None,
    ) -> TranscriptCandidate:
        session_id = validate_receipt_session_id(payload.get("session_id"))
        return TranscriptCandidate(
            location=f"opencode://{session_id}",
            session_id=session_id,
            domain=f"opencode://{self.db.resolve()}",
        )


def source_factory(
    context: ParticipantObservationContext,
    *,
    db: Path | None = None,
    correlation_dir: Path | None = None,
) -> Source:
    return OpenCodeObserver(db=db, correlation_dir=correlation_dir).open_source_context(context)


def classify_screen(context: ScreenContext) -> ScreenReading:
    return screen_reading(context.capture)


def read_transcript_candidates(
    context: TranscriptCandidatesContext, *, db: Path | None = None
) -> list[TranscriptCandidate]:
    return OpenCodeObserver(db=db).transcript_candidates(
        cwd=context.cwd,
        domain=context.domain,
        after=context.after,
    )


def admit_operator_candidate_context(
    context: OperatorCandidateContext, *, db: Path | None = None
) -> TranscriptCandidate:
    return OpenCodeObserver(db=db).admit_operator_candidate(
        cwd=context.cwd,
        candidate=context.candidate,
        domain=context.domain,
        after=context.after,
    )


def validate_receipt(
    context: ReceiptValidationContext, *, db: Path | None = None
) -> TranscriptCandidate:
    return OpenCodeObserver(db=db).validate_transcript_receipt(
        payload=context.payload,
        cwd=context.cwd,
        expected_session_id=context.expected_session_id,
    )
