"""OpenCode observer wiring."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from theater import paths
from theater.harness.observation import HarnessObserver, ScreenReading
from theater.harness.source import Source, TranscriptCandidate
from theater.provenance import TranscriptProvenance
from theater.trajectory.capabilities import TrajectoryCapabilities, TrajectoryFeature

from .constants import CORRELATION_PLUGIN_SUFFIX, DB_NAME
from .identity import admit_operator_candidate, transcript_candidates
from .screen import is_idle_screen, screen_reading
from .source import OpenCodeSource


def data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / "opencode"


class OpenCodeObserver(HarnessObserver):
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
        self.db = db or data_dir() / DB_NAME
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
        plugin_path = config_path.with_suffix(CORRELATION_PLUGIN_SUFFIX)
        return OpenCodeSource(
            self.db,
            cwd=cwd,
            session_id=session_id,
            after=after,
            receipt_expected=plugin_path.exists(),
            session_provenance=session_provenance,
            known_location=known_location,
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
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("opencode receipt payload requires a nonblank session_id")
        if "://" in session_id:
            raise ValueError(
                "opencode receipt session_id must be a native session id, not a location"
            )
        return TranscriptCandidate(
            location=f"opencode://{session_id}",
            session_id=session_id,
            domain=f"opencode://{self.db.resolve()}",
        )
