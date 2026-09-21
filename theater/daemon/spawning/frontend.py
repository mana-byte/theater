"""Passive frontend runtime lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from time import perf_counter

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


async def start_frontend_listener(  # noqa: PLR0915
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
    active: dict[int, tuple[object, object]] = {}
    connected_once = False
    listening_since = perf_counter()

    async def on_connect(connection) -> None:
        nonlocal connected_once
        binding = store.get_runtime_binding(participant.id)
        if binding is None or binding.backend_generation != generation:
            raise BadRequest("frontend runtime binding changed before connection")

        opened_binding: RuntimeBinding | None = None

        async def create():
            nonlocal opened_binding
            instance = runtime.factory(
                RuntimeContext(
                    participant_id=participant.id,
                    cwd=participant.cwd,
                    io=runtime_io,
                    backend_generation=generation,
                    endpoint=endpoint,
                    approval=approval,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    native_session_id=binding.native_session_id,
                    frontend=connection,
                    trusted_session_id_provider=lambda: _trusted_session_id(store, participant.id),
                )
            )
            try:
                opened_binding = await instance.open_session(mode=SessionOpenMode.RECONNECT)
            except BaseException:
                await instance.aclose()
                raise
            return instance

        instance = await runtime_manager.reconnect(
            participant.id,
            backend_generation=generation,
            create=create,
            monitor_recovery=False,
        )
        source = None
        try:
            if opened_binding is None:
                raise BadRequest(  # noqa: TRY301 — activation cleanup
                    "frontend runtime did not return a session binding"
                )
            validate_runtime_binding(
                store,
                participant.id,
                opened_binding,
                generation,
                require_native_session=False,
            )
            if opened_binding.native_session_id is not None:
                snapshot = await instance.snapshot()
                validate_runtime_snapshot(
                    participant.id,
                    snapshot,
                    generation,
                    opened_binding.native_session_id,
                )
                bind_runtime_identity(store, participant.id, opened_binding, generation)
                if not runtime_manager.record_snapshot(participant.id, instance, snapshot):
                    raise BadRequest(  # noqa: TRY301 — activation cleanup
                        "frontend runtime changed before its capabilities were cached"
                    )
            elif not runtime_manager.mark_session_open(participant.id, instance, opened_binding):
                raise BadRequest(  # noqa: TRY301 — activation cleanup
                    "frontend runtime changed before its session was cached"
                )
            current_binding = store.get_runtime_binding(participant.id)
            if current_binding is None or current_binding.backend_generation != generation:
                raise BadRequest("frontend runtime binding changed during connection")  # noqa: TRY301 — activation cleanup
            source = instance.live_source()
            if live_hub is not None:
                live_hub.register(
                    LiveRegistration(
                        participant_id=participant.id,
                        live_source=source,
                        channel=runtime.channel,
                        backend_generation=generation,
                        native_session_id=opened_binding.native_session_id,
                        evidence_sink=None,
                        active_job_for_turn=None,
                    )
                )
            phase = (
                RuntimeLifecyclePhase.ACTIVE
                if current_binding.lifecycle is RuntimeLifecyclePhase.ACTIVE
                else RuntimeLifecyclePhase.ATTACHED
            )
            if not store.set_runtime_lifecycle(
                participant.id,
                phase,
                backend_generation=generation,
                updated_at=now(),
            ):
                raise BadRequest("frontend runtime binding changed during activation")  # noqa: TRY301 — activation cleanup
            active.clear()
            active[id(connection)] = (instance, source)
            if not connected_once and operation_id is not None:
                connected_once = True
                timing.emit(
                    LIFECYCLE_STAGE,
                    (perf_counter() - listening_since) * 1000,
                    action="spawn",
                    stage="runtime_connected",
                    id=participant.id,
                    operation_id=operation_id,
                )
        except BaseException:
            await discard(instance, source)
            raise

    async def discard(instance, source) -> None:
        if live_hub is not None:
            registration = live_hub.registration_for(participant.id)
            if registration is not None and registration.live_source is source:
                live_hub.unregister(participant.id)
        if runtime_manager.get(participant.id) is instance:
            await runtime_manager.close(participant.id)

    async def on_disconnect(connection) -> None:
        current = active.pop(id(connection), None)
        if current is not None:
            await discard(*current)

    await host.start(
        participant_id=participant.id,
        generation=generation,
        endpoint=endpoint,
        token=token,
        on_connect=on_connect,
        on_disconnect=on_disconnect,
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
