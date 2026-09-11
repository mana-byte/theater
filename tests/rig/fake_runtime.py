"""A small in-memory fake runtime for downstream runtime workers.

Wave 2/3 workers test the daemon runtime manager, the control service, and
lifecycle integration against this fake instead of a real backend. It
implements the frozen ``HarnessRuntime`` contract faithfully:

* ``open_session`` mints or reuses an exact native session identity,
* ``frontend_plan`` returns a promptless plan,
* ``live_source`` is one shared ``Source`` whose batches tests enqueue,
* ``send``/``steer``/``interrupt``/``update_settings`` return ``ControlReceipt``
  values from configurable policy,
* ``aclose`` disconnects without terminating anything.

Test helper only: production code must not import it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path

from theater.harness.contracts.channels import (
    ChannelCapability,
    ChannelDeclaration,
    ChannelKind,
    SignalKind,
    SignalOwnership,
)
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ConnectionHealth,
    ControlReceipt,
    DeliveryResult,
    HarnessRuntime,
    LiveChannelDeclaration,
    NativeHumanInteraction,
    NativeTurnOutcome,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
    RuntimeBinding,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeCompatibility,
    RuntimeConnection,
    RuntimeContext,
    RuntimeExecutionState,
    RuntimeIO,
    RuntimeLifecyclePhase,
    RuntimeManifest,
    RuntimeNotification,
    RuntimePlan,
    RuntimeSettings,
    RuntimeSnapshot,
    RuntimeWiring,
    SessionOpenMode,
)
from theater.harness.contracts.source import Batch, Source

_TURN_SEQ = count(1)


@dataclass
class FakeRuntimeState:
    """Everything the fake runtime tracks; tests may tweak fields directly."""

    participant_id: str
    backend_generation: int = 1
    native_session_id: str | None = None
    native_turn_id: str | None = None
    pending_interaction: NativeHumanInteraction | None = None
    health: ConnectionHealth = ConnectionHealth.CONNECTED
    #: Tests may force UNKNOWN/ACTIVE explicitly.  Otherwise ``snapshot``
    #: derives ACTIVE from a live turn and IDLE from a live bound session,
    #: matching the native-runtime contract rather than treating ``None`` as
    #: implicit proof of idle.
    execution_state: RuntimeExecutionState = RuntimeExecutionState.IDLE
    connected: bool = True
    closed: bool = False
    #: Backend "process" survives aclose(); tests assert it stays True.
    backend_alive: bool = False
    #: Reject every control with this code while set.
    reject_code: str | None = None
    #: Capabilities reported as unavailable, capability -> reason.
    unavailable: dict[RuntimeCapability, CapabilityUnavailableReason] = field(default_factory=dict)
    settings: dict[str, str] = field(default_factory=dict)
    sent: list[str] = field(default_factory=list)
    steered: list[tuple[str, str]] = field(default_factory=list)
    interrupted: list[str | None] = field(default_factory=list)
    #: Batches the live source hands out, one per read.
    batches: list[Batch] = field(default_factory=list)


class FakeSource(Source):
    """Drains test-enqueued batches; never touches the filesystem."""

    def __init__(self, state: FakeRuntimeState) -> None:
        self._state = state

    async def read(self) -> Batch:
        if self._state.batches:
            return self._state.batches.pop(0)
        return Batch()


class FakeRuntimeConnection(RuntimeConnection):
    """Records requests; optional canned notifications for the receive loop."""

    def __init__(self, state: FakeRuntimeState) -> None:
        self._state = state
        self.requests: list[tuple[str, Mapping[str, object]]] = []
        self.notifications_sent: list[tuple[str, Mapping[str, object]]] = []
        self.inbound: list[RuntimeNotification] = []

    async def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: float,
    ) -> Mapping[str, object]:
        del timeout
        if not self._state.connected:
            raise ConnectionError("fake connection closed")
        self.requests.append((method, dict(params)))
        return {"ok": True, "method": method}

    async def notify(self, method: str, params: Mapping[str, object]) -> None:
        if not self._state.connected:
            raise ConnectionError("fake connection closed")
        self.notifications_sent.append((method, dict(params)))

    async def notifications(self) -> AsyncIterator[RuntimeNotification]:
        while True:
            if self.inbound:
                yield self.inbound.pop(0)
                continue
            await asyncio.sleep(0)

    async def aclose(self) -> None:
        self._state.connected = False


class FakeRuntimeIO(RuntimeIO):
    """Connects to a fake connection bound to one runtime state."""

    def __init__(self, state: FakeRuntimeState) -> None:
        self._state = state
        self.connection: FakeRuntimeConnection | None = None

    async def connect(self, endpoint: str, *, timeout: float) -> RuntimeConnection:
        del timeout
        if self._state.closed and not self._state.backend_alive:
            raise ConnectionError(f"fake backend gone at {endpoint}")
        self.connection = FakeRuntimeConnection(self._state)
        self._state.connected = True
        return self.connection


class FakeRuntime(HarnessRuntime):
    """The contract-faithful in-memory runtime."""

    def __init__(self, context: RuntimeContext) -> None:
        self.context = context
        if isinstance(context.io, FakeRuntimeIO):
            # Share the state the injected I/O already holds so connection
            # failures and runtime snapshots agree on one truth.
            self.state = context.io._state
        else:
            self.state = FakeRuntimeState(
                participant_id=context.participant_id,
                backend_generation=context.backend_generation,
            )
        self._source = FakeSource(self.state)

    # ---- session --------------------------------------------------------

    async def open_session(
        self,
        *,
        mode: SessionOpenMode,
        native_session_id: str | None = None,
    ) -> RuntimeBinding:
        if mode is SessionOpenMode.RECONNECT:
            if native_session_id is None:
                raise ValueError("reconnect requires the exact native session id")
            self.state.native_session_id = native_session_id
        else:
            self.state.native_session_id = f"thread-{next(_TURN_SEQ)}"
        self.state.backend_alive = True
        self.state.connected = True
        self.state.health = ConnectionHealth.CONNECTED
        return RuntimeBinding(
            participant_id=self.state.participant_id,
            backend_generation=self.state.backend_generation,
            wiring=RuntimeWiring.NATIVE,
            lifecycle=RuntimeLifecyclePhase.BOUND,
            endpoint=self.context.endpoint,
            pid=4242,
            native_session_id=self.state.native_session_id,
            protocol="websocket-jsonrpc",
            protocol_version="0.154.0",
            native_version="0.154.0",
            compatibility_policy="codex-0.154-verified",
        )

    async def frontend_plan(self, *, native_session_id: str | None = None) -> LaunchPlan:
        # Native UI attachment only: no initial prompt in the plan, ever.
        endpoint = self.context.endpoint or "unix:///tmp/fake.sock"
        if native_session_id is None:
            # UI-first NEW order: the promptless fresh UI creates the session
            # itself; open_session(mode=NEW) then opens exactly that session.
            return LaunchPlan(argv=["fake-cli", "--remote", endpoint])
        return LaunchPlan(argv=["fake-cli", "--remote", endpoint, "resume", native_session_id])

    def live_source(self) -> Source:
        return self._source

    async def snapshot(self) -> RuntimeSnapshot:
        execution_state = self.state.execution_state
        if self.state.native_turn_id is not None:
            execution_state = RuntimeExecutionState.ACTIVE
        elif execution_state is RuntimeExecutionState.IDLE and (
            not self.state.connected or self.state.native_session_id is None
        ):
            # The fake's convenient default is an explicit idle report only
            # after it has a live, exact session.  A disconnected/unbound
            # fake models the frozen fail-closed UNKNOWN contract instead.
            execution_state = RuntimeExecutionState.UNKNOWN
        return RuntimeSnapshot(
            participant_id=self.state.participant_id,
            backend_generation=self.state.backend_generation,
            native_session_id=self.state.native_session_id,
            native_turn_id=self.state.native_turn_id,
            pending_interaction=self.state.pending_interaction,
            settings=RuntimeSettings(
                model=self.state.settings.get("model"),
                reasoning_effort=self.state.settings.get("reasoning_effort"),
            ),
            capabilities=RuntimeCapabilities(
                available=set(RuntimeCapability) - set(self.state.unavailable),
                unavailable_reasons=self.state.unavailable,
            ),
            health=self.state.health,
            execution_state=execution_state,
        )

    # ---- controls --------------------------------------------------------

    def _receipt(self, operation_id: str, turn: str | None = None) -> ControlReceipt:
        """A faithful receipt names the operation it answers."""
        return ControlReceipt(
            operation_id=operation_id,
            result=DeliveryResult.ACCEPTED,
            native_turn_id=turn,
        )

    async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
        if self.state.reject_code is not None:
            return ControlReceipt(
                operation_id=operation_id,
                result=DeliveryResult.REJECTED,
                error_code=self.state.reject_code,
                error="fake rejection",
            )
        self.state.sent.append(prompt)
        self.state.native_turn_id = f"turn-{next(_TURN_SEQ)}"
        return self._receipt(operation_id, self.state.native_turn_id)

    async def steer(
        self,
        *,
        operation_id: str,
        native_turn_id: str,
        prompt: str,
    ) -> ControlReceipt:
        if self.state.native_turn_id != native_turn_id:
            return ControlReceipt(
                operation_id=operation_id,
                result=DeliveryResult.REJECTED,
                error_code="stale_turn",
                error="expectedTurnId no longer active",
            )
        self.state.steered.append((native_turn_id, prompt))
        return self._receipt(operation_id, native_turn_id)

    async def interrupt(
        self,
        *,
        operation_id: str,
        native_turn_id: str | None = None,
    ) -> ControlReceipt:
        self.state.interrupted.append(native_turn_id)
        turn = native_turn_id or self.state.native_turn_id
        self.state.native_turn_id = None
        return self._receipt(operation_id, turn)

    async def update_settings(
        self,
        *,
        operation_id: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ControlReceipt:
        if CapabilityUnavailableReason.GATED_BY_BACKEND in self.state.unavailable.values():
            return ControlReceipt(
                operation_id=operation_id,
                result=DeliveryResult.REJECTED,
                error_code="settings_unavailable",
                error="fake backend gates settings",
            )
        if model is not None:
            self.state.settings["model"] = model
        if reasoning_effort is not None:
            self.state.settings["reasoning_effort"] = reasoning_effort
        return self._receipt(operation_id, self.state.native_turn_id)

    async def aclose(self) -> None:
        """Disconnect only; the fake backend stays alive for tests to assert."""
        self.state.connected = False
        self.state.closed = True
        self.state.health = ConnectionHealth.DISCONNECTED
        self.state.execution_state = RuntimeExecutionState.UNKNOWN


def completed_outcome(state: FakeRuntimeState, result: str = "done") -> NativeTurnOutcome:
    """A terminal outcome for the current fake turn; tests enqueue these."""
    assert state.native_session_id is not None
    turn = state.native_turn_id or f"turn-{next(_TURN_SEQ)}"
    return NativeTurnOutcome(
        native_session_id=state.native_session_id,
        native_turn_id=turn,
        terminal=NativeTurnTerminal.COMPLETED,
        result=result,
        completeness=ResultCompleteness.COMPLETE,
        provenance=ResultProvenance.NATIVE_EVIDENCE,
    )


def fake_runtime_manifest() -> RuntimeManifest:
    """A valid runtime manifest backed by the fake runtime."""

    def probe(context) -> RuntimeCompatibility:
        del context
        return RuntimeCompatibility(
            supported=True,
            policy="fake-verified",
            native_version="0.154.0",
        )

    def plan(context) -> RuntimePlan:
        return RuntimePlan(
            backend=LaunchPlan(argv=["fake-backend", "--listen", f"unix://{context.endpoint}"]),
            endpoint=context.endpoint,
        )

    def factory(context: RuntimeContext) -> HarnessRuntime:
        return FakeRuntime(context)

    return RuntimeManifest(
        probe=probe,
        plan=plan,
        factory=factory,
        channel=LiveChannelDeclaration(
            channel=ChannelDeclaration(
                id="live",
                kind=ChannelKind.LIVE,
                capabilities=(
                    ChannelCapability(SignalKind.CONTENT, SignalOwnership.PRIMARY),
                    ChannelCapability(SignalKind.TURN, SignalOwnership.PRIMARY),
                ),
            ),
        ),
    )


def fake_runtime_context(participant_id: str = "fake-1") -> RuntimeContext:
    """A minimal context with injected fake I/O for opening a fake runtime."""
    state = FakeRuntimeState(participant_id=participant_id)
    return RuntimeContext(
        participant_id=participant_id,
        cwd=None,
        io=FakeRuntimeIO(state),
        backend_generation=state.backend_generation,
        endpoint="unix:///tmp/fake.sock",
        config_path=Path("/tmp/fake-config.json"),
    )


__all__ = [
    "FakeRuntime",
    "FakeRuntimeConnection",
    "FakeRuntimeIO",
    "FakeRuntimeState",
    "FakeSource",
    "completed_outcome",
    "fake_runtime_context",
    "fake_runtime_manifest",
]
