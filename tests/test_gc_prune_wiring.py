"""GC-1 and GC-2 regression wiring: NULL-safe reference guards and pruned dependents.

Covers the two sweep defects the retention config could hide: a NULL ``caller_id``
poisoning ``NOT IN`` (no dead participant was ever collected), and dangling
terminal/runtime/control rows surviving the participant that owned them.
"""

from __future__ import annotations

from sqlalchemy import func, select, update

from theater.config import RetentionSection
from theater.daemon.gc import sweep
from theater.daemon.schema import (
    control_operations,
    jobs,
    native_terminal_evidence,
    participant_runtime_bindings,
    participants,
    terminal_bindings,
)
from theater.models import Job, Participant, Status, Tier, now

_DAY = 86400.0


def _retention(**overrides) -> RetentionSection:
    defaults = {
        "bus_days": 7,
        "events_days": 7,
        "jobs_days": 15,
        "refused_cap": 10000,
        "stale_running_days": 7,
        "batch": 5000,
        "interval": 3600.0,
        "enabled": True,
    }
    defaults.update(overrides)
    return RetentionSection(**defaults)


def _participant(
    store, *, pid: str, parent_id: str | None = None, status: Status = Status.DEAD
) -> None:
    store.upsert_participant(
        Participant(
            id=pid,
            harness="vibe",
            tier=Tier.SPAWNED,
            cwd="/tmp",
            parent_id=parent_id,
            status=status,
        )
    )


def _finished_job(store, *, handle: str, caller_id: str | None, target_id: str, age_days: float):
    store.create_job(
        Job(
            handle=handle,
            caller_id="cli" if caller_id is None else caller_id,
            target_id=target_id,
            kind="spawn",
            prompt=None,
            state="done",
            result="ok",
            error_code=None,
            created_at=now() - age_days * _DAY,
            finished_at=now() - age_days * _DAY,
        )
    )
    if caller_id is None:
        # The daemon records frontend root spawns with a NULL caller (admission.py).
        store.conn.execute(update(jobs).values(caller_id=None).where(jobs.c.handle == handle))


def _op(
    store,
    *,
    operation_id: str,
    participant_id: str,
    phase: str = "settled",
    barrier: int = 0,
    kind: str = "send",
    job_handle: str | None = None,
    age_days: float = 30,
) -> None:
    timestamp = now() - age_days * _DAY
    store.conn.execute(
        control_operations.insert().values(
            operation_id=operation_id,
            participant_id=participant_id,
            job_handle=job_handle,
            kind=kind,
            transport="legacy_tmux",
            delivery_phase=phase,
            execution_barrier=barrier,
            created_at=timestamp,
            updated_at=timestamp,
        )
    )


def _evidence(store, *, session_id: str, participant_id: str = "p1", age_days: float = 30) -> None:
    store.conn.execute(
        native_terminal_evidence.insert().values(
            participant_id=participant_id,
            backend_generation=1,
            native_session_id=session_id,
            native_turn_id=f"turn-{session_id}",
            terminal="completed",
            result_completeness="complete",
            result_provenance="native_evidence",
            recorded_at=now() - age_days * _DAY,
        )
    )


def _binding(store, *, participant_id: str, lifecycle: str) -> None:
    timestamp = now()
    store.conn.execute(
        participant_runtime_bindings.insert().values(
            participant_id=participant_id,
            harness="vibe",
            wiring="native",
            backend_generation=1,
            lifecycle_phase=lifecycle,
            created_at=timestamp,
            updated_at=timestamp,
        )
    )
    store.conn.execute(
        terminal_bindings.insert().values(
            participant_id=participant_id,
            provider_id="tmux",
            provider_generation=1,
            terminal_id=f"t-{participant_id}",
            terminal_incarnation="inc1",
            occupant_evidence="verified",
            health="healthy",
            report_revision=1,
            created_at=timestamp,
            updated_at=timestamp,
        )
    )


def _count(store, table) -> int:
    return store.conn.execute(select(func.count()).select_from(table)).scalar()


