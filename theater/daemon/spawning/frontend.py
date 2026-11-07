"""Passive frontend runtime lifecycle and the frontend launch sequence."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import Any

from theater import timing
from theater.daemon.controls.dispatch import TEMPORARY_REFUSALS
from theater.daemon.observation.live import LiveRegistration
from theater.daemon.runtime.launch_policy import decode_launch_policy
from theater.daemon.spawning.models import ProviderLaunchOutcome, Reservation
from theater.daemon.spawning.runtime_identity import (
    bind_runtime_identity,
    validate_runtime_binding,
    validate_runtime_snapshot,
)
from theater.harness import get as get_harness
from theater.harness.contracts.channels import ChannelKind
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    RuntimeBinding,
    RuntimeContext,
    RuntimeHost,
    RuntimeLifecyclePhase,
    SessionOpenMode,
)
from theater.models import BadRequest, Participant, Status, TheaterError, now
from theater.observability.catalog import LIFECYCLE_STAGE
from theater.provenance import is_trusted_provenance
from theater.transcript_identity import TRANSCRIPT_IDENTITY_LOST_CODE

logger = logging.getLogger("theater.spawner")

#: Bounded wait for the plugin's native readiness after the promptless UI starts.
FRONTEND_READY_TIMEOUT_SECONDS = 15.0

#: Bounded deadline for a launch-time fallback to deliver the spawn prompt through the
#: provider route once the terminal shows a ready composer.
FALLBACK_PROMPT_DEADLINE_SECONDS = 30.0

#: Poll interval between temporary provider refusals while the composer becomes ready.
FALLBACK_PROMPT_POLL_SECONDS = 0.5


def completion_owning_frontend(runtime) -> bool:
    """A frontend-host runtime whose live channel owns exact job completion."""
    return runtime.host is RuntimeHost.FRONTEND and runtime.channel.drives_job_completion


async def send_spawn_prompt(spawner, reservation: Reservation, participant_id: str) -> None:
    """Submit the spawn prompt exactly once through the control service, reusing the spawn job.

    ``job_handle`` binds the spawn RPC's job (its handle is the participant id) to the
    terminal evidence the runtime reports, so no second send job exists.
    """
    req = reservation.req
    if not req.prompt:
        return
    if spawner.controls is None:
        raise BadRequest(
            f"native wiring for {participant_id!r} requires the daemon control service; "
            "this spawner was composed without it"
        )
    await spawner.controls.send(
        participant_id,
        caller_id=req.parent_id or "cli",
        prompt=req.prompt,
        response_format=req.response_format,
        job_handle=participant_id,
    )


@dataclass(frozen=True, slots=True)
class _FrontendServices:
    host: Any
    runtime_manager: Any
    runtime_io: Any
    live_hub: Any
    store: Any
    #: Evidence hooks computed once at listener start; set only for a completion-owning channel.
    evidence_sink: Any = None
    active_job_for_turn: Any = None


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
            # A reconnect may only retain evidence for a departed native scope.
            if snapshot.health is ConnectionHealth.CONNECTED:
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
                    evidence_sink=self.services.evidence_sink,
                    active_job_for_turn=self.services.active_job_for_turn,
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
            if (
                registration is not None
                and registration.live_source is source
                and not completion_owning_frontend(self.config.runtime)
            ):
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
    controls=None,
) -> None:
    """Start a listener; a stock UI connection creates the live runtime."""
    if runtime.host is not RuntimeHost.FRONTEND:
        raise BadRequest("frontend listener requires a frontend runtime manifest")
    if endpoint is None:
        raise BadRequest("frontend listener requires the daemon-selected endpoint")
    owns_completion = completion_owning_frontend(runtime)
    if owns_completion and controls is None:
        raise BadRequest(
            "a completion-owning frontend listener requires the control service as its "
            "evidence sink; its jobs could never complete without it"
        )
    evidence_sink, active_job_for_turn = (
        (controls.record_terminal_evidence, controls.active_job_for_native_turn)
        if owns_completion
        else (None, None)
    )
    services = _FrontendServices(
        host,
        runtime_manager,
        runtime_io,
        live_hub,
        store,
        evidence_sink=evidence_sink,
        active_job_for_turn=active_job_for_turn,
    )
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


async def launch_frontend(spawner, reservation: Reservation) -> Participant:
    """Launch the promptless stock UI, authenticate native readiness, then submit the prompt.

    Startup-only fallback: before native activation or possible delivery the ordinary
    provider route may take over; an established route never replays into the pane.
    """
    native = reservation.native
    assert native is not None
    if (
        spawner.frontend_runtime_host is None
        or spawner.runtime_manager is None
        or spawner.runtime_io is None
    ):
        raise BadRequest("frontend wiring requires the daemon frontend runtime host")
    credential = spawner.registry.store.get_channel_credential(
        reservation.participant.id,
        ChannelKind.LIVE,
        native.runtime.channel.channel.id,
    )
    if credential is None:
        raise BadRequest("frontend wiring requires its launch channel credential")
    try:
        await start_frontend_listener(
            host=spawner.frontend_runtime_host,
            runtime_manager=spawner.runtime_manager,
            runtime_io=spawner.runtime_io,
            live_hub=spawner.live_hub,
            controls=spawner.controls,
            store=spawner.registry.store,
            participant=reservation.participant,
            runtime=native.runtime,
            generation=native.backend_generation,
            endpoint=native.endpoint,
            approval=reservation.req.approval,
            model=reservation.req.model,
            reasoning_effort=reservation.req.reasoning_effort,
            token=credential.token,
            operation_id=(
                reservation.provider.operation_id if reservation.provider is not None else None
            ),
        )
    except Exception:
        return await _fallback_frontend(spawner, reservation)
    try:
        attached = await spawner._launch_terminal(reservation)
    except ProviderLaunchOutcome as exc:
        if exc.outcome.state != "uncertain":
            await spawner._close_frontend_launch(reservation.participant.id)
        raise
    except BaseException:
        await spawner._close_frontend_launch(reservation.participant.id)
        raise
    if completion_owning_frontend(native.runtime):
        try:
            await _ready_frontend(spawner, attached.id)
        except Exception:
            return await _fallback_frontend(spawner, reservation, attached=attached)
        # Beyond this point native submission may have happened: never retry through the pane.
        await send_spawn_prompt(spawner, reservation, attached.id)
    phase = (
        RuntimeLifecyclePhase.ACTIVE if reservation.req.prompt else RuntimeLifecyclePhase.ATTACHED
    )
    if not spawner.registry.store.set_runtime_lifecycle(
        reservation.participant.id,
        phase,
        backend_generation=native.backend_generation,
        updated_at=now(),
    ):
        await spawner._close_frontend_launch(reservation.participant.id)
        raise TheaterError("frontend runtime binding changed during terminal launch")
    return attached


async def _ready_frontend(spawner, participant_id: str) -> None:
    """Wait for the plugin's bounded native readiness and a usable native scope."""
    await spawner.frontend_runtime_host.wait_ready(
        participant_id, timeout=FRONTEND_READY_TIMEOUT_SECONDS
    )
    instance = spawner.runtime_manager.get(participant_id)
    if instance is None:
        raise TheaterError("frontend runtime disappeared before initial dispatch")
    snapshot = await instance.snapshot()
    if snapshot.health is not ConnectionHealth.CONNECTED:
        raise TheaterError("frontend runtime did not establish a usable native scope")


