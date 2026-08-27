"""Static channel declarations and runtime health values."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from theater.constants.core import HARNESS_NAME
from theater.constants.harness import (
    HARNESS_CHANNEL_DEFAULT_MAX_PAYLOAD_BYTES,
    HARNESS_CHANNEL_DEFAULT_MAX_QUEUE,
    HARNESS_CHANNEL_HEALTH_COUNTER_MAX,
    HARNESS_CHANNEL_HEALTH_DIAGNOSTIC_MAX_CHARS,
    HARNESS_CHANNEL_HEALTH_MAX_DIAGNOSTICS,
    HARNESS_CHANNEL_ID_MAX_CHARS,
    HARNESS_OTEL_DEFAULT_MAX_ATTRIBUTES,
    HARNESS_OTEL_DEFAULT_MAX_RECORDS,
    HARNESS_OTEL_DEFAULT_MAX_TEXT_BYTES,
    HARNESS_OTEL_DEFAULT_MAX_VALUE_DEPTH,
)
from theater.harness.contracts.callbacks import (
    HookCorrelationExtractor,
    HookDecoder,
    OtelCorrelationExtractor,
    OtelSignalDecoder,
)
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.contracts.values import freeze_json_mapping


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


class OtelProtocol(StrEnum):
    """The bounded inbound OTLP encodings Theater can receive."""

    OTLP_HTTP_JSON = "otlp_http_json"
    OTLP_HTTP_PROTOBUF = "otlp_http_protobuf"


class OtelSignal(StrEnum):
    """The bounded OTLP signal families Theater can receive."""

    LOGS = "logs"


@dataclass(frozen=True, slots=True)
class ChannelBounds:
    """Static limits a future channel implementation must respect."""

    max_queue: int = HARNESS_CHANNEL_DEFAULT_MAX_QUEUE
    max_payload_bytes: int = HARNESS_CHANNEL_DEFAULT_MAX_PAYLOAD_BYTES


@dataclass(frozen=True, slots=True)
class OtelBounds:
    """Additional bounded limits for one native OTel channel."""

    max_records: int = HARNESS_OTEL_DEFAULT_MAX_RECORDS
    max_attributes: int = HARNESS_OTEL_DEFAULT_MAX_ATTRIBUTES
    max_value_depth: int = HARNESS_OTEL_DEFAULT_MAX_VALUE_DEPTH
    max_text_bytes: int = HARNESS_OTEL_DEFAULT_MAX_TEXT_BYTES


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
class OtelCorrelation:
    """Explicit header and resource identity fields for one OTel channel."""

    auth_header: str
    participant_attribute: str
    harness_attribute: str
    channel_attribute: str
    binding_attribute: str
    delivery_id_attribute: str


@dataclass(frozen=True, slots=True)
class OtelBinding:
    """One declared native OTel signal binding."""

    name: str
    signal: OtelSignal
    signals: tuple[SignalKind, ...]
    decoder: OtelSignalDecoder
    correlation: OtelCorrelationExtractor

    def __post_init__(self) -> None:
        object.__setattr__(self, "signals", tuple(self.signals))


@dataclass(frozen=True, slots=True)
class OtelRecord:
    """One bounded generic native OTel record exposed to plugin callbacks."""

    signal: OtelSignal
    resource: Mapping[str, object]
    attributes: Mapping[str, object]
    body: object | None = None
    timestamp_unix_nano: int | None = None
    observed_timestamp_unix_nano: int | None = None
    trace_id: str | None = None
    span_id: str | None = None
    severity_number: int | None = None
    severity_text: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.signal, OtelSignal):
            raise TypeError("OTel record signal must be an OtelSignal")
        if not isinstance(self.resource, Mapping) or not isinstance(self.attributes, Mapping):
            raise TypeError("OTel record resource and attributes must be mappings")
        object.__setattr__(self, "resource", freeze_json_mapping(self.resource))
        object.__setattr__(self, "attributes", freeze_json_mapping(self.attributes))
        object.__setattr__(self, "body", freeze_json_mapping({"body": self.body})["body"])
        for attribute in (
            "timestamp_unix_nano",
            "observed_timestamp_unix_nano",
            "severity_number",
        ):
            value = getattr(self, attribute)
            if value is not None and (type(value) is not int or value < 0):
                raise TypeError(f"OTel record {attribute} must be a non-negative integer or null")
        for attribute in ("trace_id", "span_id", "severity_text"):
            value = getattr(self, attribute)
            if value is not None and (not isinstance(value, str) or not value):
                raise TypeError(f"OTel record {attribute} must be a non-blank string or null")


@dataclass(frozen=True, slots=True)
class ChannelHealth:
    """Bounded runtime health distinct from a static declaration."""

    channel_id: str
    state: ChannelHealthState = ChannelHealthState.INACTIVE
    diagnostics: tuple[str, ...] = ()
    dropped: int = 0
    accepted: int = 0
    last_success_at: float | None = None

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
        if (
            type(self.dropped) is not int
            or self.dropped < 0
            or self.dropped > HARNESS_CHANNEL_HEALTH_COUNTER_MAX
        ):
            raise ValueError("channel health dropped must be a bounded non-negative integer")
        if (
            type(self.accepted) is not int
            or self.accepted < 0
            or self.accepted > HARNESS_CHANNEL_HEALTH_COUNTER_MAX
        ):
            raise ValueError("channel health accepted must be a bounded non-negative integer")
        if self.last_success_at is not None and (
            type(self.last_success_at) not in (int, float)
            or not math.isfinite(self.last_success_at)
            or self.last_success_at < 0
        ):
            raise ValueError("channel health last_success_at must be a non-negative finite number")
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
    "OtelBinding",
    "OtelBounds",
    "OtelCorrelation",
    "OtelProtocol",
    "OtelRecord",
    "OtelSignal",
    "SignalKind",
    "SignalOwnership",
]
