"""Passive frontend runtime lifecycle."""

from __future__ import annotations

from collections.abc import Mapping

from theater.daemon.observation.live import LiveRegistration
from theater.harness import get as get_harness
from theater.harness.contracts.channels import ChannelKind
from theater.harness.contracts.runtime import (
    RuntimeContext,
    RuntimeHost,
    RuntimeLifecyclePhase,
    SessionOpenMode,
)
from theater.models import BadRequest, Participant, Status, now
from theater.provenance import is_trusted_provenance
from theater.transcript_identity import TRANSCRIPT_IDENTITY_LOST_CODE


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
    endpoint: str,
    approval: str | None,
    model: str | None,
    reasoning_effort: str | None,
    token: str,
) -> None:
    """Start a listener; a stock UI connection creates the live runtime."""
    if runtime.host is not RuntimeHost.FRONTEND:
        raise BadRequest("frontend listener requires a frontend runtime manifest")
    active: dict[int, tuple[object, object]] = {}

    async def on_connect(connection) -> None:
        binding = store.get_runtime_binding(participant.id)
        if binding is None or binding.backend_generation != generation:
            raise BadRequest("frontend runtime binding changed before connection")

        async def create():
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
                await instance.open_session(mode=SessionOpenMode.RECONNECT)
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
                        native_session_id=None,
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
