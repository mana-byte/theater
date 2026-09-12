"""Frozen public runtime contracts for native harness wiring."""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from theater.constants.harness import (
    HARNESS_RUNTIME_ERROR_MAX_CHARS,
    HARNESS_RUNTIME_HEALTH_MAX_DIAGNOSTICS,
    HARNESS_RUNTIME_ID_MAX_CHARS,
    HARNESS_RUNTIME_INTERACTION_DETAIL_MAX_CHARS,
    HARNESS_RUNTIME_POLICY_MAX_CHARS,
    HARNESS_RUNTIME_RESULT_MAX_CHARS,
)
from theater.harness.contracts.channels import ChannelDeclaration, ChannelKind
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.values import freeze_json_mapping

if TYPE_CHECKING:
    from theater.harness.contracts.source import Source

#: Tested compatibility-policy names are identifier-like and may carry versions.
_POLICY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]*$")


class RuntimeWiring(StrEnum):
    """Wiring selection vocabulary for spawn surfaces and persisted bindings."""

    AUTO = "auto"
    NATIVE = "native"
    LEGACY = "legacy"


class RuntimeHost(StrEnum):
    """Where a harness runtime is hosted."""

    DETACHED_BACKEND = "detached_backend"
    FRONTEND = "frontend"


class RuntimeLifecyclePhase(StrEnum):
    """The persisted lifecycle phase of one participant runtime binding."""

    INTENDED = "intended"
    STARTED = "started"
    BOUND = "bound"
    ATTACHED = "attached"
    ACTIVE = "active"
    DETACHED = "detached"
    STOPPED = "stopped"
    FAILED = "failed"


class SessionOpenMode(StrEnum):
    """How ``open_session`` establishes the native session."""

    NEW = "new"
    FORK = "fork"
    RECONNECT = "reconnect"


class DeliveryResult(StrEnum):
    """What the runtime could establish about one control delivery."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class ControlDeliveryPhase(StrEnum):
    """The durable phase of one control operation."""

    RESERVED = "reserved"
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    SETTLED = "settled"


class ControlKind(StrEnum):
    """The kind of control an operation represents."""

    SEND = "send"
    STEER = "steer"
    QUEUE_FOLLOWUP = "queue_followup"
    SETTINGS_UPDATE = "settings_update"
    INTERRUPT = "interrupt"


class ControlTransport(StrEnum):
    """The transport selected for one control operation."""

    LEGACY_TMUX = "legacy_tmux"
    NATIVE_RUNTIME = "native_runtime"


class RuntimeCapability(StrEnum):
    """A per-session control capability the runtime may or may not offer."""

    SEND = "send"
    STEER = "steer"
    QUEUE_FOLLOWUP = "queue_followup"
    SETTINGS_UPDATE = "settings_update"
    INTERRUPT = "interrupt"


class CapabilityUnavailableReason(StrEnum):
    """Why one effective capability is unavailable, explicitly."""

    NOT_DETERMINED = "not_determined"
    UNSUPPORTED_NATIVE_VERSION = "unsupported_native_version"
    GATED_BY_BACKEND = "gated_by_backend"
    SESSION_STATE = "session_state"
    WIRING_MODE = "wiring_mode"
    THEATER_POLICY = "theater_policy"


class ConnectionHealth(StrEnum):
    """The health of the runtime's control/observation connection."""

    UNOPENED = "unopened"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"


class RuntimeExecutionState(StrEnum):
    """The plugin-confirmed execution state of one runtime's session."""

    UNKNOWN = "unknown"
    IDLE = "idle"
    ACTIVE = "active"


