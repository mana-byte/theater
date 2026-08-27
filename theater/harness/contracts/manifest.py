"""Immutable values describing one named harness plugin."""

from __future__ import annotations

from dataclasses import dataclass, field

from theater.constants.harness import (
    HARNESS_MANIFEST_API_VERSION as MANIFEST_API_VERSION,
)
from theater.constants.harness import (
    HARNESS_PLUGIN_API_VERSION as PLUGIN_API_VERSION,
)
from theater.harness.contracts.callbacks import (
    LaunchPlanner,
    ModelDiscoverer,
    NativeChildrenReader,
    OperatorCandidateAdmitter,
    ReceiptValidator,
    ResumePlanner,
    ScreenClassifier,
    SourceFactory,
    StreamFloorReader,
    TranscriptCandidatesReader,
)
from theater.harness.contracts.channels import (
    ChannelDeclaration,
    SignalKind,
)
from theater.harness.contracts.harness import ResumeStrategy
from theater.trajectory import TrajectoryCapabilities


@dataclass(frozen=True, slots=True)
class LaunchManifest:
    """Explicit launch and resume capabilities for one harness."""

    planner: LaunchPlanner
    #: Declared order is user-visible: it is the order the refusal message lists.
    approvals: tuple[str, ...]
    supports_model: bool = False
    supports_reasoning_effort: bool = False
    supports_resume: bool = False
    resume_planner: ResumePlanner | None = None
    resume_takes_prompt: bool = True
    resume_strategy: ResumeStrategy = "continue"

    def __post_init__(self) -> None:
        if isinstance(self.approvals, (str, bytes)):
            raise TypeError("launch.approvals must be a sequence of approval policies")
        object.__setattr__(self, "approvals", tuple(self.approvals))


@dataclass(frozen=True, slots=True)
class SourceManifest:
    """The durable primary source and its static channel declaration."""

    factory: SourceFactory
    channel: ChannelDeclaration


@dataclass(frozen=True, slots=True)
class ScreenManifest:
    """An explicit terminal-screen classifier."""

    classifier: ScreenClassifier


@dataclass(frozen=True, slots=True)
class ModelDiscoveryManifest:
    """An optional model-discovery callback."""

    discoverer: ModelDiscoverer


@dataclass(frozen=True, slots=True)
class IdentityManifest:
    """Optional exact identity and operator-recovery callbacks."""

    stream_floor: StreamFloorReader | None = None
    transcript_candidates: TranscriptCandidatesReader | None = None
    receipt_validator: ReceiptValidator | None = None
    operator_candidate_admitter: OperatorCandidateAdmitter | None = None


@dataclass(frozen=True, slots=True)
class LineageManifest:
    """Optional native child discovery."""

    native_children: NativeChildrenReader | None = None


@dataclass(frozen=True, slots=True)
class HookChannelManifest:
    """A future hook channel declaration with no transport implementation."""

    declaration: ChannelDeclaration
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class OtelChannelManifest:
    """A future native-telemetry declaration with no transport implementation."""

    declaration: ChannelDeclaration
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class UnavailableChannelManifest:
    """A known channel limitation made visible without claiming a transport."""

    declaration: ChannelDeclaration
    reason: str


type EnrichmentManifest = (
    ChannelDeclaration | HookChannelManifest | OtelChannelManifest | UnavailableChannelManifest
)


@dataclass(frozen=True, slots=True)
class ObservationManifest:
    """The primary durable source, screen reader, and declared enrichments."""

    primary: SourceManifest | None
    screen: ScreenManifest
    identity: IdentityManifest = field(default_factory=IdentityManifest)
    lineage: LineageManifest = field(default_factory=LineageManifest)
    trajectory_capabilities: TrajectoryCapabilities = field(default_factory=TrajectoryCapabilities)
    enrichments: tuple[EnrichmentManifest, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "enrichments", tuple(self.enrichments))

    @property
    def channels(self) -> tuple[ChannelDeclaration, ...]:
        """All static channels, primary first and enrichment order preserved."""
        primary = () if self.primary is None else (self.primary.channel,)
        return primary + tuple(_channel_declaration(item) for item in self.enrichments)

    @property
    def capabilities(self) -> frozenset[SignalKind]:
        """Normalized signals derived from declarations, never a second table."""
        return frozenset(
            capability.signal for channel in self.channels for capability in channel.capabilities
        )


def _channel_declaration(manifest: EnrichmentManifest) -> ChannelDeclaration:
    if isinstance(manifest, ChannelDeclaration):
        return manifest
    return manifest.declaration


@dataclass(frozen=True, slots=True)
class HarnessManifest:
    """The complete immutable contract for one canonical folder name."""

    api_version: int
    binary: str
    icon: str
    launch: LaunchManifest
    observation: ObservationManifest
    binaries: frozenset[str] = frozenset()
    aliases: tuple[str, ...] = ()
    models: ModelDiscoveryManifest | None = None
    _binaries_are_text: bool = field(init=False, repr=False, compare=False)
    _aliases_are_text: bool = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_binaries_are_text", isinstance(self.binaries, (str, bytes)))
        object.__setattr__(self, "_aliases_are_text", isinstance(self.aliases, (str, bytes)))
        object.__setattr__(self, "binaries", frozenset(self.binaries))
        object.__setattr__(self, "aliases", tuple(self.aliases))


__all__ = [
    "MANIFEST_API_VERSION",
    "PLUGIN_API_VERSION",
    "EnrichmentManifest",
    "HarnessManifest",
    "HookChannelManifest",
    "IdentityManifest",
    "LaunchManifest",
    "LineageManifest",
    "ModelDiscoveryManifest",
    "ObservationManifest",
    "OtelChannelManifest",
    "ScreenManifest",
    "SourceManifest",
    "UnavailableChannelManifest",
]
