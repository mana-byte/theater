"""Typed callback seams for immutable harness manifests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.contracts.values import freeze_json_mapping

if TYPE_CHECKING:
    from theater.harness.contracts.channels import ChannelFact, OtelRecord
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


@dataclass(frozen=True, slots=True)
class HookCorrelationContext:
    """One bounded native envelope awaiting correlation."""

    participant_id: str
    channel_id: str
    event: str
    payload: Mapping[str, object]
    delivery_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise TypeError("hook payload must be a mapping")
        object.__setattr__(
            self,
            "payload",
            freeze_json_mapping(self.payload),
        )


@dataclass(frozen=True, slots=True)
class HookDecodeContext:
    """One bounded native envelope with accepted correlation."""

    participant_id: str
    channel_id: str
    event: str
    payload: Mapping[str, object]
    native_id: str
    delivery_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise TypeError("hook payload must be a mapping")
        if not isinstance(self.native_id, str) or not self.native_id.strip():
            raise TypeError("hook native_id must be a non-blank string")
        object.__setattr__(self, "payload", freeze_json_mapping(self.payload))


@dataclass(frozen=True, slots=True)
class HookInstallContext:
    """Launch-local facts exposed to a hook installer."""

    participant_id: str
    channel_id: str
    token_file: Path
    theater_executable: str


@dataclass(frozen=True, slots=True)
class HookInstallOverlay:
    """Public files and environment from one hook installer."""

    env: Mapping[str, str] = MappingProxyType({})
    files: Mapping[Path, str] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))


@dataclass(frozen=True, slots=True)
class OtelCorrelationContext:
    """One accepted bounded native OTel record awaiting correlation."""

    participant_id: str
    harness: str
    channel_id: str
    record: OtelRecord
    delivery_id: str


@dataclass(frozen=True, slots=True)
class OtelDecodeContext:
    """One accepted native OTel record with exact correlation."""

    participant_id: str
    harness: str
    channel_id: str
    record: OtelRecord
    delivery_id: str
    native_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.native_id, str) or not self.native_id.strip():
            raise TypeError("OTel native_id must be a non-blank string")


@dataclass(frozen=True, slots=True)
class OtelInstallContext:
    """Launch-local facts exposed to a native OTel installer."""

    participant_id: str
    harness: str
    channel_id: str
    token_file: Path
    endpoint: str
    auth_header: str
    resource_attributes: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "resource_attributes",
            MappingProxyType(dict(self.resource_attributes)),
        )


@dataclass(frozen=True, slots=True)
class OtelInstallOverlay:
    """Public files and environment from one native OTel installer."""

    env: Mapping[str, str] = MappingProxyType({})
    files: Mapping[Path, str] = MappingProxyType({})
    credential_header_env: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))


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


class HookDecoder(Protocol):
    """Decode one declared hook payload into normalized facts."""

    def __call__(self, context: HookDecodeContext) -> Sequence[ChannelFact]: ...


class HookCorrelationExtractor(Protocol):
    """Extract one exact native correlation identity."""

    def __call__(self, context: HookCorrelationContext) -> str: ...


class HookInstaller(Protocol):
    """Build launch-local hook configuration without secret bytes."""

    def __call__(self, context: HookInstallContext) -> HookInstallOverlay: ...


class OtelSignalDecoder(Protocol):
    """Decode one declared OTel record into normalized facts."""

    def __call__(self, context: OtelDecodeContext) -> Sequence[ChannelFact]: ...


class OtelCorrelationExtractor(Protocol):
    """Extract one exact native correlation identity from an OTel record."""

    def __call__(self, context: OtelCorrelationContext) -> str: ...


class OtelInstaller(Protocol):
    """Build launch-local OTel configuration without secret bytes."""

    def __call__(self, context: OtelInstallContext) -> OtelInstallOverlay: ...


__all__ = [
    "HookCorrelationContext",
    "HookCorrelationExtractor",
    "HookDecodeContext",
    "HookDecoder",
    "HookInstallContext",
    "HookInstallOverlay",
    "HookInstaller",
    "LaunchContext",
    "LaunchPlanner",
    "ModelDiscoverer",
    "ModelDiscoveryContext",
    "NativeChildrenContext",
    "NativeChildrenReader",
    "OperatorCandidateAdmitter",
    "OperatorCandidateContext",
    "OtelCorrelationContext",
    "OtelCorrelationExtractor",
    "OtelDecodeContext",
    "OtelInstallContext",
    "OtelInstallOverlay",
    "OtelInstaller",
    "OtelSignalDecoder",
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