class NativeTurnTerminal(StrEnum):
    """The terminal outcome of one native turn, before job-state mapping."""

    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ResultCompleteness(StrEnum):
    """How complete an available native result is."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class ResultProvenance(StrEnum):
    """Where an available native result came from."""

    NATIVE_EVIDENCE = "native_evidence"
    LIVE_STREAM = "live_stream"
    TRANSCRIPT = "transcript"
    UNKNOWN = "unknown"


class NativeInteractionKind(StrEnum):
    """A pending human interaction only the native UI may answer."""

    APPROVAL = "approval"
    CLARIFICATION = "clarification"


def _bounded_id(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > HARNESS_RUNTIME_ID_MAX_CHARS:
        raise ValueError(f"runtime {label} must be a bounded non-blank string")


def _bounded_optional_text(
    value: object, label: str, *, limit: int, allow_blank: bool = False
) -> None:
    if value is None:
        return
    if not isinstance(value, str) or len(value) > limit or (not allow_blank and not value.strip()):
        raise ValueError(f"runtime {label} must be a bounded string or null")


#: A native server-request or control-request id exactly as the native backend captured it.
type NativeRequestId = int | str

#: Bounded integer request ids: JSON-safe and far beyond any observed native.
_NATIVE_REQUEST_ID_MAX_INT = 2**63 - 1


def validate_native_request_id(value: NativeRequestId | None, label: str) -> None:
    """Validate one native request id as captured, without stringifying it."""
    if value is None:
        return
    if isinstance(value, bool):
        raise TypeError(f"runtime {label} must be an int or str native request id, not bool")
    if isinstance(value, int):
        if not 0 <= value <= _NATIVE_REQUEST_ID_MAX_INT:
            raise ValueError(f"runtime {label} must be a non-negative bounded integer")
        return
    _bounded_id(value, label)


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Effective native session settings, only as confirmed by the backend."""

    model: str | None = None
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        _bounded_optional_text(self.model, "settings model", limit=HARNESS_RUNTIME_ID_MAX_CHARS)
        _bounded_optional_text(
            self.reasoning_effort, "settings reasoning_effort", limit=HARNESS_RUNTIME_ID_MAX_CHARS
        )


