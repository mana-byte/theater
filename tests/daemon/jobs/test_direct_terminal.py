"""Direct terminal writes keep their stored fields and wake-only contract."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import delete

from theater.daemon.jobs import JobManager
from theater.daemon.schema import jobs as jobs_table
from theater.models import Job, JobState, JournalEventRecord


def test_direct_finish_rolls_back_then_wakes_without_listeners_or_bus(store) -> None:
    initial = Job(
        handle="direct-job",
        caller_id="cli",
        target_id="participant-a",
        kind="send",
        prompt="work",
        state=JobState.RUNNING.value,
        result="prior result",
        error_code=None,
        created_at=1.0,
        finished_at=None,
        response_format="json",
        structured_result='{"answer":"old"}',
        structured_status="parsed",
        actor_client_id="client-a",
        actor_participant_id="participant-b",
    )
    store.create_job(initial)
    jobs = JobManager(store)
    jobs.track(initial.handle)
    wake = jobs._events[initial.handle]
    assert jobs.replace_touch_accumulator(initial.handle, cwd="/tmp")
    listeners: list[str] = []
    jobs.add_terminal_listener(listeners.append)
    bus_before = store.bus_tail()
    cursor = store.journal.current_sequence()

    def write(unit) -> tuple[Job, JournalEventRecord]:
        outcome = jobs.write_direct_terminal_in_unit(
            store,
            unit,
            job=initial,
            state=JobState.CRASHED,
            result=initial.result,
            error_code="provider_busy",
            finished_at=8.0,
        )
        assert outcome is not None
        finished, event = outcome
        assert event.entity_revision == 0 and event.recorded_at == 8.0
        store.journal.append_group(
            unit,
            [
                replace(
                    event,
                    entity_revision=store.journal.current_sequence(connection=unit.connection) + 1,
                )
            ],
        )
        unit.after_commit(lambda: jobs.wake_committed_direct_finish(initial.handle))
        return finished, event

    with pytest.raises(RuntimeError, match="abort"), store.write_unit() as unit:
        write(unit)
        raise RuntimeError("abort")
    assert store.get_job(initial.handle) == initial
    assert store.journal.current_sequence() == cursor
    assert not wake.is_set() and initial.handle in jobs._events
    assert initial.handle in jobs._accumulators
    assert listeners == [] and store.bus_tail() == bus_before

    with store.write_unit() as unit:
        finished, event = write(unit)
    stored = store.get_job(initial.handle)
    assert stored == finished
    assert stored.state == JobState.CRASHED and stored.result == "prior result"
    assert (stored.response_format, stored.structured_result, stored.structured_status) == (
        initial.response_format,
        initial.structured_result,
        initial.structured_status,
    )
    assert (stored.actor_client_id, stored.actor_participant_id) == (
        initial.actor_client_id,
        initial.actor_participant_id,
    )
    assert event.payload["structured_status"] == "parsed"
    assert event.payload["actor"] == {
        "client_id": "client-a",
        "participant_id": "participant-b",
    }
    assert store.journal.current_sequence() == cursor + 1
    assert wake.is_set() and initial.handle not in jobs._events
    assert initial.handle not in jobs._accumulators
    assert listeners == [] and store.bus_tail() == bus_before
    with store.write_unit() as unit:
        assert (
            jobs.write_direct_terminal_in_unit(
                store,
                unit,
                job=initial,
                state=JobState.KILLED,
                result=None,
                error_code="stale",
                finished_at=9.0,
            )
            is None
        )
    assert store.journal.current_sequence() == cursor + 1


def test_direct_finish_skips_wake_when_earlier_callback_deletes_row(store) -> None:
    initial = Job(
        handle="deleted-before-wake",
        caller_id="cli",
        target_id="participant-a",
        kind="send",
        prompt=None,
        state=JobState.RUNNING.value,
        result=None,
        error_code=None,
        created_at=1.0,
        finished_at=None,
    )
    store.create_job(initial)
    jobs = JobManager(store)
    jobs.track(initial.handle)
    wake = jobs._events[initial.handle]
    assert jobs.replace_touch_accumulator(initial.handle, cwd="/tmp")

    def delete_row() -> None:
        with store.write_unit() as unit:
            unit.connection.execute(delete(jobs_table).where(jobs_table.c.handle == initial.handle))

    with store.write_unit() as unit:
        assert (
            jobs.write_direct_terminal_in_unit(
                store,
                unit,
                job=initial,
                state=JobState.KILLED,
                result=None,
                error_code="cancelled",
                finished_at=8.0,
            )
            is not None
        )
        unit.after_commit(delete_row)
        unit.after_commit(lambda: jobs.wake_committed_direct_finish(initial.handle))

    assert store.get_job(initial.handle) is None
    assert not wake.is_set() and jobs._events[initial.handle] is wake
    assert initial.handle in jobs._accumulators
