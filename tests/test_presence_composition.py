"""Cross-surface presence guarantees through the real daemon RPC boundary."""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import delete

from tests.rig.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState, completed_outcome
from theater.client import DaemonClient
from theater.daemon.persistence.repositories.runtime_bindings import ParticipantRuntimeBinding
from theater.daemon.presence import PresenceSnapshot, PresenceState
from theater.daemon.schema import participants
from theater.harness.contracts.runtime import RuntimeContext, RuntimeLifecyclePhase, RuntimeWiring
from theater.models import HumanPresent, JobState, Status, now
from theater.protocol import RemoteError


@pytest.fixture(autouse=True)
def _restore_presence_before_shutdown(daemon):
    original = getattr(daemon, "presence", None)
    yield
    daemon.presence = original


class ControlledPresence:
    """Explicit focus facts; only the OS observation seam is replaced."""

    def __init__(self, target: str):
        self.target = target
        self.revision = 0
        self.state = PresenceState.ABSENT
        self.changed = asyncio.Event()
        self.waiting = asyncio.Event()

    def set(self, state: PresenceState) -> None:
        self.state = state
        self.revision += 1
        old, self.changed = self.changed, asyncio.Event()
        old.set()

    def snapshot(self, participant_id: str) -> PresenceSnapshot:
        state = self.state if participant_id == self.target else PresenceState.ABSENT
        return PresenceSnapshot(state, "controlled-focus", self.revision, time.time())

    async def refresh(self) -> None:
        pass

    async def require_absent(self, participant_id: str) -> None:
        await self.refresh()
        if self.snapshot(participant_id).protected:
            raise HumanPresent("Human focus protects this pane; await departure before retrying.")

    async def wait_for_change(self, after_revision: int) -> int:
        self.waiting.set()
        while self.revision <= after_revision:
            await self.changed.wait()
        return self.revision


async def _pair(daemon, terminal_provider, monkeypatch):
    daemon.config.reasoning["vibe"] = ["high"]
    parent = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent.id)
    terminal_provider.bind(daemon, child.id)
    state = FakeRuntimeState(participant_id=child.id, native_session_id="presence-thread")
    daemon.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=child.id,
            harness="vibe",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=1,
            lifecycle=RuntimeLifecyclePhase.ACTIVE,
            native_session_id="presence-thread",
            created_at=now(),
            updated_at=now(),
        )
    )
    runtime = FakeRuntime(
        RuntimeContext(
            participant_id=child.id, cwd="/tmp", io=FakeRuntimeIO(state), backend_generation=1
        )
    )

    async def create():
        return runtime

    installed = await daemon.runtime_manager.get_or_create(
        child.id, backend_generation=1, create=create
    )
    assert daemon.runtime_manager.record_snapshot(child.id, installed, await installed.snapshot())
    presence = ControlledPresence(child.id)
    monkeypatch.setattr(daemon, "presence", presence, raising=False)
    return parent, daemon.registry.get(child.id), state, presence


@pytest.mark.parametrize("protected", [PresenceState.PRESENT, PresenceState.UNKNOWN])
async def test_protected_rpc_mutations_leave_jobs_controls_and_participant_unchanged(
    client, daemon, terminal_provider, monkeypatch, protected
):
    parent, child, state, presence = await _pair(daemon, terminal_provider, monkeypatch)
    presence.set(protected)
    original = daemon.registry.get(child.id).to_dict()
    mutations = [
        ("send", {"target": child.id, "prompt": "agent send", "caller_id": parent.id}),
        ("send", {"target": child.id, "prompt": "CLI send", "caller_id": "cli"}),
        ("participant.steer", {"target": child.id, "prompt": "amend", "caller_id": parent.id}),
        (
            "participant.queue_followup",
            {"target": child.id, "prompt": "later", "caller_id": parent.id},
        ),
        ("participant.interrupt", {"target": child.id, "caller_id": parent.id}),
        (
            "participant.settings.update",
            {"target": child.id, "reasoning_effort": "high", "caller_id": parent.id},
        ),
        ("participant.status", {"id": child.id, "status": "working"}),
        ("participant.kill", {"id": child.id, "caller_id": parent.id}),
    ]
    for method, params in mutations:
        with pytest.raises(RemoteError) as raised:
            await client.call(method, **params)
        assert raised.value.code == "human_present", method
    assert daemon.registry.get(child.id).to_dict() == original
    assert daemon.store.running_jobs_for_target(child.id) == []
    assert daemon.store.queued_control_operations(child.id) == []
    assert state.sent == state.steered == state.interrupted == terminal_provider.deliveries == []
    assert state.settings == {}
    row = await client.call("participants.get", id=child.id)
    controls = await client.call("participant.controls", target=child.id)
    assert row["human_presence"]["state"] == controls["human_presence"]["state"] == protected
    assert row["human_presence"]["protected"] is True


