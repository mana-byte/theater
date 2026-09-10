"""Frozen public runtime contracts for native harness wiring.

A runtime-capable harness plugin exposes one :class:`RuntimeManifest` through
``HarnessManifest.runtime``. The manifest is pure declaration: a read-only
compatibility probe, a pure backend planner, a runtime factory, and a
first-class live-channel declaration. Everything with side effects lives in
:class:`HarnessRuntime`, created by the factory from an immutable
:class:`RuntimeContext` that carries participant/configuration facts plus
injected runtime I/O services — never a Store or Registry.

Plugin code must not import ``theater.daemon``; shared I/O implementations are
injected through the public :class:`RuntimeIO` and :class:`RuntimeConnection`
seams below. Those are in-process interfaces, not an inter-process protocol:
the wire protocol spoken to a native backend belongs to the runtime
implementation, and Theater's own daemon protocol stays NDJSON version 1.

Money rules frozen with these contracts:

* One live :class:`~theater.harness.contracts.source.Source` per runtime. The
  daemon creates the runtime once and shares it between observation and
  controls. History reads never launch a backend or open a control connection.
* ``aclose()`` disconnects only. It never terminates the backend.
* ``send``/``steer``/``interrupt``/``update_settings`` take an explicit
  ``operation_id`` the daemon reserved durably before transmission; a native
  request id is a correlation fact, never a durable idempotency guarantee.
* Leave approval and clarification responses exclusively in the native UI:
  server requests arrive as :class:`RuntimeNotification` values and Theater
  records them, never answers them.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
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


class RuntimeLifecyclePhase(StrEnum):
    """The persisted lifecycle phase of one participant runtime binding.

    Ordered by the accepted UI-first spawn refinement: launch intent is
    persisted before the backend starts; exact native identity is persisted
    before the initial prompt; the prompt is submitted exactly once after UI
    readiness is verified; the observer subscribes once the returned turn
    materializes the rollout.
    """

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
    """What the runtime could establish about one control delivery.

    ``UNKNOWN`` means transmission or acceptance was uncertain: no retry, no
    tmux fallback, and the operation remains eligible only for reconciliation.
    """

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class ControlDeliveryPhase(StrEnum):
    """The durable phase of one control operation.

    ``RESERVED`` is persisted before transmission. ``QUEUED`` is a Theater
    owned followup waiting for an authoritative idle check; its position uses
    the persisted send-sequence allocator. ``DISPATCHED`` is persisted *before*
    transmission begins, so an interrupted transmission stays potentially
    delivered. ``SETTLED`` records a terminal delivery result; job state is
    separate and never implied by phase.
    """

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
    """Why one effective capability is unavailable, explicitly.

    Never guess: an unavailable capability reports one of these reasons, and an
    unsupported optional capability stays explicitly disabled rather than
    optimistically enabled.
    """

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
    """One pending approval or clarification the native UI owns.

    Theater records it and surfaces it; the answer belongs to the human in
    the native UI. A clarification is answered by typing, which starts a new
    turn — never a Theater control.
    """

    kind: NativeInteractionKind
    native_request_id: str | None = None
    native_turn_id: str | None = None
    native_item_id: str | None = None
    details: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, NativeInteractionKind):
            raise TypeError("runtime interaction kind must be a NativeInteractionKind")
        _bounded_optional_text(
            self.native_request_id,
            "interaction native_request_id",
            limit=HARNESS_RUNTIME_ID_MAX_CHARS,
        )
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
    """Effective per-session capabilities with explicit unavailable reasons.

    A capability is available unless it appears in ``unavailable_reasons``.
    The default is everything-available: a runtime that cannot honor a
    capability must say why, and honest refusal is the only way an
    unsupported native capability stays disabled.
    """

    unavailable_reasons: Mapping[RuntimeCapability, CapabilityUnavailableReason] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        if not isinstance(self.unavailable_reasons, Mapping):
            raise TypeError("runtime capabilities unavailable_reasons must be a mapping")
        reasons: dict[RuntimeCapability, CapabilityUnavailableReason] = {}
        for capability, reason in self.unavailable_reasons.items():
            if not isinstance(capability, RuntimeCapability):
                raise TypeError("runtime capabilities keys must be RuntimeCapability values")
            if not isinstance(reason, CapabilityUnavailableReason):
                raise TypeError("runtime capabilities reasons must be CapabilityUnavailableReason")
            reasons[capability] = reason
        object.__setattr__(self, "unavailable_reasons", MappingProxyType(reasons))

    def reason_for(self, capability: RuntimeCapability) -> CapabilityUnavailableReason | None:
        """The explicit reason one capability is unavailable, or None."""
        return self.unavailable_reasons.get(capability)

    def supports(self, capability: RuntimeCapability) -> bool:
        return capability not in self.unavailable_reasons


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """One runtime's exact session state at a moment.

    ``native_session_id`` and ``native_turn_id`` are the exact native
    identities, never cwd-derived guesses. ``pending_interaction`` is a
    native approval or clarification only the native UI may answer.
    """

    participant_id: str
    backend_generation: int
    native_session_id: str | None = None
    native_turn_id: str | None = None
    pending_interaction: NativeHumanInteraction | None = None
    settings: RuntimeSettings = field(default_factory=RuntimeSettings)
    capabilities: RuntimeCapabilities = field(default_factory=RuntimeCapabilities)
    health: ConnectionHealth = ConnectionHealth.UNOPENED
    health_diagnostics: tuple[str, ...] = ()

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


@dataclass(frozen=True, slots=True)
class ControlReceipt:
    """What one control operation established.

    ``result`` is the delivery result, not a job outcome. ``native_request_id``
    is a correlation fact only — native request ids are not durable
    idempotency guarantees, and a persisted operation id never justifies
    retrying a native mutation.
    """

    operation_id: str
    result: DeliveryResult
    native_turn_id: str | None = None
    native_request_id: str | None = None
    error_code: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        _bounded_id(self.operation_id, "receipt operation_id")
        if not isinstance(self.result, DeliveryResult):
            raise TypeError("runtime receipt result must be a DeliveryResult")
        _bounded_optional_text(
            self.native_turn_id, "receipt native_turn_id", limit=HARNESS_RUNTIME_ID_MAX_CHARS
        )
        _bounded_optional_text(
            self.native_request_id, "receipt native_request_id", limit=HARNESS_RUNTIME_ID_MAX_CHARS
        )
        _bounded_optional_text(
            self.error_code, "receipt error_code", limit=HARNESS_RUNTIME_ID_MAX_CHARS
        )
        _bounded_optional_text(self.error, "receipt error", limit=HARNESS_RUNTIME_ERROR_MAX_CHARS)


@dataclass(frozen=True, slots=True)
class NativeTurnOutcome:
    """Exact terminal evidence for one native turn.

    This is the evidence that completes a Theater job exactly and once: it is
    keyed by the exact native session/turn identity and carries the available
    result, its completeness, and its provenance. Status broadcasts are not
    terminal evidence; a snapshot-derived result is at most partial.
    """

    native_session_id: str
    native_turn_id: str
    terminal: NativeTurnTerminal
    result: str | None = None
    completeness: ResultCompleteness = ResultCompleteness.UNAVAILABLE
    provenance: ResultProvenance = ResultProvenance.NATIVE_EVIDENCE
    error_code: str | None = None
    error: str | None = None

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
    """The persisted identity of one participant's native runtime.

    Facts are exact: ``pid`` is only set after the daemon verified the backend
    process identity, and ``native_session_id`` only after the exact native
    session identity was discovered — never inferred from the working
    directory. Approval/model configuration remains on the backend; this value
    carries no credentials.
    """

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
    """A first-class declaration of a runtime's live channel.

    A live channel is not a transcript and not a database: it is the runtime's
    single live ``Source``, and it must never be encoded as an ordinary
    ``CompositeSource`` enrichment, whose enrichments cannot drive
    authoritative events or status. A future ``HybridSource`` composes this
    channel with the existing durable reader instead.
    """

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
    """One native notification or server request observed on a connection.

    ``request_id`` is set for server requests (approval requests and their
    kin). Theater records them and relies on the native resolution
    notification to learn their outcome; it must never send a response.
    """

    method: str
    params: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.method, str) or not self.method.strip():
            raise ValueError("runtime notification method must be a non-blank string")
        if not isinstance(self.params, Mapping):
            raise TypeError("runtime notification params must be a mapping")
        object.__setattr__(self, "params", freeze_json_mapping(self.params))
        _bounded_optional_text(
            self.request_id, "notification request_id", limit=HARNESS_RUNTIME_ID_MAX_CHARS
        )


class RuntimeConnection(ABC):
    """One bounded connection to a native backend endpoint.

    The injected implementation owns framing, request correlation, deadlines,
    and the single bounded receive loop. Plugin code sees only requests,
    notifications, and typed failures.
    """

    @abstractmethod
    async def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: float,
    ) -> Mapping[str, object]:
        """Send one request and await its correlated result.

        Raises :class:`RuntimeRequestError` for a remote error,
        :class:`RuntimeRequestTimeout` for a missed deadline, and
        :class:`RuntimeConnectionClosed` when the connection is gone.
        """

    @abstractmethod
    async def notify(self, method: str, params: Mapping[str, object]) -> None:
        """Send one notification; no result is expected."""

    @abstractmethod
    def notifications(self) -> AsyncIterator[RuntimeNotification]:
        """Iterate observed notifications and server requests.

        Implementations must buffer boundedly and never silently discard
        identity or terminal evidence; on overflow they degrade observation
        and surface it rather than inventing completion.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Close this connection without terminating the backend."""


class RuntimeIO(ABC):
    """The injected runtime I/O service seam.

    Implementations are daemon-owned shared helpers; a runtime created inside
    plugin code reaches native backends only through this public contract and
    never by importing daemon internals.
    """

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
    """The result of one read-only compatibility probe.

    Automatic selection means Theater-verified compatibility, not presumed
    vendor stability: ``policy`` names the tested compatibility policy and
    ``native_version`` the version it was verified against. Unknown or
    unsupported versions select legacy under ``wiring="auto"``; an explicit
    ``wiring="native"`` fails with the recorded reason.
    """

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
class RuntimeContext:
    """Immutable facts plus injected I/O for one runtime instance.

    Deliberately carries no Store and no Registry: a plugin that could touch
    daemon state would re-implement daemon policy per harness. ``io`` is the
    only way out.
    """

    participant_id: str
    cwd: str | None
    io: RuntimeIO
    endpoint: str | None = None
    config_path: Path | None = None
    approval: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    native_session_id: str | None = None

    def __post_init__(self) -> None:
        _bounded_id(self.participant_id, "context participant_id")
        if not isinstance(self.io, RuntimeIO):
            raise TypeError("runtime context io must be a RuntimeIO")
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


class RuntimeFactory(Protocol):
    """Create one runtime instance from immutable facts and injected I/O."""

    def __call__(self, context: RuntimeContext) -> HarnessRuntime: ...


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    """The runtime half of one harness manifest.

    ``probe`` is read-only, ``plan`` is pure, and ``factory`` produces the
    per-participant :class:`HarnessRuntime`. ``channel`` declares the single
    live channel a future ``HybridSource`` composes with the durable reader.
    """

    probe: RuntimeCompatibilityProbe
    plan: RuntimeBackendPlanner
    factory: RuntimeFactory
    channel: LiveChannelDeclaration


class HarnessRuntime(ABC):
    """One participant's live native runtime.

    The daemon creates exactly one instance per participant and shares it
    between observation and controls; the instance exposes exactly one live
    ``Source``. History reads go through durable readers and must never reach
    this object's controlling connection.
    """

    @abstractmethod
    async def open_session(
        self,
        *,
        mode: SessionOpenMode,
        native_session_id: str | None = None,
    ) -> RuntimeBinding:
        """Open, fork, or reconnect the native session and return its binding.

        ``NEW`` starts from the backend's own thread creation. ``FORK``
        preserves native fork semantics from ``native_session_id``.
        ``RECONNECT`` attaches to the exact existing ``native_session_id`` —
        identity mismatch must fail closed, never attach by working-directory
        resemblance.
        """

    @abstractmethod
    async def frontend_plan(self, *, native_session_id: str) -> LaunchPlan:
        """Plan native UI attachment only; the plan carries no initial prompt."""

    @abstractmethod
    def live_source(self) -> Source:
        """The single live ``Source`` shared by observation and controls."""

    @abstractmethod
    async def snapshot(self) -> RuntimeSnapshot:
        """Exact session identity, active turn, interaction, settings, health."""

    @abstractmethod
    async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
        """Submit one prompt as a new native turn.

        If a simultaneous native-UI submission absorbs this message into an
        already-active turn, report the actual returned turn; never fabricate
        a second one.
        """

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
        """Update only the supplied settings, idle-only, without emulating
        unconfirmed application. Uncertain application stays visibly
        uncertain.
        """

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
    "RuntimeFactory",
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
]