@dataclass(frozen=True, slots=True)
class NativeHumanInteraction:
    """One pending approval or clarification the native UI owns."""

    kind: NativeInteractionKind
    native_request_id: NativeRequestId | None = None
    native_turn_id: str | None = None
    native_item_id: str | None = None
    details: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, NativeInteractionKind):
            raise TypeError("runtime interaction kind must be a NativeInteractionKind")
        validate_native_request_id(self.native_request_id, "interaction native_request_id")
        _bounded_optional_text(
            self.native_turn_id,
            "interaction native_turn_id",
            limit=HARNESS_RUNTIME_ID_MAX_CHARS,
        )
        _bounded_optional_text(
            self.native_item_id,
            "interaction native_item_id",
            limit=HARNESS_RUNTIME_ID_MAX_CHARS,
        )
        _bounded_optional_text(
            self.details,
            "interaction details",
            limit=HARNESS_RUNTIME_INTERACTION_DETAIL_MAX_CHARS,
            allow_blank=True,
        )


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    """Effective per-session capabilities, failing closed before determination."""

    #: The capabilities explicitly determined available.
    available: frozenset[RuntimeCapability] = frozenset()
    #: Explicit reasons for capabilities known to be unavailable.
    unavailable_reasons: Mapping[RuntimeCapability, CapabilityUnavailableReason] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        if isinstance(self.available, str) or not hasattr(self.available, "__iter__"):
            raise TypeError("runtime capabilities available must be a collection")
        available: set[RuntimeCapability] = set()
        for capability in self.available:
            if not isinstance(capability, RuntimeCapability):
                raise TypeError("runtime capabilities available must contain RuntimeCapability")
            available.add(capability)
        if not isinstance(self.unavailable_reasons, Mapping):
            raise TypeError("runtime capabilities unavailable_reasons must be a mapping")
        reasons: dict[RuntimeCapability, CapabilityUnavailableReason] = {}
        for capability, reason in self.unavailable_reasons.items():
            if not isinstance(capability, RuntimeCapability):
                raise TypeError("runtime capabilities keys must be RuntimeCapability values")
            if not isinstance(reason, CapabilityUnavailableReason):
                raise TypeError("runtime capabilities reasons must be CapabilityUnavailableReason")
            if capability in available:
                raise ValueError(
                    f"runtime capability cannot be both available and unavailable: {capability}"
                )
            reasons[capability] = reason
        object.__setattr__(self, "available", frozenset(available))
        object.__setattr__(self, "unavailable_reasons", MappingProxyType(reasons))

    def reason_for(self, capability: RuntimeCapability) -> CapabilityUnavailableReason | None:
        """The effective reason one capability is unavailable, or None."""
        if capability in self.available:
            return None
        return self.unavailable_reasons.get(capability, CapabilityUnavailableReason.NOT_DETERMINED)

    def supports(self, capability: RuntimeCapability) -> bool:
        return capability in self.available


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """One runtime's exact session state at a moment."""

    participant_id: str
    backend_generation: int
    native_session_id: str | None = None
    native_turn_id: str | None = None
    pending_interaction: NativeHumanInteraction | None = None
    settings: RuntimeSettings = field(default_factory=RuntimeSettings)
    capabilities: RuntimeCapabilities = field(default_factory=RuntimeCapabilities)
    health: ConnectionHealth = ConnectionHealth.UNOPENED
    health_diagnostics: tuple[str, ...] = ()
    #: Plugin-confirmed execution state; ``UNKNOWN`` is never proof of idle.
    execution_state: RuntimeExecutionState = RuntimeExecutionState.UNKNOWN

    def __post_init__(self) -> None:
        _bounded_id(self.participant_id, "snapshot participant_id")
        if type(self.backend_generation) is not int or self.backend_generation < 0:
            raise ValueError("runtime snapshot backend_generation must be a non-negative integer")
        _bounded_optional_text(
            self.native_session_id,
            "snapshot native_session_id",
            limit=HARNESS_RUNTIME_ID_MAX_CHARS,
        )
        _bounded_optional_text(
            self.native_turn_id, "snapshot native_turn_id", limit=HARNESS_RUNTIME_ID_MAX_CHARS
        )
        if self.pending_interaction is not None and not isinstance(
            self.pending_interaction, NativeHumanInteraction
        ):
            raise TypeError("runtime snapshot pending_interaction must be a NativeHumanInteraction")
        if not isinstance(self.settings, RuntimeSettings):
            raise TypeError("runtime snapshot settings must be RuntimeSettings")
        if not isinstance(self.capabilities, RuntimeCapabilities):
            raise TypeError("runtime snapshot capabilities must be RuntimeCapabilities")
        if not isinstance(self.health, ConnectionHealth):
            raise TypeError("runtime snapshot health must be a ConnectionHealth")
        if isinstance(self.health_diagnostics, str):
            raise TypeError("runtime snapshot health_diagnostics must be a collection of strings")
        diagnostics = tuple(self.health_diagnostics)
        if len(diagnostics) > HARNESS_RUNTIME_HEALTH_MAX_DIAGNOSTICS:
            raise ValueError("runtime snapshot health_diagnostics exceed the bounded limit")
        object.__setattr__(self, "health_diagnostics", diagnostics)
        if not isinstance(self.execution_state, RuntimeExecutionState):
            raise TypeError("runtime snapshot execution_state must be a RuntimeExecutionState")


@dataclass(frozen=True, slots=True)
class ControlReceipt:
    """What one control operation established."""

    operation_id: str
    result: DeliveryResult
    native_turn_id: str | None = None
    native_request_id: NativeRequestId | None = None
    error_code: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        _bounded_id(self.operation_id, "receipt operation_id")
        if not isinstance(self.result, DeliveryResult):
            raise TypeError("runtime receipt result must be a DeliveryResult")
        _bounded_optional_text(
            self.native_turn_id, "receipt native_turn_id", limit=HARNESS_RUNTIME_ID_MAX_CHARS
        )
        validate_native_request_id(self.native_request_id, "receipt native_request_id")
        _bounded_optional_text(
            self.error_code, "receipt error_code", limit=HARNESS_RUNTIME_ID_MAX_CHARS
        )
        _bounded_optional_text(self.error, "receipt error", limit=HARNESS_RUNTIME_ERROR_MAX_CHARS)


