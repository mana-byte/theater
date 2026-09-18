"""Exact native-session identity persistence shared by runtime hosts."""

from theater.harness.contracts.runtime import RuntimeBinding
from theater.models import TheaterError, now
from theater.provenance import TranscriptProvenance


def bind_runtime_identity(
    store,
    participant_id: str,
    binding: RuntimeBinding,
    generation: int,
) -> None:
    """Persist one generation's non-null native identity before activation."""
    if binding.native_session_id is None:
        raise TheaterError(
            f"the native runtime for {participant_id!r} reported no native "
            "session id; refusing to bind an unnamed session"
        )
    updated = store.bind_runtime_identity(
        participant_id,
        backend_generation=generation,
        native_session_id=binding.native_session_id,
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
    current = store.get_participant(participant_id)
    if current is None:
        raise TheaterError(f"participant {participant_id!r} vanished during its native launch")
    current.session_id = binding.native_session_id
    current.session_correlation = str(TranscriptProvenance.EXACT)
    store.upsert_participant(current)


__all__ = ["bind_runtime_identity"]
