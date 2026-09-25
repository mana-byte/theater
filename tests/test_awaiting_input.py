"""Input-request await behaviour through the public coordination entry points."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from presence_fakes import ABSENT, PRESENT, FakePresence

from theater.daemon.awaiting import coordinate_await, parse_targets
from theater.daemon.jobs import JobManager
from theater.models import JobState, Status


class ListeningStore:
    """Expose subscriptions while retaining the store's real status publications."""

    def __init__(self, store):
        self.store = store
        self.listeners = []

    def get_participant(self, participant_id):
        return self.store.get_participant(participant_id)

    def register_bus_listener(self, listener):
        self.listeners.append(listener)
        self.store.register_bus_listener(listener)

    def unregister_bus_listener(self, listener):
        self.store.unregister_bus_listener(listener)
        self.listeners.remove(listener)


@pytest.fixture
def waiting(store, registry):
    participant = registry.create_spawned(harness="vibe", cwd="/tmp")
    jobs = JobManager(store)
    jobs.create(handle=participant.id, caller_id="cli", target_id=participant.id, kind="spawn")
    registry.set_status(participant.id, Status.WORKING)
    daemon = SimpleNamespace(
        store=ListeningStore(store), registry=registry, jobs=jobs, presence=FakePresence()
    )
    return daemon, participant.id


async def test_status_flip_wakes_running_job_and_preserves_wait_any_order(waiting):
    daemon, first_id = waiting
    second = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    job = daemon.jobs.create(handle=second.id, caller_id="cli", target_id=second.id, kind="spawn")
    targets = parse_targets(daemon, [first_id, job.handle])
    blocked = asyncio.Event()
    task = asyncio.create_task(coordinate_await(daemon, targets, max_wait=30, blocked=blocked))
    try:
        await asyncio.wait_for(blocked.wait(), 1)
        assert daemon.store.listeners
        daemon.registry.set_status(second.id, Status.AWAITING_INPUT)
        assert await asyncio.wait_for(task, 1) == {
            first_id: "pending",
            job.handle: "awaiting_input",
        }
        assert daemon.jobs.get(job.handle).state == JobState.RUNNING
        assert daemon.store.listeners == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("depart", [False, True])
async def test_protected_input_request_needs_an_observed_departure(waiting, depart):
    daemon, participant_id = waiting
    daemon.presence.set(participant_id, PRESENT)
    blocked = asyncio.Event()
    task = asyncio.create_task(
        coordinate_await(
            daemon,
            parse_targets(daemon, [participant_id]),
            max_wait=0.2 if not depart else 30,
            blocked=blocked,
        )
    )
    try:
        await asyncio.wait_for(blocked.wait(), 1)
        assert daemon.store.listeners
        daemon.registry.set_status(participant_id, Status.AWAITING_INPUT)
        if depart:
            daemon.presence.set(participant_id, ABSENT)
        reason = "awaiting_input" if depart else "timeout"
        assert await asyncio.wait_for(task, 1) == {participant_id: reason}
        assert daemon.jobs.get(participant_id).state == JobState.RUNNING
        assert daemon.store.listeners == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("protected", [False, True])
async def test_terminal_job_takes_precedence_over_input_request(waiting, protected):
    daemon, participant_id = waiting
    if protected:
        daemon.presence.set(participant_id, PRESENT)
    blocked = asyncio.Event()
    task = asyncio.create_task(
        coordinate_await(
            daemon, parse_targets(daemon, [participant_id]), max_wait=30, blocked=blocked
        )
    )
    try:
        await asyncio.wait_for(blocked.wait(), 1)
        daemon.registry.set_status(participant_id, Status.AWAITING_INPUT)
        daemon.jobs.finish(participant_id, state=JobState.DONE)
        daemon.presence.set(participant_id, ABSENT)
        assert await asyncio.wait_for(task, 1) == {participant_id: "job_terminal"}
        assert daemon.registry.get(participant_id).status == Status.AWAITING_INPUT
        assert daemon.store.listeners == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_resolved_prompt_before_departure_evaluation_does_not_release(waiting):
    daemon, participant_id = waiting
    daemon.registry.set_status(participant_id, Status.AWAITING_INPUT)
    daemon.presence.set(participant_id, PRESENT)
    blocked = asyncio.Event()
    task = asyncio.create_task(
        coordinate_await(
            daemon, parse_targets(daemon, [participant_id]), max_wait=0.2, blocked=blocked
        )
    )
    try:
        await asyncio.wait_for(blocked.wait(), 1)
        daemon.registry.set_status(participant_id, Status.WORKING)
        daemon.presence.set(participant_id, ABSENT)
        assert await asyncio.wait_for(task, 1) == {participant_id: "timeout"}
        assert daemon.store.listeners == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("protected", [False, True])