@dataclass(frozen=True, slots=True)
class NativeTurnOutcome:
    """Exact terminal evidence for one native turn."""

    native_session_id: str
    native_turn_id: str
    terminal: NativeTurnTerminal
    result: str | None = None
    completeness: ResultCompleteness = ResultCompleteness.UNAVAILABLE
    provenance: ResultProvenance = ResultProvenance.NATIVE_EVIDENCE
    error_code: str | None = None
    error: str | None = None
    #: History can finish an exact job, but is not a fresh interruption of
    #: whatever work happens to be queued when backfill reaches this turn.
    from_history: bool = False
    #: Native terminal time in Unix seconds when available. Sources must not
    #: substitute the time they read history for the time the turn ended.
    completed_at: float | None = None

    def __post_init__(self) -> None:
        _bounded_id(self.native_session_id, "outcome native_session_id")
        _bounded_id(self.native_turn_id, "outcome native_turn_id")
        if not isinstance(self.terminal, NativeTurnTerminal):
            raise TypeError("runtime outcome terminal must be a NativeTurnTerminal")
        _bounded_optional_text(
            self.result,
            "outcome result",
            limit=HARNESS_RUNTIME_RESULT_MAX_CHARS,
            allow_blank=True,
        )
        if not isinstance(self.completeness, ResultCompleteness):
            raise TypeError("runtime outcome completeness must be a ResultCompleteness")
        if not isinstance(self.provenance, ResultProvenance):
            raise TypeError("runtime outcome provenance must be a ResultProvenance")
        _bounded_optional_text(
            self.error_code, "outcome error_code", limit=HARNESS_RUNTIME_ID_MAX_CHARS
        )
        _bounded_optional_text(self.error, "outcome error", limit=HARNESS_RUNTIME_ERROR_MAX_CHARS)
        if not isinstance(self.from_history, bool):
            raise TypeError("runtime outcome from_history must be a boolean")
        if self.completed_at is not None and (
            not isinstance(self.completed_at, (int, float))
            or isinstance(self.completed_at, bool)
            or not 0 <= self.completed_at <= 253402300799
            or not math.isfinite(self.completed_at)
        ):
            raise ValueError("runtime outcome completed_at must be a finite Unix timestamp")


@dataclass(frozen=True, slots=True)
class RuntimePlan:
    """A pure backend plan plus the private local endpoint it will listen on."""

    backend: LaunchPlan
    endpoint: str

    def __post_init__(self) -> None:
        if not isinstance(self.backend, LaunchPlan):
            raise TypeError("runtime plan backend must be a LaunchPlan")
        if not isinstance(self.endpoint, str) or not self.endpoint.strip():
            raise ValueError("runtime plan endpoint must be a non-blank string")


@dataclass(frozen=True, slots=True)
class RuntimeBinding:
    """The persisted identity of one participant's native runtime."""

    participant_id: str
    backend_generation: int
    wiring: RuntimeWiring
    lifecycle: RuntimeLifecyclePhase = RuntimeLifecyclePhase.INTENDED
    endpoint: str | None = None
    pid: int | None = None
    native_session_id: str | None = None
    protocol: str | None = None
    protocol_version: str | None = None
    native_version: str | None = None
    compatibility_policy: str | None = None
    launch_policy: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        _bounded_id(self.participant_id, "binding participant_id")
        if type(self.backend_generation) is not int or self.backend_generation < 0:
            raise ValueError("runtime binding backend_generation must be a non-negative integer")
        if not isinstance(self.wiring, RuntimeWiring):
            raise TypeError("runtime binding wiring must be a RuntimeWiring")
        if not isinstance(self.lifecycle, RuntimeLifecyclePhase):
            raise TypeError("runtime binding lifecycle must be a RuntimeLifecyclePhase")
        _bounded_optional_text(self.endpoint, "binding endpoint", limit=4096)
        if self.pid is not None and (type(self.pid) is not int or self.pid <= 0):
            raise ValueError("runtime binding pid must be a positive integer or null")
        _bounded_optional_text(
            self.native_session_id, "binding native_session_id", limit=HARNESS_RUNTIME_ID_MAX_CHARS
        )
        _bounded_optional_text(
            self.protocol, "binding protocol", limit=HARNESS_RUNTIME_ID_MAX_CHARS
        )
        _bounded_optional_text(
            self.protocol_version,
            "binding protocol_version",
            limit=HARNESS_RUNTIME_ID_MAX_CHARS,
        )
        _bounded_optional_text(
            self.native_version, "binding native_version", limit=HARNESS_RUNTIME_ID_MAX_CHARS
        )
        _bounded_optional_text(
            self.compatibility_policy,
            "binding compatibility_policy",
            limit=HARNESS_RUNTIME_POLICY_MAX_CHARS,
        )
        if not isinstance(self.launch_policy, Mapping):
            raise TypeError("runtime binding launch_policy must be a mapping or null")
        object.__setattr__(self, "launch_policy", freeze_json_mapping(self.launch_policy))


