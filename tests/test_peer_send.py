"""Seamless peer send: one handle whether delivered at once or queued, never replayed."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from tests._presence_doubles import AbsentPresence
from tests.test_control_rpc import _active_send, _native_pair
from tests.test_presence_controls import Rig, _operation_rows
from tests.test_send import _target
from theater.constants.daemon import (
    CONTROL_QUEUE_MAX_PENDING,
    CONTROL_QUEUE_PEER_MAX_PENDING,
    CONTROL_QUEUE_SENDER_MAX_PENDING,
)
from theater.daemon.persistence.repositories.job_repairs import RepairAttempt, RepairState
from theater.daemon.presence import PresenceState
from theater.daemon.schema import jobs as jobs_table
from theater.models import Busy, JobState, NotYourChild, NotYourJob, TranscriptUntrusted
from theater.protocol import RemoteError


@pytest.fixture
async def rig(store):
    rig = Rig(store, "p1")
    yield rig
    await rig.service.aclose()


def _job_handles(store, pid: str) -> list[str]:
    rows = store.conn.execute(select(jobs_table.c.handle).where(jobs_table.c.target_id == pid))
    return [row[0] for row in rows]


async def _busy(rig, pid: str = "p1") -> None:
    rig.state(pid).native_turn_id = "busy-turn"


def _free(rig, pid: str = "p1") -> None:
    rig.state(pid).native_turn_id = None


async def test_idle_target_gets_the_message_at_once(rig):
    job, queued = await rig.service.send_when_safe("p1", caller_id="peer", prompt="hi")

    assert queued is False
    assert rig.state("p1").sent == ["hi"]
    assert _job_handles(rig.store, "p1") == [job.handle]


async def test_busy_target_queues_under_one_handle_and_delivers_after_idle(rig):
    await _busy(rig)
    job, queued = await rig.service.send_when_safe("p1", caller_id="peer", prompt="later")

    assert queued is True and rig.state("p1").sent == []
    assert _job_handles(rig.store, "p1") == [job.handle], "no second job for the same message"
    (operation,) = rig.store.queued_control_operations("p1")
    assert operation.job_handle == job.handle

    _free(rig)
    outcome = await rig.service.dispatch_queue("p1")
    assert outcome.dispatched == (job.handle,)
    assert rig.state("p1").sent == ["later"]


async def test_attended_target_queues_and_delivers_only_after_fresh_absence(rig):
    rig.presence.refusals = {"p1"}
    job, queued = await rig.service.send_when_safe("p1", caller_id="peer", prompt="later")

    assert queued is True and rig.state("p1").sent == []
    assert (await rig.service.dispatch_queue("p1")).deferred is True
    assert rig.store.get_job(job.handle).state == JobState.RUNNING

    rig.presence.refusals.clear()
    assert (await rig.service.dispatch_queue("p1")).dispatched == (job.handle,)
    assert rig.state("p1").sent == ["later"]


async def test_permanent_refusal_is_not_masked_by_presence_and_accepts_nothing(rig):
    rig.presence.refusals = {"p1"}
    rig.presence.preflight_exception = TranscriptUntrusted("no trusted transcript")

    with pytest.raises(TranscriptUntrusted):
        await rig.service.send_when_safe("p1", caller_id="peer", prompt="hi")

    assert _operation_rows(rig.store, "p1") == []
    assert _job_handles(rig.store, "p1") == []


async def test_queue_admission_is_atomic_job_and_control_row(rig, monkeypatch):
    await _busy(rig)

    def explode(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(rig.service, "_reserve", explode)
    with pytest.raises(RuntimeError):
        await rig.service.send_when_safe("p1", caller_id="peer", prompt="later")

    assert _job_handles(rig.store, "p1") == []
    assert _operation_rows(rig.store, "p1") == []


async def test_sender_bound_leaves_room_for_others_and_lanes_keep_send_order(rig):
    await _busy(rig)
    mine = [
        (await rig.service.send_when_safe("p1", caller_id="a", prompt=f"a{i}"))[0].handle
        for i in range(CONTROL_QUEUE_SENDER_MAX_PENDING)
    ]
    with pytest.raises(Busy, match="cancel_job"):
        await rig.service.send_when_safe("p1", caller_id="a", prompt="one too many")
    other = (await rig.service.send_when_safe("p1", caller_id="b", prompt="b0"))[0].handle

    heads = [operation.job_handle for operation in rig.service._lane_heads("p1")]
    assert heads == [mine[0], other], "each sender's oldest row heads its lane, in send order"


async def test_repair_fence_holds_a_peer_message_until_it_clears(rig):
    rig.store.create_repair_attempt(
        RepairAttempt(
            attempt_id="x~repair1",
            job_handle="x",
            participant_id="p1",
            attempt=1,
            state=RepairState.SETTLED,
            fence=True,
        )
    )
    job, queued = await rig.service.send_when_safe("p1", caller_id="peer", prompt="later")

    assert queued is True
    assert (await rig.service.dispatch_queue("p1")).deferred is True
    assert rig.state("p1").sent == []

    rig.store.update_repair_attempt(
        "x~repair1", updated_at=1.0, state=RepairState.SETTLED, fence=False, outcome="done"
    )
    assert (await rig.service.dispatch_queue("p1")).dispatched == (job.handle,)


async def test_cancel_withdraws_only_untransmitted_work_for_its_requester(rig):
    await _busy(rig)
    job, _ = await rig.service.send_when_safe("p1", caller_id="peer", prompt="later")

    with pytest.raises(NotYourJob):
        await rig.service.cancel_job(job.handle, requester_id="someone-else")
    outcome = await rig.service.cancel_job(job.handle, requester_id="peer")
    assert outcome.cancelled is True
    stored = rig.store.get_job(job.handle)
    assert stored.state == JobState.KILLED and stored.error_code == "cancelled"
    _free(rig)
    assert (await rig.service.dispatch_queue("p1")).dispatched == ()
    assert rig.state("p1").sent == []

    delivered, _ = await rig.service.send_when_safe("p1", caller_id="peer", prompt="now")
    late = await rig.service.cancel_job(delivered.handle, requester_id="peer")
    assert late.cancelled is False and late.reason in {"delivered", "delivery_uncertain"}
    assert (await rig.service.cancel_job(job.handle, requester_id="peer")).reason == "finished"


@pytest.fixture
async def _absent(daemon):
    with pytest.MonkeyPatch.context() as patch:
        absent = AbsentPresence(capture_screen=daemon.presence.capture_screen)
        patch.setattr(daemon, "presence", absent, raising=False)
        yield


async def test_a_stranger_may_message_a_busy_child_and_it_dispatches(client, daemon, _absent):
    parent, child, state = await _native_pair(daemon)
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    active = await _active_send(client, parent, child)

    queued = await client.call("send", target=child.id, prompt="hello", caller_id=stranger.id)
    assert queued["delivery"] == "queued" and queued["handle"] != active["handle"]
    with pytest.raises(RemoteError) as refused:
        await client.call(
            "send", target=child.id, prompt="x", caller_id=stranger.id, if_busy="queue"
        )
    assert refused.value.code == "not_your_child", "explicit queueing stays parent-only"

    state.native_turn_id = None
    daemon.jobs.finish(active["handle"], state=JobState.DONE, result="ok")
    outcome = await daemon.controls.dispatch_queue(child.id)
    assert outcome.dispatched == (queued["handle"],), "dispatch re-authorizes a peer as a send"
    assert state.sent[-1] == "hello"

    with pytest.raises(RemoteError) as stranger_cancel:
        await client.call("jobs.cancel", handle=queued["handle"], caller_id=parent.id)
    assert stranger_cancel.value.code == "not_your_job"


async def test_provider_route_refusal_after_admission_checks_leaves_one_job(
    client, daemon, terminal_provider, _absent, monkeypatch
):
    target = await _target(client, daemon)
    provider = daemon.controls._provider
    real = provider.require
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Busy("provider route is reconciling")
        return real(*args, **kwargs)

    monkeypatch.setattr(provider, "require", flaky)
    sent = await client.call("send", target=target["id"], prompt="hi", caller_id="peer")

    assert sent["delivery"] == "queued"
    assert _job_handles(daemon.store, target["id"]) == [sent["handle"]], (
        "the refused plain attempt created no job"
    )


async def test_presence_arriving_during_the_modal_check_leaves_one_queued_job(
    client, daemon, terminal_provider, monkeypatch
):
    real_capture = daemon.presence.capture_screen
    presence = AbsentPresence()

    async def capture_then_human_arrives(participant_id, *, max_bytes):
        screen = await real_capture(participant_id, max_bytes=max_bytes)
        presence.set_state(PresenceState.PRESENT, "human arrived mid-send")
        return screen

    presence._capture_screen = capture_then_human_arrives
    monkeypatch.setattr(daemon, "presence", presence, raising=False)
    target = await _target(client, daemon)

    sent = await client.call("send", target=target["id"], prompt="hi", caller_id="peer")

    assert sent["delivery"] == "queued"
    assert _job_handles(daemon.store, target["id"]) == [sent["handle"]], "no stranded second job"
    assert [row["delivery_phase"] for row in _operation_rows(daemon.store, target["id"])] == [
        "queued"
    ]
    assert terminal_provider.deliveries == []


async def test_peers_cannot_take_the_parents_share_of_the_queue(rig):
    await _busy(rig)
    peers = CONTROL_QUEUE_PEER_MAX_PENDING // CONTROL_QUEUE_SENDER_MAX_PENDING
    for peer in range(peers):
        for _ in range(CONTROL_QUEUE_SENDER_MAX_PENDING):
            await rig.service.send_when_safe("p1", caller_id=f"peer{peer}", prompt="x")

    with pytest.raises(Busy, match="reserved for its parent"):
        await rig.service.send_when_safe("p1", caller_id="one-more-peer", prompt="x")
    parent_job = await rig.service.queue_followup("p1", caller_id="parent", prompt="explicit")

    assert rig.store.get_job(parent_job.handle).state == JobState.RUNNING
    assert rig.store.queued_control_operation_count("p1") == CONTROL_QUEUE_PEER_MAX_PENDING + 1
    assert CONTROL_QUEUE_PEER_MAX_PENDING < CONTROL_QUEUE_MAX_PENDING


async def test_a_participant_never_queues_a_message_to_itself(rig):
    await _busy(rig)

    with pytest.raises(Busy):
        await rig.service.send_when_safe("p1", caller_id="p1", prompt="note to self")
    with pytest.raises(NotYourChild):
        await rig.service.queue_followup("p1", caller_id="p1", prompt="x", origin="send")
    assert _operation_rows(rig.store, "p1") == [] and _job_handles(rig.store, "p1") == []

    _free(rig)
    _job, queued = await rig.service.send_when_safe("p1", caller_id="p1", prompt="idle self send")
    assert queued is False and rig.state("p1").sent == ["idle self send"]
