"""Pi's additive stock-extension frontend runtime.

Pi is launched through its ordinary interactive CLI.  Its bundled extension
opens an authenticated, loopback-only NDJSON connection to a daemon-owned
frontend host; that host injects the small peer protocol defined here.  This
module deliberately does not declare a generic ``RuntimeManifest``: Theater's
existing detached-WebSocket runtime lifecycle is not Pi's stock-UI lifecycle.
The parent composition layer owns that activation and adapts the peer without
changing Pi's legacy pane controls.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import subprocess
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from theater.harness.contracts.channels import ChannelHealth, ChannelHealthState
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ConnectionHealth,
    ControlReceipt,
    DeliveryResult,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeCompatibility,
    RuntimeExecutionState,
    RuntimeProbeContext,
    RuntimeRequestError,
    RuntimeSettings,
    RuntimeSnapshot,
)
from theater.harness.contracts.source import Batch, Source
from theater.models import Status

from .constants import PI_BINARY
from .frontend import PI_FRONTEND_MAX_VALUE_CHARS, PI_FRONTEND_PROTOCOL

PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY = "pi-extension-0.84.x-supported"
PI_FRONTEND_RUNTIME_PROBE_TIMEOUT_SECONDS = 5.0
PI_FRONTEND_CONTROL_TIMEOUT_SECONDS = 10.0
PI_FRONTEND_DIAGNOSTICS_MAX = 8
PI_FRONTEND_HISTORY_MAX = 64
PI_FRONTEND_CHANNEL_ID = "pi-frontend-live"

_VERSION_TOKEN = re.compile(r"(?<![\w.])(?P<version>0\.(?P<minor>\d+)\.(?P<patch>\d+))(?![\w.-])")
_REJECTED_SETTINGS_ERRORS = frozenset(
    {
        "busy",
        "invalid_request",
        "model_update_proof_gated",
        "model_unavailable",
        "not_ready",
        "unsupported_thinking",
        "wrong_session",
    }
)


class PiFrontendProtocolError(ValueError):
    """A bridge frame did not meet the bounded frontend protocol."""


@runtime_checkable
class PiFrontendPeer(Protocol):
    """Injected authenticated connection to one Pi extension bridge.

    The daemon-owned host is responsible for NDJSON framing, hello-token
    authentication, one-peer ownership, and bounded buffering.  Pi code sees
    only this narrow request/notification/close interface, rather than the
    Codex-specific WebSocket-over-Unix ``RuntimeConnection``.
    """

    async def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: float,
    ) -> Mapping[str, object]:
        """Send one correlated request to the live extension."""

    def notifications(self) -> AsyncIterator[Mapping[str, object]]:
        """Yield extension ``event``, ``snapshot``, and ``history`` frames."""

    async def aclose(self) -> None:
        """Disconnect Theater only; never terminate Pi or its native work."""


@dataclass(frozen=True, slots=True)
class _FrontendSnapshot:
    native_session_id: str
    bridge_epoch: int
    snapshot_revision: int
    sequence: int
    settings: RuntimeSettings
    execution_state: RuntimeExecutionState
    settings_available: bool
    model_update_available: bool
    reasoning_effort_update_available: bool


def _bounded_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > PI_FRONTEND_MAX_VALUE_CHARS:
        raise PiFrontendProtocolError(f"Pi frontend {label} must be a bounded non-blank string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _bounded_string(value, label)


def _decode_execution_state(value: object) -> RuntimeExecutionState:
    if not isinstance(value, str):
        raise PiFrontendProtocolError("Pi frontend execution_state is invalid")
    try:
        return RuntimeExecutionState(value)
    except ValueError as exc:
        raise PiFrontendProtocolError("Pi frontend execution_state is invalid") from exc


def _decode_bridge_epoch(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise PiFrontendProtocolError(f"Pi frontend {label} is invalid")
    return value


def _decode_sequence(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise PiFrontendProtocolError(f"Pi frontend {label} is invalid")
    return value


def _decode_settings(value: object) -> RuntimeSettings:
    if not isinstance(value, Mapping):
        raise PiFrontendProtocolError("Pi frontend snapshot has no settings object")
    return RuntimeSettings(
        model=_optional_string(value.get("model"), "settings model"),
        reasoning_effort=_optional_string(value.get("reasoning_effort"), "settings reasoning"),
    )


def _decode_snapshot(value: object) -> _FrontendSnapshot:
    if not isinstance(value, Mapping):
        raise PiFrontendProtocolError("Pi frontend snapshot must be an object")
    if value.get("protocol") != PI_FRONTEND_PROTOCOL:
        raise PiFrontendProtocolError("Pi frontend snapshot has an unsupported protocol")
    capabilities = value.get("capabilities")
    if (
        not isinstance(capabilities, Mapping)
        or not isinstance(capabilities.get("settings_update"), bool)
        or not isinstance(capabilities.get("model_update"), bool)
        or not isinstance(capabilities.get("reasoning_effort_update"), bool)
    ):
        raise PiFrontendProtocolError("Pi frontend snapshot has invalid capabilities")
    return _FrontendSnapshot(
        native_session_id=_bounded_string(value.get("native_session_id"), "native session id"),
        bridge_epoch=_decode_bridge_epoch(value.get("bridge_epoch"), "snapshot bridge epoch"),
        snapshot_revision=_decode_bridge_epoch(value.get("snapshot_revision"), "snapshot revision"),
        sequence=_decode_sequence(value.get("sequence"), "snapshot sequence"),
        settings=_decode_settings(value.get("settings")),
        execution_state=_decode_execution_state(value.get("execution_state")),
        settings_available=capabilities["settings_update"],
        model_update_available=capabilities["model_update"],
        reasoning_effort_update_available=capabilities["reasoning_effort_update"],
    )


def _decode_event(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PiFrontendProtocolError("Pi frontend event must be an object")
    _bounded_string(value.get("name"), "event name")
    _bounded_string(value.get("native_session_id"), "event native session id")
    _decode_bridge_epoch(value.get("bridge_epoch"), "event bridge epoch")
    _decode_sequence(value.get("sequence"), "event sequence")
    return value


def _decode_notification(value: object) -> tuple[str, Mapping[str, object]]:
    if not isinstance(value, Mapping):
        raise PiFrontendProtocolError("Pi frontend notification must be an object")
    kind = value.get("type")
    if kind == "snapshot":
        snapshot = value.get("snapshot")
        _decode_snapshot(snapshot)
        assert isinstance(snapshot, Mapping)
        return kind, snapshot
    if kind == "event":
        event = value.get("event")
        decoded = _decode_event(event)
        return kind, decoded
    if kind == "history":
        events = value.get("events")
        if not isinstance(events, (list, tuple)) or len(events) > PI_FRONTEND_HISTORY_MAX:
            raise PiFrontendProtocolError("Pi frontend history exceeds its event bound")
        for event in events:
            _decode_event(event)
        snapshot = value.get("snapshot")
        if snapshot is not None:
            _decode_snapshot(snapshot)
        return kind, value
    raise PiFrontendProtocolError("Pi frontend notification type is unsupported")


def parse_pi_version(output: str) -> str | None:
    """Extract a stable ``0.x.y`` Pi release version from ``pi --version``."""
    match = _VERSION_TOKEN.search(output)
    return None if match is None else match.group("version")


def _version_in_supported_range(version: str) -> bool:
    match = _VERSION_TOKEN.fullmatch(version)
    if match is None:
        return False
    parsed = (0, int(match.group("minor")), int(match.group("patch")))
    return (0, 84, 4) <= parsed < (0, 85, 0)


def _unsupported_probe(reason: str, *, version: str | None = None) -> RuntimeCompatibility:
    return RuntimeCompatibility(
        supported=False,
        policy=PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY,
        native_version=version,
        reason=(
            f"{reason}; Pi keeps its existing legacy launch and controls until a compatible "
            "frontend bridge is available"
        ),
    )


def probe_pi_frontend_compatibility(context: RuntimeProbeContext) -> RuntimeCompatibility:
    """Run only read-only executable/CLI-surface checks for the Pi bridge.

    This establishes an installed release in the declared compatible range.
    ``pi --help`` is deliberately not used: stock Pi can touch its settings
    lock while rendering help, so it is not a non-mutating probe.  The CLI
    flags, lifecycle ordering, settings persistence, and reconnect semantics
    remain release-conformance gates with an isolated stock Pi session.
    """
    binary = context.binary or PI_BINARY
    try:
        version_result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=PI_FRONTEND_RUNTIME_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _unsupported_probe(
            f"Pi compatibility probe could not run {binary!r} --version: {exc}"
        )
    if version_result.returncode != 0:
        return _unsupported_probe(f"Pi --version exited with {version_result.returncode}")
    version = parse_pi_version(f"{version_result.stdout}\n{version_result.stderr}")
    if version is None:
        return _unsupported_probe("Pi --version did not report a stable 0.x.y release")
    if not _version_in_supported_range(version):
        return _unsupported_probe(
            f"Pi {version} is outside supported range >=0.84.4,<0.85.0", version=version
        )
    return RuntimeCompatibility(
        supported=True,
        policy=PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY,
        native_version=version,
    )


class PiFrontendRuntime:
    """One Pi stock-UI extension session over an injected frontend peer.

    The runtime has no generic detached-backend lifecycle.  It only consumes
    authenticated bridge observations and performs the separately proven
    session-local thinking operation. Model mutation, ``send``, ``steer``,
    and ``interrupt`` return explicit proof-gated refusals so an incomplete
    parent integration cannot silently replace Theater's existing legacy paths.
    """

    def __init__(
        self,
        *,
        participant_id: str,
        backend_generation: int,
        peer: PiFrontendPeer,
        expected_native_session_id: str | None = None,
        native_version: str | None = None,
    ) -> None:
        _bounded_string(participant_id, "participant id")
        if type(backend_generation) is not int or backend_generation < 0:
            raise ValueError("Pi frontend backend_generation must be a non-negative integer")
        if not isinstance(peer, PiFrontendPeer):
            raise TypeError("Pi frontend peer must implement PiFrontendPeer")
        if expected_native_session_id is not None:
            _bounded_string(expected_native_session_id, "expected native session id")
        if native_version is not None:
            _bounded_string(native_version, "native version")
        self._participant_id = participant_id
        self._backend_generation = backend_generation
        self._peer: PiFrontendPeer | None = peer
        self._peer_generation = 0
        self._expected_native_session_id = expected_native_session_id
        self._native_version = native_version
        self._native_session_id: str | None = None
        self._bridge_epoch: int | None = None
        self._snapshot_revision: int | None = None
        self._settings = RuntimeSettings()
        self._execution_state = RuntimeExecutionState.UNKNOWN
        self._settings_available = False
        self._health = ConnectionHealth.UNOPENED
        self._diagnostics: deque[str] = deque(maxlen=PI_FRONTEND_DIAGNOSTICS_MAX)
        self._receive_task: asyncio.Task[None] | None = None
        self._live_source: PiFrontendLiveSource | None = None
        self._activity_callback: Callable[[], None] | None = None
        self._session_epoch = 0
        self._last_sequence = -1
        self._revision = 0
        self._accepted = 0
        self._dropped = 0
        self._closed = False

    async def attach(self, *, native_session_id: str | None = None) -> RuntimeSnapshot:
        """Confirm the exact live Pi session before exposing native settings."""
        expected = native_session_id or self._expected_native_session_id
        if expected is not None:
            _bounded_string(expected, "expected native session id")
        snapshot = await self.snapshot()
        if snapshot.health is not ConnectionHealth.CONNECTED or snapshot.native_session_id is None:
            raise RuntimeError("Pi frontend bridge did not confirm a live native session")
        if expected is not None and snapshot.native_session_id != expected:
            raise RuntimeError(
                "Pi frontend bridge attached a different native session "
                f"({snapshot.native_session_id!r} != {expected!r}); refusing to bind"
            )
        self._start_receiver()
        return snapshot

    async def reconnect(self, peer: PiFrontendPeer) -> RuntimeSnapshot:
        """Explicitly install a replacement host peer without replaying mutations."""
        if not isinstance(peer, PiFrontendPeer):
            raise TypeError("Pi frontend peer must implement PiFrontendPeer")
        old_peer = self._peer
        old_task = self._receive_task
        self._peer_generation += 1
        self._peer = peer
        self._receive_task = None
        # A replacement host may be attached to a newly loaded extension whose
        # local epoch restarts at one.  Only this explicit peer replacement may
        # discard the prior epoch/sequence; an ordinary delayed notification
        # never gets that authority.
        self._reset_for_peer_reconnect()
        if old_task is not None:
            old_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await old_task
        if old_peer is not None and old_peer is not peer:
            with contextlib.suppress(Exception):
                await old_peer.aclose()
        return await self.attach(native_session_id=self._expected_native_session_id)

    def live_source(self) -> Source:
        """Return one status-only live source; durable Pi JSONL remains authoritative."""
        if self._live_source is None:
            self._live_source = PiFrontendLiveSource(self)
        return self._live_source

    async def snapshot(self) -> RuntimeSnapshot:
        """Read an exact current bridge snapshot, failing closed on transport loss."""
        peer = self._peer
        if peer is None or self._closed:
            return self._runtime_snapshot()
        peer_generation = self._peer_generation
        try:
            result = await peer.request(
                "pi.snapshot", {}, timeout=PI_FRONTEND_CONTROL_TIMEOUT_SECONDS
            )
            decoded = _decode_snapshot(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._mark_disconnected(f"pi.snapshot failed: {type(exc).__name__}: {exc}")
            return self._runtime_snapshot()
        if peer is not self._peer or peer_generation != self._peer_generation:
            self._mark_disconnected("Pi frontend peer changed while snapshot was in flight")
            return self._runtime_snapshot()
        if not self._apply_snapshot(decoded):
            self._diagnostic("Pi frontend rejected a stale or conflicting snapshot")
            self._health = ConnectionHealth.DEGRADED
            self._settings_available = False
            self._touch()
        return self._runtime_snapshot()

    async def update_settings(  # noqa: PLR0912
        self,
        *,
        operation_id: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ControlReceipt:
        """Request one idle/session-guarded, confirmed Pi settings update.

        A timeout, bridge replacement, session switch, or unconfirmed readback
        is ``UNKNOWN``.  No request is replayed and no later legacy control is
        substituted for it.
        """
        if model is None and reasoning_effort is None:
            return self._rejected(operation_id, "invalid_request", "no Pi setting was supplied")
        # Pi's public setModel binding awaits provider authentication before its
        # session mutation and has no supported expected-session guard across
        # that await.  Keep it proof-gated end-to-end: a mixed request cannot
        # change thinking either, and Theater never reports UNKNOWN only after
        # a model may already have landed in a later human session.
        if model is not None:
            return self._rejected(
                operation_id,
                "model_update_proof_gated",
                "Pi model updates remain disabled pending an atomic public session guard",
            )
        if reasoning_effort is not None:
            try:
                _bounded_string(reasoning_effort, "requested reasoning effort")
            except PiFrontendProtocolError as exc:
                return self._rejected(operation_id, "invalid_request", str(exc))

        before = await self.snapshot()
        session_id = before.native_session_id
        if before.health is not ConnectionHealth.CONNECTED or session_id is None:
            return self._rejected(
                operation_id,
                "native_settings_unavailable",
                "Pi frontend settings are unavailable while its bridge is disconnected",
            )
        if before.execution_state is not RuntimeExecutionState.IDLE:
            return self._rejected(
                operation_id,
                "settings_not_idle",
                "Pi settings require a bridge-confirmed idle session",
            )

        peer = self._peer
        if peer is None:
            return self._rejected(
                operation_id,
                "native_settings_unavailable",
                "Pi frontend settings are unavailable while its bridge is disconnected",
            )
        session_epoch = self._session_epoch
        bridge_epoch = self._bridge_epoch
        peer_generation = self._peer_generation
        params: dict[str, object] = {
            "operation_id": operation_id,
            "native_session_id": session_id,
        }
        if model is not None:
            params["model"] = model
        if reasoning_effort is not None:
            params["reasoning_effort"] = reasoning_effort
        try:
            result = await peer.request(
                "pi.settings.update", params, timeout=PI_FRONTEND_CONTROL_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            raise
        except RuntimeRequestError as exc:
            if isinstance(exc.code, str) and exc.code in _REJECTED_SETTINGS_ERRORS:
                return self._rejected(operation_id, exc.code, exc.message)
            return self._unknown(operation_id, str(exc.code), exc.message)
        except Exception as exc:
            self._mark_disconnected(f"pi.settings.update failed: {type(exc).__name__}: {exc}")
            return self._unknown(
                operation_id,
                "settings_delivery_unknown",
                "Pi settings delivery became uncertain; Theater did not replay it",
            )

        try:
            confirmed = self._decode_settings_result(result)
        except PiFrontendProtocolError as exc:
            self._diagnostic(str(exc))
            self._health = ConnectionHealth.DEGRADED
            self._touch()
            return self._unknown(
                operation_id,
                "settings_unconfirmed",
                "Pi settings response was malformed; Theater did not replay it",
            )
        if (
            peer is not self._peer
            or peer_generation != self._peer_generation
            or session_epoch != self._session_epoch
            or bridge_epoch is None
            or confirmed["native_session_id"] != session_id
            or confirmed["operation_id"] != operation_id
        ):
            return self._unknown(
                operation_id,
                "session_changed",
                "Pi session or bridge changed while settings were in flight",
            )

        snapshot = confirmed["snapshot"]
        assert isinstance(snapshot, _FrontendSnapshot)
        if snapshot.bridge_epoch != bridge_epoch or not self._apply_snapshot(snapshot):
            return self._unknown(
                operation_id,
                "session_changed",
                "Pi session or bridge changed before settings readback",
            )
        # Pi may clamp a thinking level.  The exact effective value is now in
        # ``snapshot().settings.reasoning_effort``; a clamp is confirmed, not
        # a fabricated success.
        return ControlReceipt(operation_id=operation_id, result=DeliveryResult.ACCEPTED)

    async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
        """Refuse unproven native prompt admission without touching legacy send."""
        del prompt
        return self._proof_gated(operation_id, "send")

    async def steer(self, *, operation_id: str, native_turn_id: str, prompt: str) -> ControlReceipt:
        """Refuse unproven exact-turn steering without touching legacy send."""
        del native_turn_id, prompt
        return self._proof_gated(operation_id, "steer")

    async def interrupt(
        self, *, operation_id: str, native_turn_id: str | None = None
    ) -> ControlReceipt:
        """Refuse unproven exact-turn interruption without touching legacy Escape."""
        del native_turn_id
        return self._proof_gated(operation_id, "interrupt")

    async def aclose(self) -> None:
        """Release the host connection without affecting the stock Pi process."""
        if self._closed:
            return
        self._closed = True
        self._peer_generation += 1
        task, self._receive_task = self._receive_task, None
        peer, self._peer = self._peer, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if peer is not None:
            with contextlib.suppress(Exception):
                await peer.aclose()
        self._mark_disconnected("Pi frontend runtime was closed")

    def set_activity_callback(self, callback: Callable[[], None] | None) -> None:
        """Install the observer's optional arrival wake callback."""
        self._activity_callback = callback

    def _decode_settings_result(self, value: object) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise PiFrontendProtocolError("Pi settings response must be an object")
        if value.get("status") != "accepted":
            raise PiFrontendProtocolError("Pi settings response was not accepted")
        operation_id = _bounded_string(value.get("operation_id"), "settings operation id")
        session_id = _bounded_string(value.get("native_session_id"), "settings native session id")
        snapshot_data = {
            "protocol": value.get("protocol"),
            "native_session_id": session_id,
            "bridge_epoch": value.get("bridge_epoch"),
            "snapshot_revision": value.get("snapshot_revision"),
            "sequence": value.get("sequence"),
            "settings": value.get("settings"),
            "execution_state": value.get("execution_state"),
            "capabilities": value.get("capabilities"),
        }
        return {
            "operation_id": operation_id,
            "native_session_id": session_id,
            "snapshot": _decode_snapshot(snapshot_data),
        }

    def _start_receiver(self) -> None:
        if self._closed or self._peer is None:
            return
        if self._receive_task is not None and not self._receive_task.done():
            return
        peer = self._peer
        peer_generation = self._peer_generation
        self._receive_task = asyncio.create_task(
            self._receive_notifications(peer, peer_generation),
            name=f"pi-frontend-{self._participant_id}",
        )

    async def _receive_notifications(self, peer: PiFrontendPeer, peer_generation: int) -> None:
        try:
            async for frame in peer.notifications():
                if (
                    self._closed
                    or peer is not self._peer
                    or peer_generation != self._peer_generation
                ):
                    return
                try:
                    kind, payload = _decode_notification(frame)
                    if self._apply_notification(kind, payload):
                        self._accepted += 1
                    else:
                        self._dropped += 1
                except PiFrontendProtocolError as exc:
                    self._dropped += 1
                    self._diagnostic(str(exc))
                    self._health = ConnectionHealth.DEGRADED
                    self._touch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if peer is self._peer and peer_generation == self._peer_generation:
                self._mark_disconnected(
                    f"Pi frontend notification stream failed: {type(exc).__name__}: {exc}"
                )
        else:
            if peer is self._peer and peer_generation == self._peer_generation and not self._closed:
                self._mark_disconnected("Pi frontend notification stream closed")

    def _apply_notification(self, kind: str, payload: Mapping[str, object]) -> bool:
        if kind == "snapshot":
            return self._apply_snapshot(_decode_snapshot(payload))
        if kind == "event":
            return self._apply_event(payload)
        snapshot = payload.get("snapshot")
        applied = False
        if snapshot is not None:
            applied = self._apply_snapshot(_decode_snapshot(snapshot))
        events = payload["events"]
        assert isinstance(events, (list, tuple))
        for event in events:
            applied = self._apply_event(_decode_event(event)) or applied
        return applied

    def _apply_snapshot(self, snapshot: _FrontendSnapshot) -> bool:
        """Apply an exact current snapshot without allowing identity rollback.

        Bridge epochs are extension-process-local generations.  A higher epoch
        can establish the next live Pi session; a same-epoch snapshot may only
        refresh that exact session.  An older history frame is observation-only
        noise and must not re-enable settings or change idle state.
        """
        current_epoch = self._bridge_epoch
        if current_epoch is None:
            self._bridge_epoch = snapshot.bridge_epoch
            self._native_session_id = snapshot.native_session_id
            self._snapshot_revision = snapshot.snapshot_revision
            self._session_epoch += 1
            self._last_sequence = snapshot.sequence
        elif snapshot.bridge_epoch < current_epoch:
            return False
        elif snapshot.bridge_epoch == current_epoch:
            if (
                self._native_session_id != snapshot.native_session_id
                or self._snapshot_revision is None
                or snapshot.snapshot_revision < self._snapshot_revision
                or snapshot.sequence < self._last_sequence
            ):
                return False
            self._snapshot_revision = snapshot.snapshot_revision
            self._last_sequence = max(self._last_sequence, snapshot.sequence)
        else:
            self._bridge_epoch = snapshot.bridge_epoch
            self._native_session_id = snapshot.native_session_id
            self._snapshot_revision = snapshot.snapshot_revision
            # A newer extension generation invalidates an in-flight receipt
            # even when Pi happened to retain the same session identifier.
            self._session_epoch += 1
            self._last_sequence = snapshot.sequence
        self._settings = snapshot.settings
        self._execution_state = snapshot.execution_state
        self._settings_available = (
            snapshot.settings_available and snapshot.reasoning_effort_update_available
        )
        self._health = ConnectionHealth.CONNECTED
        self._touch()
        return True

    def _apply_event(self, event: Mapping[str, object]) -> bool:
        name = _bounded_string(event.get("name"), "event name")
        session_id = _bounded_string(event.get("native_session_id"), "event native session id")
        bridge_epoch = _decode_bridge_epoch(event.get("bridge_epoch"), "event bridge epoch")
        sequence = event.get("sequence")
        assert type(sequence) is int
        # Events are never identity authority.  Require the exact snapshot's
        # live session and epoch before even considering sequence/state.
        if (
            self._bridge_epoch is None
            or bridge_epoch != self._bridge_epoch
            or self._native_session_id is None
            or session_id != self._native_session_id
            or sequence <= self._last_sequence
        ):
            return False
        self._last_sequence = sequence

        if name == "session_shutdown":
            # Keep epoch/sequence through shutdown.  Only a newer exact
            # snapshot (or explicit peer reconnect) can establish a successor.
            self._native_session_id = None
            self._session_epoch += 1
            self._execution_state = RuntimeExecutionState.UNKNOWN
            self._settings = RuntimeSettings()
            self._settings_available = False
            self._touch()
            return True
        if name in {"before_agent_start", "agent_start", "session_before_compact", "agent_end"}:
            # ``agent_end`` is intentionally still active: retries,
            # compaction/retry, and queued continuations have not reached the
            # public outer-settled boundary yet.
            self._execution_state = RuntimeExecutionState.ACTIVE
            self._touch()
            return True
        if name == "agent_settled":
            # ``agent_settled`` is Pi's outer lifecycle boundary, but retain
            # the extension's live ``ctx.isIdle()`` read: an unexpected false
            # read is unknown, never permission to manufacture idle.
            reported_state = event.get("execution_state")
            self._execution_state = (
                _decode_execution_state(reported_state)
                if reported_state is not None
                else RuntimeExecutionState.UNKNOWN
            )
            self._touch()
            return True
        reported_state = event.get("execution_state")
        if reported_state is not None:
            self._execution_state = _decode_execution_state(reported_state)
            self._touch()
        return True

    def _reset_for_peer_reconnect(self) -> None:
        """Forget peer-local ordering only after an explicit host replacement."""
        self._bridge_epoch = None
        self._snapshot_revision = None
        self._native_session_id = None
        self._session_epoch += 1
        self._last_sequence = -1
        self._settings = RuntimeSettings()
        self._execution_state = RuntimeExecutionState.UNKNOWN
        self._settings_available = False
        self._health = ConnectionHealth.UNOPENED
        self._touch()

    def _runtime_snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            participant_id=self._participant_id,
            backend_generation=self._backend_generation,
            native_session_id=self._native_session_id,
            settings=self._settings,
            capabilities=self._capabilities(),
            health=self._health,
            health_diagnostics=tuple(self._diagnostics),
            execution_state=self._execution_state,
        )

    def _capabilities(self) -> RuntimeCapabilities:
        available: set[RuntimeCapability] = set()
        if (
            self._health is ConnectionHealth.CONNECTED
            and self._native_session_id is not None
            and self._settings_available
        ):
            available.add(RuntimeCapability.SETTINGS_UPDATE)
        unavailable = {
            RuntimeCapability.SEND: CapabilityUnavailableReason.GATED_BY_BACKEND,
            RuntimeCapability.STEER: CapabilityUnavailableReason.GATED_BY_BACKEND,
            RuntimeCapability.QUEUE_FOLLOWUP: CapabilityUnavailableReason.GATED_BY_BACKEND,
            RuntimeCapability.INTERRUPT: CapabilityUnavailableReason.GATED_BY_BACKEND,
        }
        if RuntimeCapability.SETTINGS_UPDATE not in available:
            unavailable[RuntimeCapability.SETTINGS_UPDATE] = (
                CapabilityUnavailableReason.GATED_BY_BACKEND
            )
        return RuntimeCapabilities(available=frozenset(available), unavailable_reasons=unavailable)

    def _proof_gated(self, operation_id: str, control: str) -> ControlReceipt:
        return self._rejected(
            operation_id,
            "native_control_proof_gated",
            f"Pi native {control} remains disabled pending public lifecycle conformance proof",
        )

    @staticmethod
    def _rejected(operation_id: str, code: str, message: str) -> ControlReceipt:
        return ControlReceipt(
            operation_id=operation_id,
            result=DeliveryResult.REJECTED,
            error_code=code,
            error=message,
        )

    @staticmethod
    def _unknown(operation_id: str, code: str, message: str) -> ControlReceipt:
        return ControlReceipt(
            operation_id=operation_id,
            result=DeliveryResult.UNKNOWN,
            error_code=code,
            error=message,
        )

    def _mark_disconnected(self, diagnostic: str) -> None:
        self._diagnostic(diagnostic)
        self._health = ConnectionHealth.DISCONNECTED
        self._execution_state = RuntimeExecutionState.UNKNOWN
        self._settings_available = False
        self._touch()

    def _diagnostic(self, value: str) -> None:
        self._diagnostics.append(value[:PI_FRONTEND_MAX_VALUE_CHARS])

    def _touch(self) -> None:
        self._revision += 1
        callback = self._activity_callback
        if callback is not None:
            with contextlib.suppress(Exception):
                callback()