@dataclass(frozen=True, slots=True)
class LiveChannelDeclaration:
    """A first-class declaration of a runtime's live channel."""

    channel: ChannelDeclaration
    #: Native terminal evidence from this channel drives exact job completion.
    drives_job_completion: bool = True
    #: Durable history remains available when the live connection fails.
    durable_fallback: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.channel, ChannelDeclaration):
            raise TypeError("live channel declaration must wrap a ChannelDeclaration")
        if self.channel.kind is not ChannelKind.LIVE:
            raise ValueError("live channel declaration kind must be ChannelKind.LIVE")
        if type(self.drives_job_completion) is not bool:
            raise TypeError("live channel drives_job_completion must be a boolean")
        if type(self.durable_fallback) is not bool:
            raise TypeError("live channel durable_fallback must be a boolean")


class RuntimeConnectionError(Exception):
    """Base class for injected runtime connection failures."""


class RuntimeConnectionClosed(RuntimeConnectionError):
    """The injected connection is closed or was closed by the remote."""


class RuntimeRequestTimeout(RuntimeConnectionError):
    """A request deadline elapsed before a response arrived."""


class RuntimeRequestError(RuntimeConnectionError):
    """The remote answered one request with an error."""

    def __init__(self, code: object, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"runtime request failed: {message} (code {code!r})")


@dataclass(frozen=True, slots=True)
class RuntimeNotification:
    """One native notification or server request observed on a connection."""

    method: str
    params: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    request_id: NativeRequestId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.method, str) or not self.method.strip():
            raise ValueError("runtime notification method must be a non-blank string")
        if not isinstance(self.params, Mapping):
            raise TypeError("runtime notification params must be a mapping")
        object.__setattr__(self, "params", freeze_json_mapping(self.params))
        validate_native_request_id(self.request_id, "notification request_id")


class RuntimeConnection(ABC):
    """One bounded connection to a native backend endpoint."""

    @abstractmethod
    async def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: float,
    ) -> Mapping[str, object]:
        """Send one request and await its correlated result."""

    @abstractmethod
    async def notify(self, method: str, params: Mapping[str, object]) -> None:
        """Send one notification; no result is expected."""

    @abstractmethod
    def notifications(self) -> AsyncIterator[RuntimeNotification]:
        """Iterate observed notifications and server requests."""

    @abstractmethod
    async def aclose(self) -> None:
        """Close this connection without terminating the backend."""


class RuntimeFrontendConnection(ABC):
    """One authenticated local frontend connection."""

    @property
    @abstractmethod
    def closed(self) -> bool:
        """Whether the frontend has disconnected."""

    @abstractmethod
    def notifications(self) -> AsyncIterator[RuntimeNotification]:
        """Iterate bounded observation messages from the frontend."""

    async def request(
        self, method: str, params: Mapping[str, object], *, timeout: float
    ) -> Mapping[str, object]:
        """Send one bounded request, without replay after uncertain delivery."""
        raise RuntimeRequestError("unsupported", "this frontend has no request transport")

    @abstractmethod
    async def aclose(self) -> None:
        """Close this frontend connection without touching its application."""