async def test_protected_queue_stays_fifo_and_awaits_obey_the_admission_gate(
    client, daemon, terminal_provider, monkeypatch
):
    parent, child, state, presence = await _pair(daemon, terminal_provider, monkeypatch)
    state.native_turn_id = "human-work"
    queued = [
        await client.call(
            "participant.queue_followup", target=child.id, caller_id=parent.id, prompt=prompt
        )
        for prompt in ("first", "second")
    ]
    handles = [row["handle"] for row in queued]
    presence.set(PresenceState.PRESENT)
    state.native_turn_id = None
    daemon.registry.set_status(child.id, Status.WORKING)
    waiter_client = DaemonClient(autostart=False)
    await waiter_client.connect()
    waiter = asyncio.create_task(
        waiter_client.call("jobs.await", handles=[handles[0]], caller_id=parent.id, max_wait=2)
    )
    try:
        await asyncio.wait_for(presence.waiting.wait(), timeout=1)
        outcome = await daemon.controls.dispatch_queue(child.id)
        assert outcome.deferred
        assert not waiter.done()
        assert state.sent == []
        assert [
            row.job_handle for row in daemon.store.queued_control_operations(child.id)
        ] == handles
        with pytest.raises(RemoteError) as raised:
            await client.call("participant.interrupt", target=child.id, caller_id=parent.id)
        assert raised.value.code == "human_present"
        assert [
            row.job_handle for row in daemon.store.queued_control_operations(child.id)
        ] == handles

        # The await was admitted while protected, so it is gated: the
        # departure clears the gate, but the still-running queued job cannot
        # qualify on the departure alone.
        presence.set(PresenceState.ABSENT)
        await asyncio.sleep(0.15)
        assert not waiter.done()
        await daemon.controls.dispatch_queue(child.id)
        assert state.sent == ["first"]
        terminal = completed_outcome(state)
        state.native_turn_id = None
        await daemon.controls.record_terminal_evidence(
            child.id, backend_generation=1, outcome=terminal
        )
        await daemon.controls.dispatch_queue(child.id)
        assert state.sent == ["first", "second"]
        # The dispatched followup's job is now terminal: with the gate
        # already cleared by the observed departure, both conditions hold and
        # the await resolves — the terminal condition was observed last.
        released = (await asyncio.wait_for(waiter, timeout=1))[0]
        assert released["await_reason"] == "job_terminal"
        assert released["participant_status"] == "working"
        assert released["state"] == "done"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        await waiter_client.aclose()


async def test_terminal_job_hold_and_no_job_presence_wait_share_truth_without_fabrication(
    client, daemon, terminal_provider, monkeypatch
):
    parent, child, _state, presence = await _pair(daemon, terminal_provider, monkeypatch)
    daemon.registry.set_status(child.id, Status.WORKING)
    absent = (await client.call("jobs.await", handles=[child.id], max_wait=0))[0]
    assert absent["await_reason"] == "already_absent"
    assert absent["participant_status"] == "working"
    assert not {"state", "kind", "prompt", "result"} & absent.keys()
    assert daemon.jobs.get(child.id) is None

    daemon.jobs.create(handle=child.id, caller_id=parent.id, target_id=child.id, kind="spawn")
    daemon.jobs.finish(child.id, state=JobState.DONE, result="existing result")
    presence.set(PresenceState.UNKNOWN)
    timed = (await client.call("jobs.await", handles=[child.id], max_wait=0))[0]
    assert timed["await_reason"] == "timeout"
    assert timed["state"] == "done"
    assert timed["result"] == "existing result"
    assert timed["human_presence"]["protected"] is True
    presence.waiting.clear()
    waiter = asyncio.create_task(client.call("jobs.await", handles=[child.id], max_wait=2))
    try:
        await asyncio.wait_for(presence.waiting.wait(), timeout=1)
        assert not waiter.done()
        presence.set(PresenceState.ABSENT)
        released = (await asyncio.wait_for(waiter, timeout=1))[0]
        assert released["await_reason"] == "presence_released"
        assert released["state"] == "done"
        assert released["participant_status"] == "working"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def test_real_monitor_paneless_targets_remain_protected_but_pruned_jobs_complete(
    client, daemon
):
    target = daemon.registry.create_spawned(harness="pi", cwd="/tmp")
    row = (await client.call("jobs.await", handles=[target.id], max_wait=1))[0]
    assert row["await_reason"] == "timeout"
    assert row["human_presence"]["state"] == "unknown"
    assert row["human_presence"]["protected"] is True
    assert "state" not in row
    daemon.jobs.create(handle=target.id, caller_id="cli", target_id=target.id, kind="spawn")
    daemon.jobs.finish(target.id, state=JobState.DONE, result="retained result")
    daemon.store.conn.execute(delete(participants).where(participants.c.id == target.id))
    row = (await client.call("jobs.await", handles=[target.id], max_wait=1))[0]
    assert row["await_reason"] == "job_terminal"
    assert row["state"] == "done" and row["participant_status"] is None


@pytest.mark.parametrize("action", ["send", "settings", "queue"])
async def test_native_activity_during_presence_refresh_refuses_stale_idle_control(
    client, daemon, terminal_provider, monkeypatch, action
):
    parent, child, state, presence = await _pair(daemon, terminal_provider, monkeypatch)
    if action == "queue":
        state.native_turn_id = "busy-original"
        await client.call(
            "participant.queue_followup", target=child.id, caller_id=parent.id, prompt="later"
        )
        state.native_turn_id = None
    calls = 0
    original = presence.require_absent

    async def refreshed(participant_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            state.native_turn_id = "human-started"
            await asyncio.sleep(0)
        await original(participant_id)

    monkeypatch.setattr(presence, "require_absent", refreshed)
    if action == "queue":
        assert (await daemon.controls.dispatch_queue(child.id)).deferred
        assert daemon.store.queued_control_operation_count(child.id) == 1
    else:
        method, params = (
            ("send", {"prompt": "must not merge"})
            if action == "send"
            else ("participant.settings.update", {"reasoning_effort": "high"})
        )
        with pytest.raises(RemoteError) as raised:
            await client.call(method, target=child.id, caller_id=parent.id, **params)
        assert raised.value.code == "busy"
        assert daemon.store.running_jobs_for_target(child.id) == []
    assert state.sent == []
    assert state.settings == {}