def _op_ids(store) -> set[str]:
    return set(store.conn.execute(select(control_operations.c.operation_id)).scalars())


async def test_null_caller_job_does_not_block_dead_participant_sweep(store):
    """GC-1: NULL ``caller_id`` made ``id NOT IN (SELECT caller_id)`` UNKNOWN for every row."""
    _participant(store, pid="gone")
    _finished_job(store, handle="h-null", caller_id=None, target_id="other", age_days=2)
    _participant(store, pid="kept-target")
    _finished_job(store, handle="h-target", caller_id="cli", target_id="kept-target", age_days=2)
    _participant(store, pid="kept-caller")
    _finished_job(store, handle="h-caller", caller_id="kept-caller", target_id="other", age_days=2)
    _participant(store, pid="kept-parent")
    _participant(store, pid="child", parent_id="kept-parent", status=Status.IDLE)
    _participant(store, pid="kept-resumed")
    store.conn.execute(
        update(participants)
        .values(resumed_from_id="kept-resumed")
        .where(participants.c.id == "child")
    )

    result = await sweep(store, _retention())

    assert result.participants == 1
    assert store.get_participant("gone") is None
    for pid in ("kept-target", "kept-caller", "kept-parent", "kept-resumed"):
        assert store.get_participant(pid) is not None


async def test_participant_deletion_drops_dependents_and_fences_unsettled_owners(store):
    """A deleted participant leaves no dangling bindings/ops/evidence; unsafe owners are kept."""
    _participant(store, pid="deps")
    _binding(store, participant_id="deps", lifecycle="stopped")
    _op(store, operation_id="op-settled", participant_id="deps", age_days=1)
    _evidence(store, session_id="s-deps", participant_id="deps", age_days=1)
    _participant(store, pid="unsettled")
    _op(store, operation_id="op-queued", participant_id="unsettled", phase="queued", age_days=30)
    _participant(store, pid="live-backend")
    _binding(store, participant_id="live-backend", lifecycle="active")

    result = await sweep(store, _retention())

    assert result.participants == 1
    assert store.get_participant("deps") is None
    assert store.get_participant("unsettled") is not None
    assert store.get_participant("live-backend") is not None
    assert _count(store, terminal_bindings) == 1  # live-backend's
    assert _count(store, participant_runtime_bindings) == 1  # live-backend's
    assert _op_ids(store) == {"op-queued"}
    assert _count(store, native_terminal_evidence) == 0


async def test_control_operation_and_evidence_prunes_delete_only_old_settled_rows(store):
    """Wired prune phases age- and state-fence; small batches still drain every eligible row."""
    for index in range(5):
        _op(store, operation_id=f"op-old-{index}", participant_id="p1", age_days=30)
    _op(store, operation_id="op-recent", participant_id="p1", age_days=1)
    _op(store, operation_id="op-queued", participant_id="p1", phase="queued", age_days=30)
    _op(store, operation_id="op-barrier", participant_id="p1", barrier=1, age_days=30)
    store.create_job(
        Job(
            handle="live-job",
            caller_id="cli",
            target_id="p1",
            kind="spawn",
            prompt=None,
            state="running",
            result=None,
            error_code=None,
            created_at=now(),
            finished_at=None,
        )
    )
    _op(
        store,
        operation_id="op-running-job",
        participant_id="p1",
        job_handle="live-job",
        age_days=30,
    )
    _evidence(store, session_id="s-old", participant_id="p1", age_days=30)
    _evidence(store, session_id="s-recent", participant_id="p1", age_days=1)

    result = await sweep(store, _retention(batch=2))

    assert result.control_operations == 5
    assert result.native_evidence == 1
    assert _op_ids(store) == {"op-recent", "op-queued", "op-barrier", "op-running-job"}
    remaining_evidence = set(
        store.conn.execute(select(native_terminal_evidence.c.native_session_id)).scalars()
    )
    assert remaining_evidence == {"s-recent"}
    # Idempotent on rerun: nothing more to collect.
    again = await sweep(store, _retention(batch=2))
    assert again.control_operations == 0
    assert again.native_evidence == 0
