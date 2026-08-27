"""Immutable context for opening one participant observation source."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from theater.provenance import TranscriptProvenance, normalize_provenance


@dataclass(frozen=True, slots=True)
class ParticipantObservationContext:
    """Facts the daemon has already established for one source opening."""

    participant_id: str
    cwd: str | None
    session_id: str | None = None
    after: int | float | None = None
    session_provenance: str | TranscriptProvenance | None = TranscriptProvenance.HEURISTIC
    known_location: str | None = None
    transcript_domain: str | None = None
    pane_pid: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.participant_id, str):
            raise TypeError("participant observation context participant_id must be a string")
        if not self.participant_id.strip():
            raise ValueError("participant observation context participant_id must not be blank")
        for name in ("cwd", "session_id", "known_location", "transcript_domain"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"participant observation context {name} must be a string or null")
        if self.after is not None:
            if type(self.after) not in (int, float):
                raise TypeError("participant observation context after must be a number or null")
            if not isfinite(self.after):
                raise ValueError("participant observation context after must be finite")
        if self.pane_pid is not None:
            if type(self.pane_pid) is not int:
                raise TypeError(
                    "participant observation context pane_pid must be an integer or null"
                )
            if self.pane_pid <= 0:
                raise ValueError("participant observation context pane_pid must be positive")
        if self.session_provenance is not None and not isinstance(
            self.session_provenance,
            (str, TranscriptProvenance),
        ):
            raise TypeError(
                "participant observation context session_provenance must be a string or null"
            )
        object.__setattr__(
            self,
            "session_provenance",
            normalize_provenance(self.session_provenance),
        )


__all__ = ["ParticipantObservationContext"]
