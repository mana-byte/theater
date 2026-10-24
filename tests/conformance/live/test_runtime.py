"""A native runtime is a physical route, falls back to the pane path, and refuses what it must."""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from scripts.harness_compat.contract import MockReply
from tests.support import process_table
from tests.support.live_rig import inconclusive, violation
from theater.frontend.client import FrontendResponseError

pytestmark = [pytest.mark.integration, pytest.mark.family("runtime")]

#: A restarted daemon rebinds a live native session in about a second; this is the bound.
_RECONNECT_SECONDS = 30.0


async def test_a_native_runtime_is_proven_by_its_physical_route(live, obligation):
    initial = {}
    if obligation.detail == "settings":
        if len(live.provisioned.models) < 2:
            inconclusive("settings updates need two provisioned models")
        initial["model"] = live.provisioned.models[0]
        if live.harness.launch_parameter_support.reasoning_effort:
            if len(live.provisioned.efforts) < 2:
                inconclusive("settings updates need two provisioned efforts")
            initial["reasoning_effort"] = live.provisioned.efforts[0]
    participant = await live.spawn(live.approval, **initial)
    await live.ready(participant)

    if obligation.detail == "native":
        await _native_turn(live, participant)
    elif obligation.detail == "survival":
        await _survival(live, participant)
    elif obligation.detail == "legacy":
        await _legacy_turn(live, participant)
    elif obligation.detail == "queue":
        await _queue(live, participant)
    elif obligation.detail == "settings":
        await _settings(live, participant)
    else:
        await _refused(live, participant, obligation.detail.removeprefix("unavailable-"))


def _nonce() -> str:
    return f"ROUTE-{uuid.uuid4().hex[:8]}"


async def _turn(live, participant: str, nonce: str):
    live.queue(MockReply(nonce))

    job = await live.send(participant, f"Reply with {nonce}")

    assert job.state == "done", job.error
    assert nonce in str(job.result)
    seen = await live.participant(participant)
    assert seen.transcript_identity is not None and seen.transcript_identity.state == "trusted"
    return seen


def _native_route(seen):
    if seen.native_route is None:
        violation("the harness declares a native runtime but the participant has no native route")
    return seen.native_route


async def _native_turn(live, participant: str) -> None:
    route = _native_route(await _turn(live, participant, _nonce()))
    assert route.health == "connected", route
    assert route.native_session_id, "the native route carries no session identity"


async def _survival(live, participant: str) -> None:
    """A daemon restart leaves the UI and backend running; the next daemon rejoins the session."""
    earlier = _nonce()
    route = _native_route(await _turn(live, participant, earlier))
    pane = await live.pane_pid(participant)
    processes = process_table.table()
    if pane not in processes:
        inconclusive("the pane process is not in the process table")
    running = [processes[pane], *process_table.detached_children(processes, live.daemon_pid)]
    answered = _foreground(live)

    await live.restart_daemon()

    _expect_running(running)
    await _reconnected(live, participant, route.native_session_id)
    await live.ready(participant)
    later = _nonce()
    live.queue(MockReply(later))
    job = await live.send(participant, f"Reply with {later}")

    assert job.state == "done", job.error
    assert later in str(job.result)
    (continued,) = live.turns(f"Reply with {later}")
    assert f"Reply with {earlier}" in continued.user_texts, "the session lost its history"
    assert _foreground(live) == answered + 1, "the model saw more than the one new request"
    assert len(live.turns(f"Reply with {earlier}")) == 1, "the earlier prompt was submitted again"
    _expect_running(running)
    if started := process_table.detached_children(process_table.table(), live.daemon_pid):
        violation(f"the restarted daemon started a backend of its own: {started}")


def _expect_running(recorded: list[process_table.Process]) -> None:
    if gone := process_table.lost(recorded, process_table.table()):
        violation(f"the daemon restart ended processes it does not own: {gone}")


def _foreground(live) -> int:
    """Model requests made on behalf of a turn; one offering no tools is background work."""
    return sum(1 for request in live.mock.requests if request.tools)


async def _reconnected(live, participant: str, session: str | None) -> None:
    """The restarted daemon must rebind the same native session and keep the participant alive."""
    deadline = time.monotonic() + _RECONNECT_SECONDS
    while True:
        seen = await live.participant(participant)
        if seen.status == "dead":
            violation("the participant died with the daemon restart")
        route = seen.native_route
        if route is not None and route.health == "connected":
            if route.native_session_id != session:
                violation(f"the daemon bound {route.native_session_id!r}, not session {session!r}")
            return
        if time.monotonic() > deadline:
            if (broken := await live.broken_component()) is not None:
                inconclusive(f"{broken} is down after the daemon restart")
            violation(f"the native route did not reconnect within {_RECONNECT_SECONDS:g}s: {route}")
        await asyncio.sleep(0.25)


