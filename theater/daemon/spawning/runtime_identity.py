"""Exact native-session identity validation and persistence for runtime hosts."""

from theater.harness.contracts.runtime import (
    RuntimeBinding,
    RuntimeLifecyclePhase,
    RuntimeSnapshot,
    RuntimeWiring,
)
from theater.models import TheaterError, now
from theater.provenance import TranscriptProvenance


def validate_runtime_binding(
    store,
    participant_id: str,
    binding: RuntimeBinding,
    generation: int,
    *,
    require_native_session: bool = True,
) -> None:
    """Reject a runtime's returned binding unless it names the exact durable route."""
    durable = store.get_runtime_binding(participant_id)
    if durable is None:
        raise TheaterError(f"participant {participant_id!r} has no durable runtime binding")
    if binding.participant_id != participant_id:
        raise TheaterError(
            f"native runtime for {participant_id!r} returned participant "
            f"{binding.participant_id!r}; refusing a cross-participant binding"
        )
    if binding.backend_generation != generation or durable.backend_generation != generation:
        raise TheaterError(
            f"native runtime binding generation for {participant_id!r} changed during "
            "session open; refusing a stale or foreign generation"
        )
    if binding.wiring is not RuntimeWiring.NATIVE or durable.wiring is not RuntimeWiring.NATIVE:
        raise TheaterError(
            f"runtime for {participant_id!r} returned a non-native binding on a native route"
        )
    if binding.lifecycle not in {
        RuntimeLifecyclePhase.BOUND,
        RuntimeLifecyclePhase.ATTACHED,
        RuntimeLifecyclePhase.ACTIVE,
    }:
        raise TheaterError(
            f"runtime for {participant_id!r} returned lifecycle {binding.lifecycle!s} "
            "without an open session"
        )
    if binding.endpoint != durable.endpoint:
        raise TheaterError(
            f"runtime for {participant_id!r} returned endpoint {binding.endpoint!r}, "
            f"not its durable endpoint {durable.endpoint!r}"
        )
    if require_native_session and binding.native_session_id is None:
        raise TheaterError(
            f"the native runtime for {participant_id!r} reported no native "
            "session id; refusing to bind an unnamed session"
        )
    if (
        durable.native_session_id is not None
        and binding.native_session_id is not None
        and binding.native_session_id != durable.native_session_id
    ):
        raise TheaterError(
            f"runtime for {participant_id!r} returned native session "
            f"{binding.native_session_id!r}, not the durable session "
            f"{durable.native_session_id!r}"
        )


def validate_runtime_snapshot(
    participant_id: str,
    snapshot: RuntimeSnapshot,
    generation: int,
    native_session_id: str,
) -> None:
    """Require capability facts to belong to the just-opened exact session."""
    if (
        snapshot.participant_id != participant_id
        or snapshot.backend_generation != generation
        or snapshot.native_session_id != native_session_id
    ):
        raise TheaterError(
            f"runtime snapshot for {participant_id!r} did not match its exact "
            "participant, generation, and native session"
        )


def bind_runtime_identity(
    store,
    participant_id: str,
    binding: RuntimeBinding,
    generation: int,
) -> None:
    """Persist one generation's non-null native identity before activation."""
    validate_runtime_binding(store, participant_id, binding, generation)
    assert binding.native_session_id is not None
    updated = store.bind_runtime_and_participant_identity(
        participant_id,
        backend_generation=generation,
        native_session_id=binding.native_session_id,
        session_correlation=str(TranscriptProvenance.EXACT),
        protocol=binding.protocol,
        protocol_version=binding.protocol_version,
        native_version=binding.native_version,
        compatibility_policy=binding.compatibility_policy,
        updated_at=now(),
    )
    if not updated:
        raise TheaterError(
            f"runtime binding generation for {participant_id!r} changed during "
            "launch; failing closed instead of binding another generation's identity"
        )


__all__ = ["bind_runtime_identity", "validate_runtime_binding", "validate_runtime_snapshot"]
