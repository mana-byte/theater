"""send(if_busy=...): one call redirects a busy child through queue, steer, or interrupt."""

from __future__ import annotations

import asyncio

import pytest

from tests._presence_doubles import AbsentPresence
from tests.test_control_rpc import _active_send, _native_pair, _pair
from theater.daemon.rpc import redirect
from theater.models import Status
from theater.protocol import RemoteError


@pytest.fixture(autouse=True)
async def _absent_presence(daemon):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon, "presence", AbsentPresence(), raising=False)
        yield


async def _send(client, child, caller, mode, **extra):
    return await client.call(
        "send", target=child.id, prompt="new plan", caller_id=caller.id, if_busy=mode, **extra
    )


async def test_idle_target_gets_a_plain_send_and_strangers_are_refused(client, daemon):
    parent, child, _state = await _native_pair(daemon)
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")

    with pytest.raises(RemoteError) as raised:
        await _send(client, child, stranger, "queue")
    assert raised.value.code == "not_your_child"

    sent = await _send(client, child, parent, "replace")
    assert sent["route"] == "send"
    assert "Theater:" not in sent["prompt"], "an idle target gets no interruption card"


async def test_queue_and_amend_route_a_busy_native_child(client, daemon):
    parent, child, state = await _native_pair(daemon)
    active = await _active_send(client, parent, child)

    with pytest.raises(RemoteError) as busy:
        await _send(client, child, parent, "refuse")
    assert busy.value.code == "busy" and "if_busy" in busy.value.details["next"]
    assert "omit if_busy to queue" in busy.value.details["next"]

    peer = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    with pytest.raises(RemoteError) as peer_busy:
        await _send(client, child, peer, "refuse")
    assert "omit if_busy to queue" in peer_busy.value.details["next"]
    assert 'if_busy="queue"' not in peer_busy.value.details["next"]

    queued = await _send(client, child, parent, "queue")
    assert queued["route"] == "queued" and queued["handle"] != active["handle"]

    amended = await _send(client, child, parent, "amend", job_handle=active["handle"])
    assert amended["route"] == "steer" and amended["handle"] == active["handle"]
    assert state.steered[-1][1] == "new plan"

    with pytest.raises(RemoteError, match="cannot apply response_format"):
        await _send(client, child, parent, "amend", response_format={"$theater": "delegate"})
    assert state.steered[-1][1] == "new plan"

    with pytest.raises(RemoteError) as guarded:
        await _send(client, child, parent, "amend", job_handle=queued["handle"])
    assert guarded.value.code == "stale_target"


async def test_amend_without_a_steer_route_falls_back_to_the_queue(
    client, daemon, terminal_provider
):
    parent, child = _pair(daemon)
    terminal_provider.bind(daemon, child.id)
    daemon.registry.set_status(child.id, Status.WORKING)

    amended = await _send(client, child, parent, "amend")

    assert amended["route"] == "queued"
    assert amended["steer_refused"] == "bad_request"


async def test_replace_interrupts_waits_for_idle_and_tells_the_child(client, daemon):
    parent, child, state = await _native_pair(daemon)
    worker = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=child.id)
    launched = daemon.jobs.create(
        handle=worker.id, caller_id=child.id, target_id=worker.id, kind="spawn"
    )
    active = await _active_send(client, parent, child)

    async def observer_finishes_the_interrupted_job():
        while not state.interrupted:
            await asyncio.sleep(0.01)
        daemon.jobs.finish(active["handle"], state="killed", error_code="interrupted")

    finisher = asyncio.create_task(observer_finishes_the_interrupted_job())
    replaced = await _send(client, child, parent, "replace")
    await finisher

    assert replaced["route"] == "replace"
    assert replaced["interrupted_job"] == active["handle"]
    assert state.sent[-1].startswith(f"[Theater: your turn {active['handle']} was interrupted by ")
    assert f"{launched.handle} -> " in state.sent[-1]
    assert "await them, do not respawn.]\n\nnew plan" in state.sent[-1]

    status = await client.call("jobs.status", handle=active["handle"])
    assert status["replaced_by"] == replaced["handle"]
    assert "Re-send the prompt" in status["error"]


async def test_replace_times_out_without_sending(client, daemon, monkeypatch):
    parent, child, state = await _native_pair(daemon)
    await _active_send(client, parent, child)
    sent_before = list(state.sent)
    monkeypatch.setattr(redirect, "REPLACE_IDLE_WAIT_SECONDS", 0.3)

    with pytest.raises(RemoteError) as raised:
        await _send(client, child, parent, "replace")

    assert raised.value.code == "replace_timeout"
    assert state.interrupted, "the interrupt stays applied"
    assert state.sent == sent_before, "nothing was sent"
