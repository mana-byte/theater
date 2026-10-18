"""A parent and a native-wired child under a daemon, with the fake runtime installed."""

from __future__ import annotations

from tests.support.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
from theater.constants.daemon import BUS_KIND_PARTICIPANT_INTERRUPT_REQUESTED
from theater.daemon.persistence.repositories.runtime_bindings import (
    ParticipantRuntimeBinding,
)
from theater.harness.contracts.runtime import (
    RuntimeContext,
    RuntimeLifecyclePhase,
    RuntimeWiring,
)

#: The exact response-format guidance prefix, injected exactly once.
GUIDANCE = "Return your final answer as a single bare JSON value"


def pair(daemon):
    """A parent and its unbound child (child has no runtime yet)."""
    parent = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent.id)
    return parent, daemon.registry.get(child.id)


async def install_runtime(daemon, pid: str, *, cls=FakeRuntime) -> FakeRuntimeState:
    state = FakeRuntimeState(participant_id=pid, backend_generation=1)
    state.native_session_id = "thread-1"
    context = RuntimeContext(
        participant_id=pid,
        cwd="/tmp",
        io=FakeRuntimeIO(state),
        backend_generation=1,
    )
    runtime = cls(context)

    async def create():
        return runtime

    installed = await daemon.runtime_manager.get_or_create(pid, backend_generation=1, create=create)
    assert installed is not None
    daemon.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=pid,
            harness="vibe",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=1,
            lifecycle=RuntimeLifecyclePhase.ACTIVE,
            native_session_id="thread-1",
        )
    )
    assert daemon.runtime_manager.record_snapshot(pid, installed, await installed.snapshot())
    return state


async def native_pair(daemon):
    parent, child = pair(daemon)
    state = await install_runtime(daemon, child.id)
    return parent, daemon.registry.get(child.id), state


async def active_send(client, parent, child) -> dict:
    """Ordinary native send through the existing ``send`` RPC."""
    job = await client.call("send", target=child.id, prompt="do the thing", caller_id=parent.id)
    assert isinstance(job, dict)
    return job


def interrupt_events(daemon):
    return [
        event
        for event in daemon.store.bus_tail(limit=100)
        if event["kind"] == BUS_KIND_PARTICIPANT_INTERRUPT_REQUESTED
    ]
