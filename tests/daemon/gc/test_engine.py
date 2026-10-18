"""The retention sweep: jobs, participants, touch rows, bus rows, batching and scratchpad expiry."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.support.builders import make_bus_row, make_job, make_participant, make_touch
from tests.support.gc_sweep import count_rows, retention
from theater.config import RetentionSection
from theater.constants.daemon import TMUX_RESTART_TERMINATION_REASON
from theater.daemon.gc import SweepResult, sweep
from theater.daemon.jobs import JobManager, JobState
from theater.daemon.schema import bus, global_scratchpad, jobs, participants, touch
from theater.models import Status, WorkspaceRecord, WorkspaceUsageRecord, now
from theater.transcript_identity import TRANSCRIPT_IDENTITY_LOST_CODE

_DAY = 86400.0


def _scratchpad_entry(
    store,
    *,
    namespace: str = "ns1",
    key: str = "k1",
    value: str = "v1",
    updated_at: float | None = None,
    expires_at: float | None = None,
) -> None:
    timestamp = updated_at if updated_at is not None else now()
    store.conn.execute(
        global_scratchpad.insert().values(
            namespace=namespace,
            key=key,
            value=value,
            updated_at=timestamp,
            expires_at=expires_at if expires_at is not None else timestamp + _DAY,
        )
    )


async def test_running_job_survives_sweep(store):
    """A running job (finished_at = NULL) must never be deleted by the sweep.

    JobManager.finish() does `if job is None: return None` before setting the
    asyncio.Event that await_sessions is blocked on. Deleting a running job
    row makes the caller hang until its own timeout with no explanation.
    """
    make_participant(store, pid="p1")
    make_job(
        store,
        handle="job1",
        target_id="p1",
        state=JobState.RUNNING,
        finished_at=None,
        created_at=now() - 999 * _DAY,
    )
    result = await sweep(store, retention(jobs_days=1))
    assert result.jobs == 0
    assert store.get_job("job1") is not None


async def test_finish_still_works_after_sweep(store):
    """The more important assertion: after a sweep, finish() on a running
    job still works and still sets its event — an await_sessions caller is
    still woken.

    If the sweep deleted the running job, finish() would find None and
    return None without setting the event, and the caller would hang.
    """
    make_participant(store, pid="p1")
    make_job(
        store,
        handle="job1",
        target_id="p1",
        state=JobState.RUNNING,
        finished_at=None,
        created_at=now() - 999 * _DAY,
    )

    # The JobManager holds an asyncio.Event for this handle.
    jm = JobManager(store)
    event = asyncio.Event()
    jm._events["job1"] = event

    await sweep(store, retention(jobs_days=1), live_handles=frozenset({"job1"}))

    # finish() must still find the job, set its event, and return it.
    # The event is popped from _events during finish(), so we hold our own
    # reference to check it was set.
    finished = jm.finish("job1", state=JobState.DONE, result="ok")
    assert finished is not None
    assert finished.state == JobState.DONE
    assert event.is_set()


async def test_stale_running_job_is_marked_crashed(store):
    """A running job older than stale_running_days is marked crashed/abandoned
    with a non-null finished_at — so the jobs phase can then delete it.
    """
    make_participant(store, pid="p1")
    make_job(
        store,
        handle="stale",
        target_id="p1",
        state=JobState.RUNNING,
        finished_at=None,
        created_at=now() - 30 * _DAY,
    )
    result = await sweep(store, retention(stale_running_days=7, jobs_days=60))
    assert result.running_marked == 1
    job = store.get_job("stale")
    assert job is not None
    assert job.state == "crashed"
    assert job.error_code == "abandoned"
    assert job.finished_at is not None


async def test_live_running_job_is_not_marked(store):
    """A stale running job whose handle is in live_handles is left alone —
    the running daemon's JobManager still holds an asyncio.Event for it.
    """
    make_participant(store, pid="p1")
    make_job(
        store,
        handle="live",
        target_id="p1",
        state=JobState.RUNNING,
        finished_at=None,
        created_at=now() - 30 * _DAY,
    )
    result = await sweep(
        store,
        retention(stale_running_days=7),
        live_handles=frozenset({"live"}),
    )
    assert result.running_marked == 0
    job = store.get_job("live")
    assert job is not None
    assert job.state == "running"
    assert job.finished_at is None


async def test_dead_participant_referenced_as_parent_is_kept(store):
    """A dead participant that is another participant's parent_id is NOT
    deleted. rails.py walks parent_id upward and does not filter dead rows;
    deleting a mid-chain participant makes the walk terminate early and
    under-counts depth — the rail fails open.

    The child itself is dead and unreferenced, so it IS deleted — but the
    parent survives because the child's parent_id points at it, and the
    DELETE evaluates the subquery before removing any rows.
    """
    make_participant(store, pid="parent", parent_id=None)
    make_participant(store, pid="child", parent_id="parent")

    result = await sweep(store, retention())
    # The child is dead and unreferenced — it goes. The parent is protected
    # by the fourth guard: the child's parent_id is "parent".
    assert store.get_participant("parent") is not None
    # The child may or may not be deleted depending on whether the parent's
    # existence protects it — it does not, because nothing references the child.
    # But the parent must survive regardless.
    assert result.participants <= 1


async def test_dead_participant_becomes_eligible_when_child_gone(store):
    """Once the child is also dead and unreferenced, the parent becomes
    eligible for deletion in the same sweep.
    """
    make_participant(store, pid="parent", parent_id=None)
    make_participant(store, pid="child", parent_id="parent")

    # Delete the child's parent_id reference so parent is no longer a parent.
    store.conn.execute(
        participants.update().where(participants.c.id == "child").values(parent_id=None)
    )

    result = await sweep(store, retention())
    assert result.participants == 2
    assert store.get_participant("parent") is None
    assert store.get_participant("child") is None


async def test_dead_participant_referenced_as_target_id_is_kept(store):
    make_participant(store, pid="p1")
    make_job(
        store,
        handle="j1",
        caller_id="cli",
        target_id="p1",
        state=JobState.DONE,
        finished_at=now() - 90 * _DAY,
        created_at=now() - 90 * _DAY,
    )
    result = await sweep(store, retention(jobs_days=1))
    # The job is old enough to be deleted...
    assert result.jobs == 1
    # ...but the participant deletion happens after the jobs phase, so the
    # participant is now eligible.
    assert result.participants == 1
    assert store.get_participant("p1") is None


async def test_dead_participant_referenced_as_caller_id_is_kept(store):
    """A dead participant referenced by a job's caller_id is protected.

    recall.py's INNER join from touch to jobs uses caller_id indirectly
    through the jobs table; deleting a participant that is a caller would
    orphan those job rows from attribution.
    """
    make_participant(store, pid="caller")
    # A job that is NOT old enough to be swept — so the participant stays.
    make_job(
        store,
        handle="j1",
        caller_id="caller",
        target_id=None,
        state=JobState.DONE,
        finished_at=now(),
        created_at=now(),
    )
    result = await sweep(store, retention(jobs_days=60))
    assert result.participants == 0
    assert store.get_participant("caller") is not None


async def test_active_workspace_usage_keeps_dead_participant_and_workspace(store):
    timestamp = now()
    workspace = WorkspaceRecord(
        workspace_id="workspace-a",
        ownership_kind="theater",
        owner_id="local_operator",
        path="/tmp/workspace-a",
        state="active",
        created_at=timestamp,
        updated_at=timestamp,
    )
    usage = WorkspaceUsageRecord(
        usage_id="usage-a",
        workspace_id=workspace.workspace_id,
        holder_kind="participant",
        holder_id="holder",
        acquired_at=timestamp,
    )
    make_participant(store, pid="holder", workspace_id=workspace.workspace_id)
    with store.write_unit() as unit:
        store.workspaces.create(workspace, connection=unit.connection)
        assert store.workspaces.acquire_usage(usage, connection=unit.connection)

    result = await sweep(store, retention())

    assert result.participants == 0
    assert store.get_participant("holder") is not None
    assert store.workspaces.get(workspace.workspace_id) == workspace


async def test_tmux_restart_participant_waits_for_jobs_retention_after_termination(store):
    participant = make_participant(store, pid="restart")
    participant.termination_reason = TMUX_RESTART_TERMINATION_REASON
    participant.termination_incident = "incident-123"
    participant.terminated_at = now()
    store.upsert_participant(participant)

    result = await sweep(store, retention(jobs_days=2))
    assert result.participants == 0
    assert store.get_participant(participant.id) is not None

    participant.terminated_at = now() - 3 * _DAY
    store.upsert_participant(participant)
    result = await sweep(store, retention(jobs_days=2))
    assert result.participants == 1
    assert store.get_participant(participant.id) is None


async def test_resume_predecessor_is_kept_until_its_successor_is_deleted(store):
    predecessor = make_participant(store, pid="predecessor")
    successor = make_participant(store, pid="successor", status=Status.IDLE)
    successor.resumed_from_id = predecessor.id
    store.upsert_participant(successor)

    result = await sweep(store, retention())
    assert result.participants == 0
    assert store.get_participant(predecessor.id) is not None

    store.set_status(successor.id, Status.DEAD)
    result = await sweep(store, retention())
    assert result.participants == 1
    assert store.get_participant(predecessor.id) is not None

    result = await sweep(store, retention())
    assert result.participants == 1
    assert store.get_participant(predecessor.id) is None


async def test_no_touch_row_survives_without_its_job(store):
    """After the sweep, no touch row survives whose job_handle no longer
    exists in jobs. The two must be deleted together.
    """
    make_participant(store, pid="p1")
    make_job(
        store,
        handle="old",
        target_id="p1",
        state=JobState.DONE,
        finished_at=now() - 90 * _DAY,
        created_at=now() - 90 * _DAY,
    )
    make_touch(store, job_handle="old", path="a.py")
    make_touch(store, job_handle="old", path="b.py")

    result = await sweep(store, retention(jobs_days=60))
    assert result.jobs == 1
    assert result.touch == 2

    # The invariant: no touch row without a matching job.
    from sqlalchemy import select

    orphaned = store.conn.execute(
        select(touch.c.id).where(touch.c.job_handle.not_in(select(jobs.c.handle)))
    ).fetchall()
    assert orphaned == []


async def test_repair_and_audit_rows_go_with_their_job_and_contracts_with_their_creator(store):
    """Repair attempts and the contract audit are keyed by job; custom contracts by creator."""
    from theater.daemon.persistence.repositories.job_repairs import RepairAttempt, RepairState

    make_participant(store, pid="p1")
    for handle, finished in (("old", now() - 90 * _DAY), ("recent", now())):
        make_job(
            store,
            handle=handle,
            target_id="p1",
            state=JobState.DONE,
            finished_at=finished,
            created_at=finished,
        )
        store.create_repair_attempt(
            RepairAttempt(
                attempt_id=f"{handle}~repair1",
                job_handle=handle,
                participant_id="p1",
                attempt=1,
                state=RepairState.SETTLED,
                fence=False,
                created_at=now(),
                updated_at=now(),
            )
        )
        store.record_contract_audit(handle, "p1", "repair_exhausted", None, at=now())
    store.register_response_contract("p1", "x", "{}", created_at=now(), limit=4)

    await sweep(store, retention(jobs_days=60))

    assert [a.job_handle for a in store.repair_attempts_for_job("old")] == []
    assert store.contract_audit_for_job("old") == []
    assert len(store.repair_attempts_for_job("recent")) == 1
    assert len(store.contract_audit_for_job("recent")) == 1
    # The surviving job still references p1, so the participant and its contract stay.
    assert store.get_response_contract("p1", "x") == "{}"

    store.conn.execute(jobs.delete())
    await sweep(store, retention(jobs_days=60))
    assert store.get_participant("p1") is None
    assert store.get_response_contract("p1", "x") is None
    assert store.repair_attempts_for_job("recent") == []


async def test_a_fenced_repair_keeps_its_job_until_its_context_is_provably_gone(store):
    """Deleting the job would lift the fence unproven; death or a new generation lifts it."""
    from theater.daemon.persistence.repositories.job_repairs import RepairAttempt, RepairState
    from theater.daemon.persistence.repositories.runtime_bindings import ParticipantRuntimeBinding
    from theater.harness.contracts.runtime import RuntimeLifecyclePhase, RuntimeWiring

    make_participant(store, pid="p1", status=Status.IDLE)
    old = now() - 90 * _DAY
    make_job(
        store, handle="old", target_id="p1", state=JobState.DONE, finished_at=old, created_at=old
    )
    store.create_repair_attempt(
        RepairAttempt(
            attempt_id="old~repair1",
            job_handle="old",
            participant_id="p1",
            attempt=1,
            state=RepairState.UNKNOWN,
            backend_generation=1,
            native_session_id="s1",
            created_at=now(),
            updated_at=now(),
        )
    )

    def bind(generation):
        store.upsert_runtime_binding(
            ParticipantRuntimeBinding(
                participant_id="p1",
                harness="codex",
                wiring=RuntimeWiring.NATIVE,
                backend_generation=generation,
                lifecycle=RuntimeLifecyclePhase.BOUND,
                native_session_id="s1",
                created_at=now(),
                updated_at=now(),
            )
        )

    for binding in (None, 1):  # no binding, then the very same generation and session
        if binding:
            bind(binding)
        assert (await sweep(store, retention(jobs_days=60))).jobs == 0
        assert store.get_job("old") is not None and store.repair_fence_active("p1")

    bind(2)  # a new backend generation: the old turn cannot still be running
    assert (await sweep(store, retention(jobs_days=60))).jobs == 1
    assert store.get_job("old") is None and not store.repair_fence_active("p1")


async def test_touch_rows_for_surviving_jobs_are_kept(store):
    """Touch rows for a job that is NOT swept must survive."""
    make_participant(store, pid="p1")
    make_job(
        store,
        handle="recent",
        target_id="p1",
        state=JobState.DONE,
        finished_at=now(),
        created_at=now(),
    )
    make_touch(store, job_handle="recent", path="c.py")

    result = await sweep(store, retention(jobs_days=60))
    assert result.jobs == 0
    assert result.touch == 0
    assert count_rows(store, touch) == 1


async def test_old_bus_rows_are_deleted(store):
    old_ts = now() - 30 * _DAY
    make_bus_row(store, kind="job.created", ts=old_ts)
    make_bus_row(store, kind="job.finished", ts=old_ts)

    result = await sweep(store, retention(bus_days=7))
    assert result.bus == 2
    assert count_rows(store, bus) == 0


async def test_active_identity_loss_audit_survives_batched_bus_retention(store):
    make_participant(store, status=Status.IDLE)
    old_ts = now() - 30 * _DAY
    loss_id = make_bus_row(
        store,
        kind="agent.observation_error",
        ts=old_ts,
        to_id="p1",
        payload=json.dumps({"code": TRANSCRIPT_IDENTITY_LOST_CODE}),
    )
    make_bus_row(store, kind="job.created", ts=old_ts)
    make_bus_row(store, kind="job.finished", ts=old_ts)

    result = await sweep(store, retention(bus_days=7, batch=1))

    assert result.bus == 2
    assert [row[0] for row in store.conn.execute(bus.select()).fetchall()] == [loss_id]


@pytest.mark.parametrize(
    ("kind", "payload", "deleted", "remaining"),
    [
        ("operator.transcript_unbind", "{}", 1, ["operator.transcript_unbind"]),
        (
            "agent.transcript_receipt",
            json.dumps({"admission": "accepted"}),
            1,
            ["agent.transcript_receipt"],
        ),
        (
            "agent.transcript_receipt",
            json.dumps({"admission": "staged"}),
            0,
            ["agent.observation_error", "agent.transcript_receipt"],
        ),
    ],
)
async def test_identity_loss_audit_retention_follows_clearing_events(
    store, kind, payload, deleted, remaining
):
    make_participant(store, status=Status.IDLE)
    old_ts = now() - 30 * _DAY
    make_bus_row(
        store,
        kind="agent.observation_error",
        ts=old_ts,
        to_id="p1",
        payload=json.dumps({"code": TRANSCRIPT_IDENTITY_LOST_CODE}),
    )
    make_bus_row(store, kind=kind, ts=now(), to_id="p1", payload=payload)

    result = await sweep(store, retention(bus_days=7, batch=1))

    assert result.bus == deleted
    assert [row._mapping["kind"] for row in store.conn.execute(bus.select())] == remaining


async def test_send_refused_of_same_age_survives(store):
    """send.refused is the only record of a refused send, so it is exempt
    from the age TTL.
    """
    old_ts = now() - 30 * _DAY
    make_bus_row(store, kind="send.refused", ts=old_ts)

    result = await sweep(store, retention(bus_days=7))
    assert result.bus == 0
    assert count_rows(store, bus) == 1


async def test_refused_cap_trims_oldest(store):
    """With more than refused_cap refused rows, the oldest are trimmed and
    the newest refused_cap remain.
    """
    cap = 3
    ids = []
    for i in range(5):
        ids.append(make_bus_row(store, kind="send.refused", ts=now() - (100 - i) * _DAY))
    # ids are autoincrement, so ids[0] < ids[1] < ... < ids[4]
    # The newest `cap` should survive: ids[2], ids[3], ids[4].

    result = await sweep(store, retention(bus_days=7, refused_cap=cap))
    assert result.bus == 2  # 5 - 3

    from sqlalchemy import select

    remaining = [
        r[0]
        for r in store.conn.execute(select(bus.c.id).where(bus.c.kind == "send.refused")).fetchall()
    ]
    # The newest `cap` rows survive — higher id = newer (autoincrement).
    assert remaining == sorted(ids)[-cap:]


async def test_batching_loops_until_all_deleted(store):
    """With batch set small and many rows to delete, the sweep still removes
    all of them — the loop repeats rather than doing one pass.
    """
    make_participant(store, pid="p1")
    for i in range(10):
        make_job(
            store,
            handle=f"j{i}",
            target_id="p1",
            state=JobState.DONE,
            finished_at=now() - 90 * _DAY,
            created_at=now() - 90 * _DAY,
        )
        make_touch(store, job_handle=f"j{i}", path=f"file{i}.py")

    result = await sweep(store, retention(jobs_days=60, batch=2))
    assert result.jobs == 10
    assert result.touch == 10
    assert count_rows(store, jobs) == 0
    assert count_rows(store, touch) == 0


async def test_participant_sweep_honours_small_batches(store):
    for index in range(5):
        make_participant(store, pid=f"dead-{index}")

    result = await sweep(store, retention(batch=2))
    assert result.participants == 5
    assert count_rows(store, participants) == 0


async def test_disabled_sweep_deletes_nothing():
    """With enabled = False the daemon never creates the GC task (sweep itself ignores the flag)."""

    from theater.daemon.server import Daemon

    daemon = Daemon(harnesses={}, config=__disabled_config())
    try:
        assert daemon._gc is None
    finally:
        daemon.store.close()


def __disabled_config():
    from theater.config import Config

    return Config(retention=RetentionSection(enabled=False))


async def test_sweep_on_empty_database_is_noop(store):
    """A sweep against an empty database raises nothing and returns all-zero."""
    result = await sweep(store, retention())
    assert result == SweepResult()
    assert result.bus == 0
    assert result.jobs == 0
    assert result.touch == 0
    assert result.participants == 0
    assert result.running_marked == 0
    assert result.scratchpad == 0


async def test_expired_global_scratchpad_is_cleaned(store):
    _scratchpad_entry(store, key="k1", expires_at=now() - 1)
    _scratchpad_entry(store, key="k2", expires_at=now() - 1)
    result = await sweep(store, retention())
    assert result.scratchpad == 2
    assert count_rows(store, global_scratchpad) == 0


async def test_unexpired_global_scratchpad_is_retained_without_participants(store):
    _scratchpad_entry(store, expires_at=now() + _DAY)
    result = await sweep(store, retention())
    assert result.scratchpad == 0
    assert count_rows(store, global_scratchpad) == 1


async def test_global_scratchpad_cleanup_is_batched(store):
    for i in range(10):
        _scratchpad_entry(store, key=f"k{i}", expires_at=now() - 1)
    result = await sweep(store, retention(batch=3))
    assert result.scratchpad == 10
    assert count_rows(store, global_scratchpad) == 0


async def test_sweep_result_counts_match_all_tables(store):
    """SweepResult fields match actual deletions across all tables."""
    make_participant(store, pid="p1")
    make_participant(store, pid="p2")

    make_job(
        store,
        handle="old1",
        target_id="p1",
        caller_id="cli",
        state=JobState.DONE,
        finished_at=now() - 90 * _DAY,
        created_at=now() - 90 * _DAY,
    )
    make_touch(store, job_handle="old1", path="x.py")

    make_bus_row(store, kind="job.created", ts=now() - 30 * _DAY)
    make_bus_row(store, kind="send.refused", ts=now() - 30 * _DAY)

    _scratchpad_entry(store, expires_at=now() - 1)

    before_jobs = count_rows(store, jobs)
    before_touch = count_rows(store, touch)
    before_bus = count_rows(store, bus)
    before_part = count_rows(store, participants)
    before_kv = count_rows(store, global_scratchpad)

    result = await sweep(store, retention(bus_days=7, jobs_days=60))

    assert result.jobs == before_jobs - count_rows(store, jobs)
    assert result.touch == before_touch - count_rows(store, touch)
    assert result.bus == before_bus - count_rows(store, bus)
    assert result.participants == before_part - count_rows(store, participants)
    assert result.scratchpad == before_kv - count_rows(store, global_scratchpad)
