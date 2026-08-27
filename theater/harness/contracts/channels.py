"""Static channel declarations and runtime health values."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from theater.constants.core import HARNESS_NAME
from theater.constants.harness import (
    HARNESS_CHANNEL_DEFAULT_MAX_PAYLOAD_BYTES,
    HARNESS_CHANNEL_DEFAULT_MAX_QUEUE,
    HARNESS_CHANNEL_HEALTH_DIAGNOSTIC_MAX_CHARS,
    HARNESS_CHANNEL_HEALTH_MAX_DIAGNOSTICS,
    HARNESS_CHANNEL_ID_MAX_CHARS,
)
from theater.harness.contracts.callbacks import HookCorrelationExtractor, HookDecoder
from theater.harness.contracts.trajectory import TrajectoryFact


class ChannelKind(StrEnum):
    """A generic origin for normalized harness facts."""

    TRANSCRIPT = "transcript"
    DATABASE = "database"
    HOOK = "hook"
    OTEL = "otel"
    SCREEN = "screen"
    PROCESS = "process"


class SignalKind(StrEnum):
    """A normalized fact category a channel may produce."""

    IDENTITY = "identity"
    LIFECYCLE = "lifecycle"
    CONTENT = "content"
    TURN = "turn"
    MODEL = "model"
    TOOL = "tool"
    TIMING = "timing"
    USAGE = "usage"
    LINEAGE = "lineage"


@dataclass(frozen=True, slots=True)
class ChannelFact:
    """One normalized fact tagged with its declared signal."""

    signal: SignalKind
    fact: TrajectoryFact

    def __post_init__(self) -> None:
        if not isinstance(self.signal, SignalKind):
            raise TypeError("channel fact signal must be a SignalKind")
        if not isinstance(self.fact, TrajectoryFact):
            raise TypeError("channel fact fact must be a TrajectoryFact")


class SignalOwnership(StrEnum):
    """The authority a channel claims for one signal category."""

    PRIMARY = "primary"
    ENRICHMENT = "enrichment"
    FALLBACK = "fallback"


class ChannelHealthState(StrEnum):
    """The current state of a configured channel."""

    INACTIVE = "inactive"
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"


class HookDeliveryMode(StrEnum):
    """A native hook's documented delivery behaviour."""

    ORDERED = "ordered"
    RETRIED = "retried"
    BEST_EFFORT = "best_effort"


@dataclass(frozen=True, slots=True)
class ChannelBounds:
    """Static limits a future channel implementation must respect."""

    max_queue: int = HARNESS_CHANNEL_DEFAULT_MAX_QUEUE
    max_payload_bytes: int = HARNESS_CHANNEL_DEFAULT_MAX_PAYLOAD_BYTES


@dataclass(frozen=True, slots=True)
class ChannelCapability:
    """One normalized signal and its declared ownership."""

    signal: SignalKind
    ownership: SignalOwnership


@dataclass(frozen=True, slots=True)
class ChannelDeclaration:
    """Static capabilities and bounds for one named channel."""

    id: str
    kind: ChannelKind
    capabilities: tuple[ChannelCapability, ...] = ()
    bounds: ChannelBounds = field(default_factory=ChannelBounds)

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", tuple(self.capabilities))


@dataclass(frozen=True, slots=True)
class HookBinding:
    """One native event wired to explicit normalized callbacks."""

    event: str
    signals: tuple[SignalKind, ...]
    decoder: HookDecoder
    correlation: HookCorrelationExtractor
    delivery: HookDeliveryMode = HookDeliveryMode.BEST_EFFORT

    def __post_init__(self) -> None:
        object.__setattr__(self, "signals", tuple(self.signals))


@dataclass(frozen=True, slots=True)
class ChannelHealth:
    """Bounded runtime health distinct from a static declaration."""

    channel_id: str
    state: ChannelHealthState = ChannelHealthState.INACTIVE
    diagnostics: tuple[str, ...] = ()
    dropped: int = 0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.channel_id, str)
            or not self.channel_id.strip()
            or len(self.channel_id) > HARNESS_CHANNEL_ID_MAX_CHARS
            or not HARNESS_NAME.fullmatch(self.channel_id)
        ):
            raise ValueError("channel health channel_id must be a bounded non-blank string")
        if not isinstance(self.state, ChannelHealthState):
            raise TypeError("channel health state must be a ChannelHealthState")
        if isinstance(self.diagnostics, str):
            raise TypeError("channel health diagnostics must be a collection of strings")
        diagnostics = tuple(self.diagnostics)
        if len(diagnostics) > HARNESS_CHANNEL_HEALTH_MAX_DIAGNOSTICS:
            raise ValueError("channel health diagnostics exceed the bounded limit")
        if any(
            not isinstance(item, str)
            or not item.strip()
            or len(item) > HARNESS_CHANNEL_HEALTH_DIAGNOSTIC_MAX_CHARS
            for item in diagnostics
        ):
            raise ValueError("channel health diagnostics must be bounded non-blank strings")
        if type(self.dropped) is not int or self.dropped < 0:
            raise ValueError("channel health dropped must be a non-negative integer")
        object.__setattr__(self, "diagnostics", diagnostics)


__all__ = [
    "ChannelBounds",
    "ChannelCapability",
    "ChannelDeclaration",
    "ChannelFact",
    "ChannelHealth",
    "ChannelHealthState",
    "ChannelKind",
    "HookBinding",
    "HookDeliveryMode",
    "SignalKind",
    "SignalOwnership",
]
