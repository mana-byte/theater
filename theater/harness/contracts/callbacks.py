"""Typed callback seams for immutable harness manifests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from theater.harness.contracts.context import ParticipantObservationContext

if TYPE_CHECKING:
    from theater.harness.contracts.launch import LaunchPlan, NativeChild, ResumeLaunchOverlay
    from theater.harness.contracts.observation import ScreenReading
    from theater.harness.contracts.source import Source, StreamPoint, TranscriptCandidate
    from theater.models import Participant


@dataclass(frozen=True, slots=True)
class LaunchContext:
    """One requested harness launch."""

    participant_id: str
    prompt: str
    config_path: Path
    approval: str
    model: str | None = None
    reasoning_effort: str | None = None
    resume: str | None = None


@dataclass(frozen=True, slots=True)
class ResumeContext:
    """Trusted predecessor facts available to a resume overlay."""

    predecessor: Participant
    trusted_session_owners: tuple[Participant, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "trusted_session_owners", tuple(self.trusted_session_owners))


@dataclass(frozen=True, slots=True)
class ScreenContext:
    """One captured terminal screen."""

    capture: str


@dataclass(frozen=True, slots=True)
class ModelDiscoveryContext:
    """Stable metadata available to model discovery."""

    name: str
    binary: str


@dataclass(frozen=True, slots=True)
class NativeChildrenContext:
    """A transcript whose native child records may be inspected."""

    transcript: Path


@dataclass(frozen=True, slots=True)
class StreamFloorContext:
    """A native stream location whose current point is requested."""

    location: str


@dataclass(frozen=True, slots=True)
class TranscriptCandidatesContext:
    """Bounds for an operator-visible transcript candidate search."""

    cwd: str | None
    domain: str | None = None
    after: float | None = None


@dataclass(frozen=True, slots=True)
class ReceiptValidationContext:
    """Opaque native receipt plus Theater's expected identity facts."""

    payload: Mapping[str, object]
    cwd: str | None
    expected_session_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class OperatorCandidateContext:
    """One operator-selected transcript candidate to validate."""

    cwd: str | None
    candidate: str
    domain: str | None = None
    after: float | None = None


class LaunchPlanner(Protocol):
    """Build the pure launch plan for one requested participant."""

    def __call__(self, context: LaunchContext) -> LaunchPlan: ...


class ResumePlanner(Protocol):
    """Build the safe overlay for a selected resume predecessor."""

    def __call__(self, context: ResumeContext) -> ResumeLaunchOverlay: ...


class SourceFactory(Protocol):
    """Open a per-participant source without accessing daemon state."""

    def __call__(self, context: ParticipantObservationContext) -> Source: ...


class ScreenClassifier(Protocol):
    """Classify a terminal capture without authorizing any control action."""

    def __call__(self, context: ScreenContext) -> ScreenReading: ...


class ModelDiscoverer(Protocol):
    """Return model names reported by the harness, if it can be queried."""

    def __call__(self, context: ModelDiscoveryContext) -> Sequence[str]: ...


class NativeChildrenReader(Protocol):
    """Read native sub-agent identities from one transcript."""

    def __call__(self, context: NativeChildrenContext) -> Sequence[NativeChild]: ...


class StreamFloorReader(Protocol):
    """Read the current immutable point of a native stream."""

    def __call__(self, context: StreamFloorContext) -> StreamPoint | None: ...


class TranscriptCandidatesReader(Protocol):
    """Return bounded operator-visible transcript candidates."""

    def __call__(self, context: TranscriptCandidatesContext) -> Sequence[TranscriptCandidate]: ...


class ReceiptValidator(Protocol):
    """Validate one opaque native receipt into an exact candidate."""

    def __call__(self, context: ReceiptValidationContext) -> TranscriptCandidate: ...


class OperatorCandidateAdmitter(Protocol):
    """Validate one operator-selected transcript candidate."""

    def __call__(self, context: OperatorCandidateContext) -> TranscriptCandidate: ...


__all__ = [
    "LaunchContext",
    "LaunchPlanner",
    "ModelDiscoverer",
    "ModelDiscoveryContext",
    "NativeChildrenContext",
    "NativeChildrenReader",
    "OperatorCandidateAdmitter",
    "OperatorCandidateContext",
    "ReceiptValidationContext",
    "ReceiptValidator",
    "ResumeContext",
    "ResumePlanner",
    "ScreenClassifier",
    "ScreenContext",
    "SourceFactory",
    "StreamFloorContext",
    "StreamFloorReader",
    "TranscriptCandidatesContext",
    "TranscriptCandidatesReader",
]
