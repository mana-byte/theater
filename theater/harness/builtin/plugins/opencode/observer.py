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
from .dialect import domain_for, is_v2_participant, v2_database_for_domain, v2_database_path
from .identity import admit_operator_candidate, transcript_candidates, validate_receipt_session_id
from .mcp import catalog_path, plugin_path
from .native_plugin_v2 import plugin_dir
from .screen import is_idle_screen, screen_reading
from .source import OpenCodeSource
from .source_v2 import OpenCodeV2Source


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
        lineage = v2_database_for_domain(context.transcript_domain)
        if (
            lineage is None
            and context.participant_scoped
            and is_v2_participant(context.participant_id)
        ):
            lineage = v2_database_path(context.participant_id)
        if lineage is not None:
            return self._open_v2_source(context, lineage)
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

    def _open_v2_source(self, context: ParticipantObservationContext, db: Path) -> Source:
        """A 2.x participant reads its own lineage database, whatever the 1.x default is."""
        receipt_expected = False
        mcp_catalog = None
        if context.participant_scoped:
            config_path = (
                self.correlation_dir / f"{context.participant_id}.json"
                if self.correlation_dir is not None
                else paths.mcp_config_path(context.participant_id)
            )
            receipt_expected = plugin_dir(config_path).exists()
            mcp_catalog = catalog_path(context.participant_id, self.correlation_dir)
        source = OpenCodeV2Source(
            db,
            cwd=context.cwd,
            session_id=context.session_id,
            after=context.after,
            receipt_expected=receipt_expected,
            session_provenance=context.session_provenance,
            known_location=context.known_location,
            mcp_catalog_path=mcp_catalog,
        )
        source.collision_domain = domain_for(db)
        return source

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
        db = v2_database_for_domain(domain) or self.db
        return transcript_candidates(db, cwd=cwd, domain=domain, after=after)

    def admit_operator_candidate(
        self,
        *,
        cwd: str | None,
        candidate: str,
        domain: str | None = None,
        after: float | None = None,
    ) -> TranscriptCandidate:
        return admit_operator_candidate(
            v2_database_for_domain(domain) or self.db,
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