class PiFrontendLiveSource(Source):
    """Status-only live enrichment; it never duplicates durable JSONL evidence."""

    def __init__(self, runtime: PiFrontendRuntime) -> None:
        self._runtime = runtime
        self._last_revision = -1
        self._last_status: Status | None = None

    def set_activity_callback(self, callback: Callable[[], None] | None) -> None:
        self._runtime.set_activity_callback(callback)

    async def read(self) -> Batch:
        status = self._status()
        revision = self._runtime._revision
        progressed = revision != self._last_revision or status != self._last_status
        self._last_revision = revision
        self._last_status = status
        return Batch(progressed=progressed, status=status)

    def health_snapshot(self) -> tuple[ChannelHealth, ...]:
        health = self._runtime._health
        if health is ConnectionHealth.CONNECTED:
            state = ChannelHealthState.HEALTHY
        elif health is ConnectionHealth.DEGRADED:
            state = ChannelHealthState.DEGRADED
        elif health is ConnectionHealth.DISCONNECTED:
            state = ChannelHealthState.FAILED
        else:
            state = ChannelHealthState.STARTING
        return (
            ChannelHealth(
                channel_id=PI_FRONTEND_CHANNEL_ID,
                state=state,
                diagnostics=tuple(self._runtime._diagnostics),
                accepted=self._runtime._accepted,
                dropped=self._runtime._dropped,
            ),
        )

    def _status(self) -> Status | None:
        runtime = self._runtime
        if runtime._health is not ConnectionHealth.CONNECTED:
            return None
        if runtime._execution_state is RuntimeExecutionState.IDLE:
            return Status.IDLE
        if runtime._execution_state is RuntimeExecutionState.ACTIVE:
            return Status.WORKING
        return None


__all__ = [
    "PI_FRONTEND_CHANNEL_ID",
    "PI_FRONTEND_CONTROL_TIMEOUT_SECONDS",
    "PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY",
    "PiFrontendLiveSource",
    "PiFrontendPeer",
    "PiFrontendProtocolError",
    "PiFrontendRuntime",
    "parse_pi_version",
    "probe_pi_frontend_compatibility",
]
