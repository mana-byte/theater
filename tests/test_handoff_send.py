"""``send(after=…)``: one handle, queued until the source ends, lanes never overtaken."""

from __future__ import annotations

import asyncio

import pytest

from tests._presence_doubles import AbsentPresence, PresentPresence
from tests.test_control_rpc import _native_pair
from theater.daemon.rpc.cancellation import _cancel
from theater.models import BadRequest, JobState
from theater.protocol import RemoteError


@pytest.fixture(autouse=True)
async def _absent_presence(daemon):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon, "presence", AbsentPresence(), raising=False)
        yield


def _source(daemon, *, name: str = "Writer", prompt: str = "build the parser"):
    participant = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", name=name)
    job = daemon.jobs.create(
        handle=f"{participant.id}#1",
        caller_id="cli",
        target_id=participant.id,
        kind="send",
        prompt=prompt,
    )
    return participant, job


async def _pump(daemon, child, state) -> tuple[str, ...]:
    """Let any scheduled dispatch run, then one explicit pass with the target idle."""
    pending = [t for t in daemon.controls._dispatch_tasks.values() if not t.done()]
    await asyncio.sleep(0)
    await asyncio.gather(*pending, return_exceptions=True)
    state.native_turn_id = None
    return (await daemon.controls.dispatch_queue(child.id)).dispatched


def _heads(state) -> list[str]:
    return [prompt.split("\n")[0] for prompt in state.sent]


async def _send(client, child, caller, **extra) -> dict:
    return await client.call("send", target=child.id, caller_id=caller.id, **extra)


async def test_send_after_queues_until_the_source_ends_then_delivers_one_card(client, daemon):
    parent, child, state = await _native_pair(daemon)
    _, source_job = _source(daemon)

    reply = await _send(client, child, parent, prompt="Review the result", after="Writer")

    assert reply["delivery"] == "queued" and reply["handoff"]["state"] == "waiting"
    assert reply["handoff"]["source"] == source_job.handle
    await _pump(daemon, child, state)
    assert state.sent == []

    daemon.jobs.finish(source_job.handle, state=JobState.DONE, result="PARSER-READY")
    assert (await _pump(daemon, child, state)) in {(reply["handle"],), ()}
    assert len(state.sent) == 1
    delivered = state.sent[0]
    assert delivered.startswith("Review the result")
    assert "PARSER-READY" in delivered and "UNVERIFIED" in delivered and "Writer" in delivered
    assert daemon.store.get_job(reply["handle"]).prompt == delivered
    assert daemon.store.get_handoff_gate(reply["handle"]).state == "opened"


async def test_source_that_already_ended_sends_with_its_card_at_once(client, daemon):
    parent, child, state = await _native_pair(daemon)
    _, source_job = _source(daemon)
    daemon.jobs.finish(source_job.handle, state=JobState.DONE, result="ALREADY-DONE")

    reply = await _send(client, child, parent, prompt="Take over", after="Writer")

    assert reply["handoff"]["state"] == "opened"
    assert len(state.sent) == 1 and "ALREADY-DONE" in state.sent[0]


async def test_failed_source_hands_off_a_labelled_failure(client, daemon):
    parent, child, state = await _native_pair(daemon)
    _, source_job = _source(daemon)
    reply = await _send(client, child, parent, prompt="Diagnose", after=source_job.handle)

    daemon.jobs.finish(
        source_job.handle, state=JobState.CRASHED, result="half a thought", error_code="crashed"
    )
    await _pump(daemon, child, state)

    assert "FAILED source" in state.sent[0] and "crashed" in state.sent[0]
    assert daemon.store.get_job(reply["handle"]).state == JobState.RUNNING


async def test_a_gated_message_blocks_only_its_own_lane(client, daemon):
    parent, child, state = await _native_pair(daemon)
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    _, source_job = _source(daemon)

    first = await _send(client, child, parent, prompt="A", after=source_job.handle)
    second = await _send(client, child, parent, prompt="B")
    other = await _send(client, child, stranger, prompt="C")
    assert {r["delivery"] for r in (first, second, other)} == {"queued"}

    await _pump(daemon, child, state)  # only the other lane moves
    assert _heads(state) == ["C"], "A waits on its source and B stays behind A"
    daemon.jobs.finish(other["handle"], state=JobState.DONE, result="c")
    await _pump(daemon, child, state)
    assert _heads(state) == ["C"], "B must not overtake A"

    daemon.jobs.finish(source_job.handle, state=JobState.DONE, result="s")
    await _pump(daemon, child, state)
    assert _heads(state) == ["C", "A"]
    daemon.jobs.finish(first["handle"], state=JobState.DONE, result="a")
    await _pump(daemon, child, state)
    assert _heads(state) == ["C", "A", "B"]


async def test_presence_is_checked_fresh_at_dispatch_after_the_gate_opens(client, daemon):
    parent, child, state = await _native_pair(daemon)
    _, source_job = _source(daemon)
    reply = await _send(client, child, parent, prompt="later", after=source_job.handle)
    daemon.jobs.finish(source_job.handle, state=JobState.DONE, result="ok")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon, "presence", PresentPresence(), raising=False)
        await _pump(daemon, child, state)
    assert state.sent == []
    assert daemon.store.get_handoff_gate(reply["handle"]).state == "opened"

    await _pump(daemon, child, state)
    assert len(state.sent) == 1


async def test_cancel_withdraws_a_waiting_send_and_closes_its_gate(client, daemon):
    parent, child, state = await _native_pair(daemon)
    _, source_job = _source(daemon)
    reply = await _send(client, child, parent, prompt="later", after=source_job.handle)

    cancelled = await _cancel(daemon, {"handle": reply["handle"], "caller_id": parent.id})

    assert cancelled == {"handle": reply["handle"], "cancelled": True}
    assert daemon.store.get_handoff_gate(reply["handle"]).state == "cancelled"
    daemon.jobs.finish(source_job.handle, state=JobState.DONE, result="ok")
    await _pump(daemon, child, state)
    assert state.sent == []


async def test_awaiting_a_gated_send_that_depends_on_you_is_a_cycle(client, daemon, monkeypatch):
    parent, child, _ = await _native_pair(daemon)
    source, source_job = _source(daemon)
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
    parent, child, state = await _native_pair(daemon)
    _, source_job = _source(daemon)
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
    await _pump(daemon, child, state)
    assert state.sent == []


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"task_kind": "independent_review"}, "spawn_session"),
        ({"if_busy": "queue"}, "default send"),
        ({"after": "Nobody"}, "neither a job handle"),
        ({"task_kind": "bogus"}, "task_kind must be"),
    ],
)
async def test_invalid_send_after_requests_are_refused_before_anything_is_queued(
    client, daemon, extra, message
):
    parent, child, _ = await _native_pair(daemon)
    _source(daemon)
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
    parent, child, state = await _native_pair(daemon)
    _, source_job = _source(daemon)
    sent = await _send(client, child, parent, prompt="one", after=source_job.handle)
    cancelled = await _send(client, child, parent, prompt="two", after=source_job.handle)
    await _cancel(daemon, {"handle": cancelled["handle"], "caller_id": parent.id})
    daemon.jobs.finish(source_job.handle, state=JobState.DONE, result="ok")
    await _pump(daemon, child, state)

    assert sent["delivery"] == "queued"
    gate_signals = [e for e in emitted if "task_kind" in e]
    assert [(e["result"], e["kind"], e["task_kind"]) for e in gate_signals] == [
        ("cancelled", "send", "work"),
        ("success", "send", "work"),
    ]
