"""``send(after=…)``: one handle, queued until the source ends, lanes never overtaken."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.handoff_rig import CANCELLED_BEFORE_DELIVERY, handoff_source, pump
from tests.support.native_rpc import native_pair
from tests.support.presence import PresentPresence
from tests.support.waiting import wait_until
from theater.daemon.awaiting import never_transmitted
from theater.daemon.rpc.cancellation import _cancel
from theater.daemon.rpc.jobs import _job_to_dict
from theater.harness.contracts.runtime import DeliveryResult
from theater.models import BadRequest, JobState
from theater.protocol import RemoteError

pytestmark = pytest.mark.usefixtures("absent_presence")


def _heads(state) -> list[str]:
    return [prompt.split("\n")[0] for prompt in state.sent]


async def _send(client, child, caller, **extra) -> dict:
    return await client.call("send", target=child.id, caller_id=caller.id, **extra)


async def test_send_after_queues_until_the_source_ends_then_delivers_one_card(client, daemon):
    parent, child, state = await native_pair(daemon)
    _, source_job = handoff_source(daemon)

    reply = await _send(client, child, parent, prompt="Review the result", after="Writer")

    assert reply["delivery"] == "queued" and reply["handoff"]["state"] == "waiting"
    assert reply["handoff"]["source"] == source_job.handle
    await pump(daemon, child, state)
    assert state.sent == []

    daemon.jobs.finish(source_job.handle, state=JobState.DONE, result="PARSER-READY")
    assert (await pump(daemon, child, state)) in {(reply["handle"],), ()}
    assert len(state.sent) == 1
    delivered = state.sent[0]
    assert delivered.startswith("Review the result")
    assert "PARSER-READY" in delivered and "UNVERIFIED" in delivered and "Writer" in delivered
    assert daemon.store.get_job(reply["handle"]).prompt == delivered
    assert daemon.store.get_handoff_gate(reply["handle"]).state == "opened"


async def test_send_after_trails_its_card_with_the_attached_skills(client, daemon):
    parent, child, state = await native_pair(daemon)
    _, source_job = handoff_source(daemon)

    reply = await _send(
        client, child, parent, prompt="Review the result", after="Writer", skills=["theater-debate"]
    )
    daemon.jobs.finish(source_job.handle, state=JobState.DONE, result="PARSER-READY")
    await pump(daemon, child, state)

    (delivered,) = state.sent
    assert delivered.startswith("Review the result\n\n[Theater handoff")
    assert delivered.index("PARSER-READY") < delivered.index("[Theater skill theater-debate")
    assert daemon.store.get_job(reply["handle"]).prompt == delivered


async def test_source_that_already_ended_sends_with_its_card_at_once(client, daemon):
    parent, child, state = await native_pair(daemon)
    _, source_job = handoff_source(daemon)
    daemon.jobs.finish(source_job.handle, state=JobState.DONE, result="ALREADY-DONE")

    reply = await _send(client, child, parent, prompt="Take over", after="Writer")

    assert reply["handoff"]["state"] == "opened"
    assert len(state.sent) == 1 and "ALREADY-DONE" in state.sent[0]


async def test_failed_source_hands_off_a_labelled_failure(client, daemon):
    parent, child, state = await native_pair(daemon)
    _, source_job = handoff_source(daemon)
    reply = await _send(client, child, parent, prompt="Diagnose", after=source_job.handle)

    daemon.jobs.finish(
        source_job.handle, state=JobState.CRASHED, result="half a thought", error_code="crashed"
    )
    await pump(daemon, child, state)

    assert "FAILED source" in state.sent[0] and "crashed" in state.sent[0]
    assert daemon.store.get_job(reply["handle"]).state == JobState.RUNNING


async def test_a_gated_message_blocks_only_its_own_lane(client, daemon):
    parent, child, state = await native_pair(daemon)
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    _, source_job = handoff_source(daemon)

    first = await _send(client, child, parent, prompt="A", after=source_job.handle)
    second = await _send(client, child, parent, prompt="B")
    other = await _send(client, child, stranger, prompt="C")
    assert {r["delivery"] for r in (first, second, other)} == {"queued"}

    await pump(daemon, child, state)  # only the other lane moves
    assert _heads(state) == ["C"], "A waits on its source and B stays behind A"
    daemon.jobs.finish(other["handle"], state=JobState.DONE, result="c")
    await pump(daemon, child, state)
    assert _heads(state) == ["C"], "B must not overtake A"

    daemon.jobs.finish(source_job.handle, state=JobState.DONE, result="s")
    await pump(daemon, child, state)
    assert _heads(state) == ["C", "A"]
    daemon.jobs.finish(first["handle"], state=JobState.DONE, result="a")
    await pump(daemon, child, state)
    assert _heads(state) == ["C", "A", "B"]


async def test_presence_is_checked_fresh_at_dispatch_after_the_gate_opens(client, daemon):
    parent, child, state = await native_pair(daemon)
    _, source_job = handoff_source(daemon)
    reply = await _send(client, child, parent, prompt="later", after=source_job.handle)
    daemon.jobs.finish(source_job.handle, state=JobState.DONE, result="ok")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon, "presence", PresentPresence(), raising=False)
        await pump(daemon, child, state)
    assert state.sent == []
    assert daemon.store.get_handoff_gate(reply["handle"]).state == "opened"

    await pump(daemon, child, state)
    assert len(state.sent) == 1


async def test_cancel_withdraws_a_waiting_send_and_closes_its_gate(client, daemon):
    parent, child, state = await native_pair(daemon)
    _, source_job = handoff_source(daemon)
    reply = await _send(client, child, parent, prompt="later", after=source_job.handle)

    cancelled = await _cancel(daemon, {"handle": reply["handle"], "caller_id": parent.id})

    assert cancelled == {"handle": reply["handle"], "cancelled": True}
    assert daemon.store.get_handoff_gate(reply["handle"]).state == "cancelled"
    row = _job_to_dict(daemon.store.get_job(reply["handle"]))
    assert row["error"] == CANCELLED_BEFORE_DELIVERY
    assert row["result"].startswith("Cancelled by its requester before delivery")
    assert daemon.handoff.describe(reply["handle"])["error"] == "the task ended before it launched"
    daemon.jobs.finish(source_job.handle, state=JobState.DONE, result="ok")
    await pump(daemon, child, state)
    assert state.sent == []


async def test_await_releases_a_protected_cancelled_send_whose_gate_had_opened(client, daemon):
    parent, child, state = await native_pair(daemon)
    _, source_job = handoff_source(daemon)
    reply = await _send(client, child, parent, prompt="later", after=source_job.handle)
    daemon.jobs.finish(source_job.handle, state=JobState.DONE, result="ok")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon, "presence", PresentPresence(), raising=False)
        await pump(daemon, child, state)  # gate opens; presence keeps the message queued
        assert daemon.store.get_handoff_gate(reply["handle"]).state == "opened"
        waiter = asyncio.create_task(
            client.call("jobs.await", handles=[reply["handle"]], max_wait=2, caller_id=parent.id)
        )
        await wait_until(lambda: daemon.jobs.wait_graph != {}, what="await admission")
        assert not waiter.done()
        cancelled = await _cancel(daemon, {"handle": reply["handle"], "caller_id": parent.id})
        assert cancelled == {"handle": reply["handle"], "cancelled": True}
        (job,) = await asyncio.wait_for(waiter, 5.0)

    assert state.sent == []
    assert daemon.store.get_handoff_gate(reply["handle"]).state == "opened"
    assert job["state"] == "killed" and job["await_reason"] == "job_terminal"


async def _queued_plain_send_await(client, daemon, parent, child):
    """A plain send held queued by a present human, plus a protected await on it."""
    reply = await _send(client, child, parent, prompt="hello")
    assert reply["delivery"] == "queued"
    waiter = asyncio.create_task(
        client.call("jobs.await", handles=[reply["handle"]], max_wait=2, caller_id=parent.id)
    )
    await wait_until(lambda: daemon.jobs.wait_graph != {}, what="await admission")
    assert not waiter.done()
    return reply, waiter


async def test_await_releases_a_plain_send_cancelled_while_queued(client, daemon):
    parent, child, state = await native_pair(daemon)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon, "presence", PresentPresence(), raising=False)
        reply, waiter = await _queued_plain_send_await(client, daemon, parent, child)
        await _cancel(daemon, {"handle": reply["handle"], "caller_id": parent.id})
        (job,) = await asyncio.wait_for(waiter, 5.0)

    assert state.sent == [] and job["await_reason"] == "job_terminal"
    assert job["error"] == CANCELLED_BEFORE_DELIVERY
    assert job["result"].startswith("Cancelled by its requester before delivery")


async def test_await_releases_a_queued_send_cancelled_by_interrupt(client, daemon):
    parent, child, state = await native_pair(daemon)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon, "presence", PresentPresence(), raising=False)
        reply, waiter = await _queued_plain_send_await(client, daemon, parent, child)
        await daemon.controls.cancel_queued_followups(child.id)
        (job,) = await asyncio.wait_for(waiter, 5.0)

    assert daemon.store.get_job(reply["handle"]).error_code == "interrupted"
    assert state.sent == [] and job["await_reason"] == "job_terminal"


async def test_await_releases_a_queued_send_failed_by_restart_recovery(client, daemon):
    parent, child, state = await native_pair(daemon)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon, "presence", PresentPresence(), raising=False)
        reply, waiter = await _queued_plain_send_await(client, daemon, parent, child)
        daemon.controls.fail_undelivered_followups([child.id])
        (job,) = await asyncio.wait_for(waiter, 5.0)

    stored = daemon.store.get_job(reply["handle"])
    assert stored.state == JobState.CRASHED and stored.error_code == "daemon_restarted"
    assert state.sent == [] and job["await_reason"] == "job_terminal"


async def test_restart_closes_a_cancelled_row_whose_job_never_finished_as_unsent(client, daemon):
    parent, child, _ = await native_pair(daemon)
    reply = await _send(client, child, parent, prompt="hello")
    (op,) = daemon.store.control_operations_for_job(reply["handle"])
    daemon.store.settle_control_operation(  # crash between settlement and job finish
        op.operation_id,
        result=DeliveryResult.REJECTED,
        error_code="cancelled",
        error="cancelled",
        updated_at=1.0,
    )

    daemon.controls.fail_undelivered_followups([child.id])

    stored = daemon.store.get_job(reply["handle"])
    assert stored.state == JobState.CRASHED and never_transmitted(daemon.store, stored)


@pytest.mark.parametrize("shape", ["accepted", "dispatched", "turn", "barrier"])
async def test_a_restart_crashed_send_that_may_have_transmitted_stays_held(client, daemon, shape):
    parent, child, _ = await native_pair(daemon)
    reply = await _send(client, child, parent, prompt="hello")
    (op,) = daemon.store.control_operations_for_job(reply["handle"])
    store, rejected = daemon.store, DeliveryResult.REJECTED
    store.mark_control_operation_dispatched(op.operation_id, updated_at=1.0)
    if shape == "accepted":
        store.settle_control_operation(
            op.operation_id, result=DeliveryResult.ACCEPTED, updated_at=2.0
        )
    elif shape == "turn":
        store.settle_control_operation(
            op.operation_id,
            result=rejected,
            native_turn_id="t1",
            error_code="daemon_restarted",
            updated_at=2.0,
        )
    elif shape == "barrier":
        store.settle_control_operation(
            op.operation_id,
            result=rejected,
            execution_barrier=True,
            error_code="daemon_restarted",
            updated_at=2.0,
        )
    daemon.jobs.finish(reply["handle"], state=JobState.CRASHED, error_code="daemon_restarted")

    assert never_transmitted(store, store.get_job(reply["handle"])) is False


async def test_a_delivered_send_with_an_opened_gate_stays_held_by_presence(client, daemon):
    parent, child, state = await native_pair(daemon)
    _, source_job = handoff_source(daemon)
    reply = await _send(client, child, parent, prompt="later", after=source_job.handle)
    daemon.jobs.finish(source_job.handle, state=JobState.DONE, result="ok")
    await pump(daemon, child, state)
    assert len(state.sent) == 1
    daemon.jobs.finish(reply["handle"], state=JobState.KILLED, error_code="cancelled")

    job = daemon.store.get_job(reply["handle"])
    assert never_transmitted(daemon.store, job) is False


async def test_a_killed_send_with_a_dispatched_row_is_never_proven_unsent(client, daemon):
    parent, child, _ = await native_pair(daemon)
    reply = await _send(client, child, parent, prompt="hello")
    (operation,) = daemon.store.control_operations_for_job(reply["handle"])
    daemon.store.mark_control_operation_dispatched(operation.operation_id, updated_at=1.0)
    daemon.jobs.finish(reply["handle"], state=JobState.KILLED, error_code="interrupted")

    assert never_transmitted(daemon.store, daemon.store.get_job(reply["handle"])) is False


async def test_awaiting_a_gated_send_that_depends_on_you_is_a_cycle(client, daemon, monkeypatch):
    parent, child, _ = await native_pair(daemon)
    source, source_job = handoff_source(daemon)
    gated = await _send(client, child, parent, prompt="A", after=source_job.handle)
    behind = await _send(client, child, parent, prompt="B")

    # The source's own participant would wait on a message that starts only after it finishes.
    with pytest.raises(RemoteError, match="deadlock"):
        await client.call("jobs.await", handles=[gated["handle"]], max_wait=1, caller_id=source.id)
    # A later message in the same lane inherits the dependency.
    assert daemon.handoff.dependency_targets([behind["handle"]]) == [source.id]
    with pytest.raises(RemoteError, match="deadlock"):
        await client.call("jobs.await", handles=[behind["handle"]], max_wait=1, caller_id=source.id)
    monkeypatch.setattr("theater.daemon.handoff.service.MAX_CHAIN", 1)
    with pytest.raises(BadRequest, match="exceed"):
        daemon.handoff.dependency_targets([behind["handle"]])


async def test_restart_fails_a_queued_native_gate_closed_and_recover_keeps_a_live_one(
    client, daemon
):
    parent, child, state = await native_pair(daemon)
    _, source_job = handoff_source(daemon)
    reply = await _send(client, child, parent, prompt="later", after=source_job.handle)

    daemon.jobs._events.clear()
    await daemon.handoff.recover()  # a live gate is re-armed, never replayed or dropped
    assert daemon.store.get_handoff_gate(reply["handle"]).state == "waiting"
    assert reply["handle"] in daemon.jobs._events

    daemon.controls.fail_undelivered_followups([child.id])  # a restart's undelivered-queue sweep
    job = daemon.store.get_job(reply["handle"])
    assert (job.state, job.error_code) == (JobState.CRASHED, "daemon_restarted")
    assert daemon.store.get_handoff_gate(reply["handle"]).state == "cancelled"
    daemon.jobs.finish(source_job.handle, state=JobState.DONE, result="ok")
    await pump(daemon, child, state)
    assert state.sent == []


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"task_kind": "independent_review"}, "spawn_session"),
        ({"if_busy": "queue"}, "default send"),
        ({"job_handle": "old#1"}, "cannot be combined with job_handle"),
        ({"after": "Nobody"}, "neither a job handle"),
        ({"task_kind": "bogus"}, "task_kind must be"),
    ],
)
async def test_invalid_send_after_requests_are_refused_before_anything_is_queued(
    client, daemon, extra, message
):
    parent, child, _ = await native_pair(daemon)
    handoff_source(daemon)
    params = {"prompt": "x", "after": "Writer", **extra}
    with pytest.raises(RemoteError, match=message):
        await _send(client, child, parent, **params)
    assert daemon.store.queued_control_operations(child.id) == []


async def test_gate_transitions_emit_one_low_cardinality_signal(client, daemon, monkeypatch):
    emitted: list[dict] = []
    monkeypatch.setattr(
        "theater.daemon.handoff.service.timing.emit",
        lambda spec, ms, **fields: emitted.append(fields),
    )
    parent, child, state = await native_pair(daemon)
    _, source_job = handoff_source(daemon)
    sent = await _send(client, child, parent, prompt="one", after=source_job.handle)
    cancelled = await _send(client, child, parent, prompt="two", after=source_job.handle)
    await _cancel(daemon, {"handle": cancelled["handle"], "caller_id": parent.id})
    daemon.jobs.finish(source_job.handle, state=JobState.DONE, result="ok")
    await pump(daemon, child, state)

    assert sent["delivery"] == "queued"
    gate_signals = [e for e in emitted if "task_kind" in e]
    assert [(e["result"], e["kind"], e["task_kind"]) for e in gate_signals] == [
        ("cancelled", "send", "work"),
        ("success", "send", "work"),
    ]
