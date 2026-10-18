"""Native-runtime and public-view helpers for provider control tests."""

from __future__ import annotations

from tests.support.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
from theater.daemon.persistence.repositories.runtime_bindings import ParticipantRuntimeBinding
from theater.frontend.capabilities import METHOD_CATALOG
from theater.frontend.schemas import validator_for
from theater.harness.contracts.runtime import (
    RuntimeContext,
    RuntimeLifecyclePhase,
    RuntimeWiring,
)
from theater.models import now


async def install_native_runtime(
    daemon, participant_id: str, *, generation: int, session_id: str
) -> tuple[FakeRuntime, FakeRuntimeState]:
    state = FakeRuntimeState(
        participant_id=participant_id,
        backend_generation=generation,
        native_session_id=session_id,
    )
    runtime = FakeRuntime(
        RuntimeContext(
            participant_id=participant_id,
            cwd=None,
            io=FakeRuntimeIO(state),
            backend_generation=generation,
        )
    )

    async def create() -> FakeRuntime:
        return runtime

    installed = await daemon.runtime_manager.get_or_create(
        participant_id, backend_generation=generation, create=create
    )
    assert installed is runtime
    return runtime, state


def assert_result_valid(method: str, value: object) -> None:
    validator_for(METHOD_CATALOG[method].result_schema_id).validate(value)


_SAME = object()


async def bind_native_participant(
    daemon, *, generation: int, session_id: str, bound_session_id: object = _SAME
):
    """Register a codex participant bound to a native runtime and install its fake runtime.

    `session_id` is the live runtime's session; `bound_session_id` (default: the same) is
    the durable binding's, so a test can make the two disagree.
    """
    participant = daemon.registry.register(harness="codex", pane=None, cwd=None)
    daemon.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=participant.id,
            harness="codex",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=generation,
            lifecycle=RuntimeLifecyclePhase.ACTIVE,
            native_session_id=session_id if bound_session_id is _SAME else bound_session_id,
            created_at=now(),
            updated_at=now(),
        )
    )
    runtime, state = await install_native_runtime(
        daemon, participant.id, generation=generation, session_id=session_id
    )
    return participant, runtime, state