async def _fallback_frontend(
    spawner, reservation: Reservation, *, attached: Participant | None = None
) -> Participant:
    native = reservation.native
    assert native is not None
    owns_completion = completion_owning_frontend(native.runtime)
    participant_id = reservation.participant.id
    store = spawner.registry.store
    # Fallback is startup-only, before any native operation can own a job.
    binding = store.get_runtime_binding(participant_id)
    current = store.get_participant(participant_id)
    if (
        binding is None
        or binding.backend_generation != native.backend_generation
        or current is None
        or current.session_id != reservation.participant.session_id
        or (
            owns_completion
            and spawner.live_hub is not None
            and spawner.live_hub.registration_for(participant_id) is not None
        )
        or any(
            store.control_operations_for_job(job.handle)
            for job in store.active_running_jobs_for_target(participant_id)
        )
    ):
        raise TheaterError("frontend startup changed scope or accepted work; refusing fallback")
    if reservation.legacy_plan is None:
        raise TheaterError("frontend startup has no validated legacy fallback plan")
    logger.warning("frontend startup for %s unavailable; using provider controls", participant_id)
    await spawner._close_frontend_launch(participant_id)
    store.delete_runtime_binding(participant_id)
    if attached is None:
        attached = await spawner._launch_terminal(
            replace(reservation, plan=reservation.legacy_plan, native=None, legacy_plan=None)
        )
    if owns_completion and reservation.req.prompt:
        await _deliver_fallback_prompt(spawner, reservation, attached.id)
    return attached


async def _deliver_fallback_prompt(spawner, reservation: Reservation, participant_id: str) -> None:
    """Deliver the spawn prompt on the provider route once its composer is ready.

    The provider route refuses with Busy/AwaitingDecision until the terminal shows a ready
    composer, and a refused attempt leaves nothing durable, so temporary refusals are
    retried until the bounded deadline. A possibly accepted native send is never replayed
    into the pane.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + FALLBACK_PROMPT_DEADLINE_SECONDS
    while True:
        try:
            await send_spawn_prompt(spawner, reservation, participant_id)
        except TEMPORARY_REFUSALS as exc:
            if loop.time() >= deadline:
                raise
            logger.debug(
                "fallback prompt for %s deferred (%s); retrying until the composer is ready",
                participant_id,
                type(exc).__name__,
            )
            await asyncio.sleep(FALLBACK_PROMPT_POLL_SECONDS)
        else:
            return


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
    policy = decode_launch_policy(binding.launch_policy).fields
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
        controls=daemon.controls,
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
    policy = decode_launch_policy(binding.launch_policy)
    if policy.runtime_host_present:
        return policy.fields["runtime_host"] == RuntimeHost.FRONTEND.value
    try:
        harness = get_harness(binding.harness)
    except Exception:
        return False
    runtime = getattr(harness, "runtime", None)
    return runtime is not None and runtime.host is RuntimeHost.FRONTEND


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
    "completion_owning_frontend",
    "is_frontend_binding",
    "launch_frontend",
    "restore_frontend_listener",
    "send_spawn_prompt",
    "start_frontend_listener",
]
