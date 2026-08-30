"""Focused participant interruption RPC tests."""

from __future__ import annotations

import pytest

from theater.constants.daemon import BUS_KIND_PARTICIPANT_INTERRUPT_REQUESTED
from theater.models import Status
from theater.protocol import RemoteError


async def _working_child(daemon, fake_tmux):
    parent = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent.id)
    fake_tmux.add_pane("%1", command="vibe", pid=4242)
    daemon.registry.attach_pane(child.id, "%1", pane_pid=4242)
    daemon.registry.set_status(child.id, Status.WORKING)
    return parent, daemon.registry.get(child.id)


def _interrupt_events(daemon):
    return [
        event
        for event in daemon.store.bus_tail(limit=100)
        if event["kind"] == BUS_KIND_PARTICIPANT_INTERRUPT_REQUESTED
    ]


async def test_parent_interrupts_a_working_child_by_live_name(
    client, daemon, fake_tmux, monkeypatch
):
    from theater.tmux import client as tmux

    parent, child = await _working_child(daemon, fake_tmux)
    delivered = []

    async def deliver_keys(pane, keys, *, inter_key_delay_seconds=None):
        delivered.append((pane, keys, inter_key_delay_seconds))

    monkeypatch.setattr(tmux, "deliver_keys", deliver_keys)

    result = await client.call("participant.interrupt", target=child.name, caller_id=parent.id)

    assert result == {"id": child.id, "interrupted": True}
    assert delivered == [("%1", ("Escape",), None)]
    assert [
        (event["kind"], event["from_id"], event["to_id"], event["payload"])
        for event in _interrupt_events(daemon)
    ] == [(BUS_KIND_PARTICIPANT_INTERRUPT_REQUESTED, parent.id, child.id, None)]
    assert daemon.store.running_jobs_for_target(child.id) == []


async def test_interrupt_returns_without_injection_when_child_is_not_working(
    client, daemon, fake_tmux, monkeypatch
):
    from theater.tmux import client as tmux

    parent, child = await _working_child(daemon, fake_tmux)
    daemon.registry.set_status(child.id, Status.IDLE)

    async def unexpected_delivery(*args, **kwargs):
        raise AssertionError("an idle child must not receive interrupt keys")

    monkeypatch.setattr(tmux, "deliver_keys", unexpected_delivery)

    result = await client.call("participant.interrupt", target=child.id, caller_id=parent.id)

    assert result == {"id": child.id, "interrupted": False, "reason": "already_not_working"}
    assert _interrupt_events(daemon) == []


async def test_interrupt_refuses_self_and_non_child_callers(client, daemon, fake_tmux):
    _parent, child = await _working_child(daemon, fake_tmux)
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")

    for caller_id in (child.id, stranger.id):
        with pytest.raises(RemoteError) as raised:
            await client.call("participant.interrupt", target=child.id, caller_id=caller_id)
        assert raised.value.code == "not_your_child"

    assert _interrupt_events(daemon) == []


async def test_interrupt_reuses_the_pane_and_human_presence_gates(
    client, daemon, fake_tmux, monkeypatch
):
    from theater.daemon.rpc import sending
    from theater.tmux import client as tmux

    parent, child = await _working_child(daemon, fake_tmux)
    fake_tmux.remove_pane("%1")

    async def unexpected_delivery(*args, **kwargs):
        raise AssertionError("a stale pane must not receive interrupt keys")

    monkeypatch.setattr(tmux, "deliver_keys", unexpected_delivery)
    with pytest.raises(RemoteError) as stale:
        await client.call("participant.interrupt", target=child.id, caller_id=parent.id)
    assert stale.value.code == "stale_target"
    assert daemon.registry.get(child.id).status is Status.DEAD

    parent, child = await _working_child(daemon, fake_tmux)

    async def human_present(pane):
        return True

    monkeypatch.setattr(sending, "human_present", human_present)
    with pytest.raises(RemoteError) as occupied:
        await client.call("participant.interrupt", target=child.id, caller_id=parent.id)
    assert occupied.value.code == "human_present"
    assert _interrupt_events(daemon) == []


async def test_interrupt_requires_a_manifest_control_and_records_only_delivery_success(
    client, daemon, fake_tmux, monkeypatch
):
    from theater.harness import HARNESSES
    from theater.harness.contracts.manifest import ControlManifest
    from theater.tmux import client as tmux

    parent, child = await _working_child(daemon, fake_tmux)
    original_controls = HARNESSES["vibe"].controls
    monkeypatch.setattr(HARNESSES["vibe"], "controls", ControlManifest())
    with pytest.raises(RemoteError) as unsupported:
        await client.call("participant.interrupt", target=child.id, caller_id=parent.id)
    assert unsupported.value.code == "bad_request"
    assert "does not declare an interrupt control" in str(unsupported.value)

    monkeypatch.setattr(HARNESSES["vibe"], "controls", original_controls)
    parent, child = await _working_child(daemon, fake_tmux)

    async def failed_delivery(*args, **kwargs):
        raise RuntimeError("tmux delivery failed")

    monkeypatch.setattr(tmux, "deliver_keys", failed_delivery)
    with pytest.raises(RemoteError):
        await client.call("participant.interrupt", target=child.id, caller_id=parent.id)
    assert _interrupt_events(daemon) == []