class RuntimeIO(ABC):
    """The injected runtime I/O service seam."""

    @abstractmethod
    async def connect(self, endpoint: str, *, timeout: float) -> RuntimeConnection:
        """Connect to one private local endpoint within the timeout."""


@dataclass(frozen=True, slots=True)
class RuntimeProbeContext:
    """Read-only facts available to a compatibility probe."""

    participant_id: str | None = None
    binary: str | None = None
    cwd: str | None = None

    def __post_init__(self) -> None:
        _bounded_optional_text(
            self.participant_id, "probe participant_id", limit=HARNESS_RUNTIME_ID_MAX_CHARS
        )
        _bounded_optional_text(self.binary, "probe binary", limit=4096)
        _bounded_optional_text(self.cwd, "probe cwd", limit=4096)


@dataclass(frozen=True, slots=True)
class RuntimeCompatibility:
    """The result of one read-only compatibility probe."""

    supported: bool
    policy: str = "unverified"
    native_version: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.supported) is not bool:
            raise TypeError("runtime compatibility supported must be a boolean")
        if (
            not isinstance(self.policy, str)
            or not self.policy.strip()
            or not _POLICY_NAME.fullmatch(self.policy)
            or len(self.policy) > HARNESS_RUNTIME_POLICY_MAX_CHARS
        ):
            raise ValueError("runtime compatibility policy must be a bounded non-blank name")
        _bounded_optional_text(
            self.native_version,
            "compatibility native_version",
            limit=HARNESS_RUNTIME_ID_MAX_CHARS,
        )
        _bounded_optional_text(
            self.reason, "compatibility reason", limit=HARNESS_RUNTIME_ERROR_MAX_CHARS
        )


class RuntimeCompatibilityProbe(Protocol):
    """Read-only probe of installed native compatibility."""

    def __call__(self, context: RuntimeProbeContext) -> RuntimeCompatibility: ...


@dataclass(frozen=True, slots=True)
class RuntimePlanningContext:
    """Immutable facts for one pure backend planning call."""

    participant_id: str
    cwd: str
    endpoint: str
    config_path: Path | None = None
    approval: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        _bounded_id(self.participant_id, "planning participant_id")
        if not isinstance(self.cwd, str) or not self.cwd.strip():
            raise ValueError("runtime planning cwd must be a non-blank string")
        if not isinstance(self.endpoint, str) or not self.endpoint.strip():
            raise ValueError("runtime planning endpoint must be a non-blank string")
        if self.config_path is not None and not isinstance(self.config_path, Path):
            raise TypeError("runtime planning config_path must be a Path or null")
        _bounded_optional_text(
            self.approval, "planning approval", limit=HARNESS_RUNTIME_ID_MAX_CHARS
        )
        _bounded_optional_text(self.model, "planning model", limit=HARNESS_RUNTIME_ID_MAX_CHARS)
        _bounded_optional_text(
            self.reasoning_effort,
            "planning reasoning_effort",
            limit=HARNESS_RUNTIME_ID_MAX_CHARS,
        )


class RuntimeBackendPlanner(Protocol):
    """Build the pure backend plan for one participant, writing nothing."""

    def __call__(self, context: RuntimePlanningContext) -> RuntimePlan: ...


@dataclass(frozen=True, slots=True)
class RuntimeFrontendInstallContext:
    """Facts for overlaying a passive frontend extension on a stock plan."""

    participant_id: str
    endpoint: str
    token_file: Path

    def __post_init__(self) -> None:
        _bounded_id(self.participant_id, "frontend install participant_id")
        if not isinstance(self.endpoint, str) or not self.endpoint.strip():
            raise ValueError("runtime frontend install endpoint must be a non-blank string")
        if not isinstance(self.token_file, Path):
            raise TypeError("runtime frontend install token_file must be a Path")