async def _legacy_turn(live, participant: str) -> None:
    """Under a plugin whose probe refuses the native runtime, the pane path still carries a turn."""
    seen = await _turn(live, participant, _nonce())
    if seen.native_route is not None:
        violation("a native route was bound although the runtime probe refused the release")
    assert seen.terminal_route is not None and seen.terminal_route.health == "healthy"


async def _queue(live, participant: str) -> None:
    _native_route(await live.participant(participant))
    prompt, followup, answer = _nonce(), _nonce(), _nonce()
    held = await live.hold_turn(participant, prompt)
    key = uuid.uuid4().hex
    accepted = (
        await live.client.controls.queue_followup(participant, followup, idempotency_key=key)
    ).value
    repeated = (
        await live.client.controls.queue_followup(participant, followup, idempotency_key=key)
    ).value
    assert accepted.job_handle is not None
    assert repeated.job_handle == accepted.job_handle, "a retry minted another queued job"
    assert not live.turns(followup), "the follow-up reached the model before the held turn ended"

    live.queue(MockReply(answer))
    held.hold.release()
    original = await live.job(held.handle)
    queued = await live.job(accepted.job_handle)

    assert original.state == "done", original.error
    assert queued.state == "done", queued.error
    assert answer in str(queued.result)
    (first,) = live.turns(prompt)
    (second,) = live.turns(followup)
    assert prompt in second.user_texts, "the follow-up lost the original turn's history"
    assert second.messages > first.messages


async def _settings(live, participant: str) -> None:
    _native_route(await live.participant(participant))
    changed = {"model": live.provisioned.models[1]}
    effort = live.harness.launch_parameter_support.reasoning_effort
    if effort:
        changed["reasoning_effort"] = live.provisioned.efforts[1]
    before, after = _nonce(), _nonce()
    await _turn(live, participant, before)
    await live.ready(participant)

    held = await live.hold_turn(participant, _nonce())
    try:
        accepted = (
            await live.client.controls.update_settings(
                participant, idempotency_key=uuid.uuid4().hex, **changed
            )
        ).value
    except FrontendResponseError as refusal:
        assert refusal.value.code == "busy", refusal
    else:
        operation = await live._settled(accepted.operation_id)
        assert operation.state == "failed", "settings changed during an active turn"
        assert operation.error is not None and operation.error.code == "busy", operation.error
    held.hold.release()
    assert (await live.job(held.handle)).state == "done"
    await live.ready(participant)

    accepted = (
        await live.client.controls.update_settings(
            participant, idempotency_key=uuid.uuid4().hex, **changed
        )
    ).value
    operation = await live._settled(accepted.operation_id)
    assert operation.state == "succeeded", operation.error
    await _turn(live, participant, after)
    (first,) = live.turns(f"Reply with {before}")
    (second,) = live.turns(f"Reply with {after}")
    assert first.model is not None and second.model is not None
    assert first.model != second.model, "the settings update did not change the wire model"
    if effort:
        assert first.effort is not None and second.effort is not None
        assert first.effort != second.effort, "the settings update did not change the wire effort"


async def _refused(live, participant: str, capability: str) -> None:
    offered = (await live.controls(participant)).actions.get(capability)
    assert offered is None or not offered.admissible, f"{capability} is declared unavailable"
    key = uuid.uuid4().hex
    try:
        if capability == "settings_update":
            result = await live.client.controls.update_settings(
                participant, idempotency_key=key, model=live.provisioned.models[-1]
            )
        elif capability == "queue_followup":
            result = await live.client.controls.queue_followup(
                participant, _nonce(), idempotency_key=key
            )
        elif capability == "interrupt":
            result = await live.client.controls.interrupt(participant, idempotency_key=key)
        elif capability == "send":
            result = await live.client.controls.send(participant, _nonce(), idempotency_key=key)
        else:
            inconclusive(f"no public call exercises the {capability!r} capability yet")
    except FrontendResponseError as refusal:
        assert refusal.value.code == "bad_request", refusal
        assert capability.split("_", maxsplit=1)[0] in refusal.value.message.lower(), refusal
        return
    accepted = result.value
    operation = await live._settled(accepted.operation_id)
    assert operation.state != "succeeded", (
        f"{capability} was accepted although declared unavailable"
    )
    assert operation.error is not None and operation.error.code == "bad_request", operation.error
