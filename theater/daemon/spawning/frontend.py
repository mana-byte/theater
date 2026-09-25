"""Passive frontend runtime lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from theater import timing
from theater.daemon.observation.live import LiveRegistration
from theater.daemon.spawning.runtime_identity import (
    bind_runtime_identity,
    validate_runtime_binding,
    validate_runtime_snapshot,
)
from theater.harness import get as get_harness
from theater.harness.contracts.channels import ChannelKind
from theater.harness.contracts.runtime import (
    RuntimeBinding,
    RuntimeContext,
    RuntimeHost,
    RuntimeLifecyclePhase,
    SessionOpenMode,
)
from theater.models import BadRequest, Participant, Status, now
from theater.observability.catalog import LIFECYCLE_STAGE
from theater.provenance import is_trusted_provenance
from theater.transcript_identity import TRANSCRIPT_IDENTITY_LOST_CODE


@dataclass(frozen=True, slots=True)
class _FrontendServices:
    host: Any
    runtime_manager: Any
    runtime_io: Any
    live_hub: Any
    store: Any


@dataclass(frozen=True, slots=True)
class _FrontendConfig:
    participant: Participant
    runtime: Any
    generation: int
    endpoint: str
    approval: str | None
    model: str | None
    reasoning_effort: str | None
    token: str
    operation_id: str | None


@dataclass(slots=True)
class _FrontendListener:
    services: _FrontendServices
    config: _FrontendConfig
    active: dict[int, tuple[object, object]] = field(default_factory=dict)
    connected_once: bool = False
    listening_since: float = field(default_factory=perf_counter)

    async def on_connect(self, connection) -> None:
        binding = self.services.store.get_runtime_binding(self.config.participant.id)
        if binding is None or binding.backend_generation != self.config.generation:
            raise BadRequest("frontend runtime binding changed before connection")
        instance, opened_binding = await self._reconnect(connection, binding)
        source = None
        try:
            if opened_binding is None:
                raise BadRequest(  # noqa: TRY301 — activation cleanup
                    "frontend runtime did not return a session binding"
                )
            current_binding = await self._cache_session(instance, opened_binding)
            source = instance.live_source()
            self._register_live_source(source, opened_binding)
            self._activate_runtime(current_binding)
            self.active.clear()
            self.active[id(connection)] = (instance, source)
            self._record_first_connection()
        except BaseException:
            await self.discard(instance, source)
            raise

    async def _reconnect(self, connection, binding) -> tuple[Any, RuntimeBinding | None]:
        opened_binding: RuntimeBinding | None = None

        async def create():
            nonlocal opened_binding
            instance = self.config.runtime.factory(
                RuntimeContext(
                    participant_id=self.config.participant.id,
                    cwd=self.config.participant.cwd,
                    io=self.services.runtime_io,
                    backend_generation=self.config.generation,
                    endpoint=self.config.endpoint,
                    approval=self.config.approval,
                    model=self.config.model,
                    reasoning_effort=self.config.reasoning_effort,
                    native_session_id=binding.native_session_id,
                    frontend=connection,
                    trusted_session_id_provider=lambda: _trusted_session_id(
                        self.services.store, self.config.participant.id
                    ),
                )
            )
            try:
                opened_binding = await instance.open_session(mode=SessionOpenMode.RECONNECT)
            except BaseException:
                await instance.aclose()
                raise
            return instance

        instance = await self.services.runtime_manager.reconnect(
            self.config.participant.id,
            backend_generation=self.config.generation,
            create=create,
            monitor_recovery=False,
        )
        return instance, opened_binding

    async def _cache_session(self, instance, opened_binding: RuntimeBinding):
        participant_id = self.config.participant.id
        validate_runtime_binding(
            self.services.store,
            participant_id,
            opened_binding,
            self.config.generation,
            require_native_session=False,
        )
        if opened_binding.native_session_id is not None:
            snapshot = await instance.snapshot()
            validate_runtime_snapshot(
                participant_id,
                snapshot,
                self.config.generation,
                opened_binding.native_session_id,
            )
            bind_runtime_identity(
                self.services.store,
                participant_id,
                opened_binding,
                self.config.generation,
            )
            if not self.services.runtime_manager.record_snapshot(
                participant_id, instance, snapshot
            ):
                raise BadRequest("frontend runtime changed before its capabilities were cached")
        elif not self.services.runtime_manager.mark_session_open(
            participant_id, instance, opened_binding
        ):
            raise BadRequest("frontend runtime changed before its session was cached")
        current = self.services.store.get_runtime_binding(participant_id)
        if current is None or current.backend_generation != self.config.generation:
            raise BadRequest("frontend runtime binding changed during connection")
        return current

    def _register_live_source(self, source, opened_binding: RuntimeBinding) -> None:
        if self.services.live_hub is not None:
            self.services.live_hub.register(
                LiveRegistration(
                    participant_id=self.config.participant.id,
                    live_source=source,
                    channel=self.config.runtime.channel,
                    backend_generation=self.config.generation,
                    native_session_id=opened_binding.native_session_id,
                    evidence_sink=None,
                    active_job_for_turn=None,
                )
            )

    def _activate_runtime(self, current_binding: RuntimeBinding) -> None:
        phase = (
            RuntimeLifecyclePhase.ACTIVE
            if current_binding.lifecycle is RuntimeLifecyclePhase.ACTIVE
            else RuntimeLifecyclePhase.ATTACHED
        )
        if not self.services.store.set_runtime_lifecycle(
            self.config.participant.id,
            phase,
            backend_generation=self.config.generation,
            updated_at=now(),
        ):
            raise BadRequest("frontend runtime binding changed during activation")

    def _record_first_connection(self) -> None:
        if self.connected_once or self.config.operation_id is None:
            return
        self.connected_once = True
        timing.emit(
            LIFECYCLE_STAGE,
            (perf_counter() - self.listening_since) * 1000,
            action="spawn",
            stage="runtime_connected",
            id=self.config.participant.id,
            operation_id=self.config.operation_id,
        )

    async def discard(self, instance, source) -> None:
        if self.services.live_hub is not None:
            registration = self.services.live_hub.registration_for(self.config.participant.id)
            if registration is not None and registration.live_source is source:
                self.services.live_hub.unregister(self.config.participant.id)
        if self.services.runtime_manager.get(self.config.participant.id) is instance:
            await self.services.runtime_manager.close(self.config.participant.id)

    async def on_disconnect(self, connection) -> None:
        current = self.active.pop(id(connection), None)
        if current is not None:
            await self.discard(*current)


async def start_frontend_listener(
    *,
    host,
    runtime_manager,
    runtime_io,
    live_hub,
    store,
    participant: Participant,
    runtime,
    generation: int,
    endpoint: str | None,
    approval: str | None,
    model: str | None,
    reasoning_effort: str | None,
    token: str,
    operation_id: str | None = None,
) -> None:
    """Start a listener; a stock UI connection creates the live runtime."""
    if runtime.host is not RuntimeHost.FRONTEND:
        raise BadRequest("frontend listener requires a frontend runtime manifest")
    if endpoint is None:
        raise BadRequest("frontend listener requires the daemon-selected endpoint")
    services = _FrontendServices(host, runtime_manager, runtime_io, live_hub, store)
    config = _FrontendConfig(
        participant,
        runtime,
        generation,
        endpoint,
        approval,
        model,
        reasoning_effort,
        token,
        operation_id,
    )
    listener = _FrontendListener(services, config)
    await host.start(
        participant_id=participant.id,
        generation=generation,
        endpoint=endpoint,
        token=token,
        on_connect=listener.on_connect,
        on_disconnect=listener.on_disconnect,
    )


async def restore_frontend_listener(daemon, binding, participant: Participant) -> bool:
    """Restore a listener after daemon restart without starting another UI."""
    try:
        harness = get_harness(binding.harness)
    except Exception:
        return False
    runtime = getattr(harness, "runtime", None)
    if runtime is None or runtime.host is not RuntimeHost.FRONTEND or binding.endpoint is None:
        return False
    credential = daemon.store.get_channel_credential(
        participant.id,
        ChannelKind.LIVE,
        runtime.channel.channel.id,
    )
    if credential is None or credential.harness != binding.harness:
        return False
    policy = _launch_policy(binding.launch_policy)
    await start_frontend_listener(
        host=daemon.frontend_runtime_host,
        runtime_manager=daemon.runtime_manager,
        runtime_io=daemon.runtime_io,
        live_hub=getattr(daemon.observer, "live", None),
        store=daemon.store,
        participant=participant,
        runtime=runtime,
        generation=binding.backend_generation,
        endpoint=binding.endpoint,
        approval=_optional_text(policy, "approval"),
        model=_optional_text(policy, "model"),
        reasoning_effort=_optional_text(policy, "reasoning_effort"),
        token=credential.token,
    )
    return True


async def close_frontend_runtime(daemon, participant_id: str) -> None:
    """Close passive observation resources without touching the stock UI."""
    host = getattr(daemon, "frontend_runtime_host", None)
    if host is not None:
        await host.close(participant_id)
    await daemon.runtime_manager.close(participant_id)
    hub = getattr(daemon.observer, "live", None)
    if hub is not None:
        hub.unregister(participant_id)


def is_frontend_binding(binding) -> bool:
    policy = _launch_policy(binding.launch_policy)
    if "runtime_host" in policy:
        return policy["runtime_host"] == RuntimeHost.FRONTEND.value
    try:
        harness = get_harness(binding.harness)
    except Exception:
        return False
    runtime = getattr(harness, "runtime", None)
    return runtime is not None and runtime.host is RuntimeHost.FRONTEND


def _launch_policy(raw: str | None) -> dict[str, object]:
    import json

    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _optional_text(values: Mapping[str, object], key: str) -> str | None:
    value = values.get(key)
    return value if isinstance(value, str) and value else None


def _trusted_session_id(store, participant_id: str) -> str | None:
    participant = store.get_participant(participant_id)
    if (
        participant is None
        or participant.status is Status.DEAD
        or not isinstance(participant.session_id, str)
        or not participant.session_id.strip()
        or not is_trusted_provenance(participant.session_correlation)
        or store.observation_error_active(participant_id, TRANSCRIPT_IDENTITY_LOST_CODE)
    ):
        return None
    return participant.session_id


__all__ = [
    "close_frontend_runtime",
    "is_frontend_binding",
    "restore_frontend_listener",
    "start_frontend_listener",
]