@dataclass(frozen=True, slots=True)
class RuntimeFrontendOverlay:
    """Public launch-plan additions from a passive frontend installer."""

    env: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    files: Mapping[Path, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not isinstance(self.env, Mapping) or any(
            not isinstance(key, str) or not key or not isinstance(value, str)
            for key, value in self.env.items()
        ):
            raise TypeError("runtime frontend overlay env must map non-blank strings to strings")
        if not isinstance(self.files, Mapping) or any(
            not isinstance(path, Path) or not isinstance(contents, str)
            for path, contents in self.files.items()
        ):
            raise TypeError("runtime frontend overlay files must map Paths to strings")
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))


class RuntimeFrontendInstaller(Protocol):
    """Overlay one passive stock-frontend extension onto an ordinary plan."""

    def __call__(self, context: RuntimeFrontendInstallContext) -> RuntimeFrontendOverlay: ...


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Immutable facts plus injected I/O for one runtime instance."""

    participant_id: str
    cwd: str | None
    io: RuntimeIO
    backend_generation: int
    endpoint: str | None = None
    config_path: Path | None = None
    approval: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    native_session_id: str | None = None
    frontend: RuntimeFrontendConnection | None = None
    trusted_session_id_provider: Callable[[], str | None] | None = None

    def __post_init__(self) -> None:
        _bounded_id(self.participant_id, "context participant_id")
        if not isinstance(self.io, RuntimeIO):
            raise TypeError("runtime context io must be a RuntimeIO")
        if type(self.backend_generation) is not int or self.backend_generation < 0:
            raise ValueError("runtime context backend_generation must be a non-negative integer")
        _bounded_optional_text(self.cwd, "context cwd", limit=4096)
        _bounded_optional_text(self.endpoint, "context endpoint", limit=4096)
        if self.config_path is not None and not isinstance(self.config_path, Path):
            raise TypeError("runtime context config_path must be a Path or null")
        _bounded_optional_text(
            self.approval, "context approval", limit=HARNESS_RUNTIME_ID_MAX_CHARS
        )
        _bounded_optional_text(self.model, "context model", limit=HARNESS_RUNTIME_ID_MAX_CHARS)
        _bounded_optional_text(
            self.reasoning_effort, "context reasoning_effort", limit=HARNESS_RUNTIME_ID_MAX_CHARS
        )
        _bounded_optional_text(
            self.native_session_id,
            "context native_session_id",
            limit=HARNESS_RUNTIME_ID_MAX_CHARS,
        )
        if self.frontend is not None and not isinstance(self.frontend, RuntimeFrontendConnection):
            raise TypeError("runtime context frontend must be a RuntimeFrontendConnection or null")
        if self.trusted_session_id_provider is not None and not callable(
            self.trusted_session_id_provider
        ):
            raise TypeError("runtime context trusted_session_id_provider must be callable or null")


class RuntimeFactory(Protocol):
    """Create one runtime instance from immutable facts and injected I/O."""

    def __call__(self, context: RuntimeContext) -> HarnessRuntime: ...


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    """The runtime half of one harness manifest."""

    probe: RuntimeCompatibilityProbe
    plan: RuntimeBackendPlanner | None
    factory: RuntimeFactory
    channel: LiveChannelDeclaration
    host: RuntimeHost = RuntimeHost.DETACHED_BACKEND
    frontend_installer: RuntimeFrontendInstaller | None = None
    legacy_fallback: frozenset[RuntimeCapability] = frozenset()
    unavailable_capabilities: frozenset[RuntimeCapability] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.host, RuntimeHost):
            raise TypeError("runtime manifest host must be a RuntimeHost")
        if self.host is RuntimeHost.DETACHED_BACKEND and self.plan is None:
            raise ValueError("detached runtime manifest requires a backend planner")
        if self.host is RuntimeHost.FRONTEND and self.frontend_installer is None:
            raise ValueError("frontend runtime manifest requires a frontend installer")
        fallback = _runtime_capability_set(self.legacy_fallback, "legacy_fallback")
        unavailable = _runtime_capability_set(
            self.unavailable_capabilities,
            "unavailable_capabilities",
        )
        if fallback & unavailable:
            raise ValueError(
                "runtime manifest capabilities cannot be both fallback and unavailable"
            )
        object.__setattr__(self, "legacy_fallback", fallback)
        object.__setattr__(self, "unavailable_capabilities", unavailable)


def _runtime_capability_set(value: object, label: str) -> frozenset[RuntimeCapability]:
    if isinstance(value, str) or not hasattr(value, "__iter__"):
        raise TypeError(f"runtime manifest {label} must be a collection")
    result: set[RuntimeCapability] = set()
    for capability in value:
        if not isinstance(capability, RuntimeCapability):
            raise TypeError(f"runtime manifest {label} must contain RuntimeCapability")
        result.add(capability)
    return frozenset(result)


class HarnessRuntime(ABC):
    """One participant's live native runtime."""

    @abstractmethod
    async def open_session(
        self,
        *,
        mode: SessionOpenMode,
        native_session_id: str | None = None,
    ) -> RuntimeBinding:
        """Open, fork, or reconnect the native session and return its binding."""

    @abstractmethod
    async def frontend_plan(self, *, native_session_id: str | None = None) -> LaunchPlan:
        """Plan native UI attachment only; the plan carries no initial prompt."""

    @abstractmethod
    def live_source(self) -> Source:
        """The single live ``Source`` shared by observation and controls."""

    @abstractmethod
    async def snapshot(self) -> RuntimeSnapshot:
        """Exact session identity, active turn, interaction, settings, health."""

    @abstractmethod
    async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
        """Submit one prompt as a new native turn."""

    @abstractmethod
    async def steer(
        self,
        *,
        operation_id: str,
        native_turn_id: str,
        prompt: str,
    ) -> ControlReceipt:
        """Amend exactly the named active native turn; a stale refusal stays a refusal."""

    @abstractmethod
    async def interrupt(
        self,
        *,
        operation_id: str,
        native_turn_id: str | None = None,
    ) -> ControlReceipt:
        """Request interruption of the exact current native turn."""

    @abstractmethod
    async def update_settings(
        self,
        *,
        operation_id: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ControlReceipt:
        """Update only the supplied settings, idle-only, without emulating unconfirmed
        application."""

    @abstractmethod
    async def aclose(self) -> None:
        """Disconnect Theater's connections only; never terminate the backend."""


__all__ = [
    "CapabilityUnavailableReason",
    "ConnectionHealth",
    "ControlDeliveryPhase",
    "ControlKind",
    "ControlReceipt",
    "ControlTransport",
    "DeliveryResult",
    "LiveChannelDeclaration",
    "NativeHumanInteraction",
    "NativeInteractionKind",
    "NativeRequestId",
    "NativeTurnOutcome",
    "NativeTurnTerminal",
    "ResultCompleteness",
    "ResultProvenance",
    "RuntimeBackendPlanner",
    "RuntimeBinding",
    "RuntimeCapabilities",
    "RuntimeCompatibility",
    "RuntimeCompatibilityProbe",
    "RuntimeConnection",
    "RuntimeConnectionClosed",
    "RuntimeConnectionError",
    "RuntimeContext",
    "RuntimeExecutionState",
    "RuntimeFactory",
    "RuntimeFrontendConnection",
    "RuntimeFrontendInstallContext",
    "RuntimeFrontendInstaller",
    "RuntimeFrontendOverlay",
    "RuntimeHost",
    "RuntimeIO",
    "RuntimeLifecyclePhase",
    "RuntimeManifest",
    "RuntimeNotification",
    "RuntimePlan",
    "RuntimePlanningContext",
    "RuntimeProbeContext",
    "RuntimeRequestError",
    "RuntimeRequestTimeout",
    "RuntimeSettings",
    "RuntimeSnapshot",
    "RuntimeWiring",
    "SessionOpenMode",
    "validate_native_request_id",
]