async def test_participant_without_job_still_waits_only_for_presence(waiting, protected):
    daemon, _participant_id = waiting
    participant = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    daemon.registry.set_status(participant.id, Status.AWAITING_INPUT)
    if protected:
        daemon.presence.set(participant.id, PRESENT)
    blocked = asyncio.Event()
    task = asyncio.create_task(
        coordinate_await(
            daemon, parse_targets(daemon, [participant.id]), max_wait=30, blocked=blocked
        )
    )
    try:
        if protected:
            await asyncio.wait_for(blocked.wait(), 1)
            daemon.presence.set(participant.id, ABSENT)
        reason = "presence_released" if protected else "already_absent"
        assert await asyncio.wait_for(task, 1) == {participant.id: reason}
        assert daemon.jobs.get(participant.id) is None
        assert daemon.store.listeners == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_await_removes_subscriptions(waiting):
    daemon, participant_id = waiting
    blocked = asyncio.Event()
    task = asyncio.create_task(
        coordinate_await(
            daemon, parse_targets(daemon, [participant_id]), max_wait=30, blocked=blocked
        )
    )
    try:
        await asyncio.wait_for(blocked.wait(), 1)
        assert daemon.store.listeners
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert daemon.store.listeners == []


async def test_qualification_does_not_rearm_after_prompt_resolves_during_teardown(
    waiting, monkeypatch
):
    daemon, participant_id = waiting
    armed = asyncio.Event()
    original = daemon.jobs.await_jobs
    calls = 0

    async def await_jobs(handles, *, max_wait):
        nonlocal calls
        calls += 1
        armed.set()
        try:
            return await original(handles, max_wait=max_wait)
        finally:
            daemon.registry.set_status(participant_id, Status.WORKING)

    monkeypatch.setattr(daemon.jobs, "await_jobs", await_jobs)
    task = asyncio.create_task(
        coordinate_await(daemon, parse_targets(daemon, [participant_id]), max_wait=30)
    )
    try:
        await asyncio.wait_for(armed.wait(), 1)
        daemon.registry.set_status(participant_id, Status.AWAITING_INPUT)
        assert await asyncio.wait_for(task, 1) == {participant_id: "awaiting_input"}
        assert daemon.registry.get(participant_id).status == Status.WORKING
        assert calls == 1
        assert daemon.store.listeners == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_status_notification_from_another_thread_wakes_the_await(waiting, store, monkeypatch):
    daemon, participant_id = waiting
    loop_thread = threading.get_ident()
    event_set = asyncio.Event.set

    def set_on_loop(event):
        assert threading.get_ident() == loop_thread
        event_set(event)

    monkeypatch.setattr(asyncio.Event, "set", set_on_loop)
    blocked = asyncio.Event()
    task = asyncio.create_task(
        coordinate_await(
            daemon, parse_targets(daemon, [participant_id]), max_wait=30, blocked=blocked
        )
    )
    try:
        await asyncio.wait_for(blocked.wait(), 1)
        store.set_status(participant_id, Status.AWAITING_INPUT)
        row = {"to_id": participant_id, "kind": "participant.status"}
        for listener in tuple(daemon.store.listeners):
            await asyncio.to_thread(listener, row)
        assert await asyncio.wait_for(task, 1) == {participant_id: "awaiting_input"}
        assert daemon.store.listeners == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
