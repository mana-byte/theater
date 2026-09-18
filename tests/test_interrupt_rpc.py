"""Focused participant interruption RPC tests."""

from __future__ import annotations

import pytest

from tests._presence_doubles import AbsentPresence
from theater.constants.daemon import BUS_KIND_PARTICIPANT_INTERRUPT_REQUESTED
from theater.models import Status
from theater.protocol import RemoteError


@pytest.fixture(autouse=True)
async def _absent_presence(daemon):
    """No human focus: the composed gates pass for every test in this module."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon, "presence", AbsentPresence(), raising=False)
        yield


async def _working_child(daemon, terminal_provider):
    parent = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent.id)
    terminal_provider.bind(daemon, child.id)
    daemon.registry.set_status(child.id, Status.WORKING)
    return parent, daemon.registry.get(child.id)


def _interrupt_events(daemon):
    return [
        event
        for event in daemon.store.bus_tail(limit=100)
        if event["kind"] == BUS_KIND_PARTICIPANT_INTERRUPT_REQUESTED
    ]


async def test_interrupt_refuses_self_and_non_child_callers(client, daemon, terminal_provider):
    _parent, child = await _working_child(daemon, terminal_provider)
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")

    for caller_id in (child.id, stranger.id):
        with pytest.raises(RemoteError) as raised:
            await client.call("participant.interrupt", target=child.id, caller_id=caller_id)
        assert raised.value.code == "not_your_child"

    assert _interrupt_events(daemon) == []


# ---- native routing (Wave 4A) ---------------------------------------------
#
# A participant with a live runtime is interrupted through the control
# service: queued followups are durably cancelled first, then the exact
# active native turn. The pane path above is the unchanged behaviour for
# participants without one.


async def _native_working_child(daemon, terminal_provider):
    from tests.rig.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
    from theater.harness.contracts.runtime import RuntimeContext

    parent = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent.id)
    terminal_provider.bind(daemon, child.id)

    state = FakeRuntimeState(participant_id=child.id, backend_generation=1)
    state.native_session_id = "thread-1"
    context = RuntimeContext(
        participant_id=child.id, cwd="/tmp", io=FakeRuntimeIO(state), backend_generation=1
    )

    async def create():
        return FakeRuntime(context)

    await daemon.runtime_manager.get_or_create(child.id, backend_generation=1, create=create)
    return parent, daemon.registry.get(child.id), state


async def test_native_interrupt_cancels_queued_followups_and_requests_the_turn(
    client, daemon, terminal_provider
):
    parent, child, state = await _native_working_child(daemon, terminal_provider)
    active = await client.call("send", target=child.id, prompt="first", caller_id=parent.id)
    queued = await client.call(
        "participant.queue_followup", target=child.id, prompt="later", caller_id=parent.id
    )
    active_turn = state.native_turn_id
    assert active_turn is not None

    result = await client.call("participant.interrupt", target=child.id, caller_id=parent.id)

    assert result["id"] == child.id
    assert result["interrupted"] is True
    assert result["cancelled_followups"] == [queued["handle"]]
    assert state.interrupted == [active_turn]
    assert daemon.store.get_job(queued["handle"]).state == "killed"
    assert daemon.store.get_job(active["handle"]).state == "running", (
        "the active job finishes only from terminal evidence"
    )


async def test_native_interrupt_while_idle_still_clears_the_queue(
    client, daemon, terminal_provider
):
    parent, child, state = await _native_working_child(daemon, terminal_provider)
    await client.call("send", target=child.id, prompt="first", caller_id=parent.id)
    queued = await client.call(
        "participant.queue_followup", target=child.id, prompt="later", caller_id=parent.id
    )
    # The turn ends without Theater involvement (the human let it finish in
    # the native UI); the queued followup has not dispatched yet.
    state.native_turn_id = None

    result = await client.call("participant.interrupt", target=child.id, caller_id=parent.id)

    assert result == {
        "id": child.id,
        "interrupted": False,
        "reason": "already_idle",
        "cancelled_followups": [queued["handle"]],
    }
    assert daemon.store.get_job(queued["handle"]).state == "killed"


async def test_native_interrupt_authorizes_like_the_existing_gate(
    client, daemon, terminal_provider
):
    _parent, child, _state = await _native_working_child(daemon, terminal_provider)
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")

    for caller_id in (stranger.id, child.id):
        with pytest.raises(RemoteError) as raised:
            await client.call("participant.interrupt", target=child.id, caller_id=caller_id)
        assert raised.value.code == "not_your_child"
    assert _interrupt_events(daemon) == []
