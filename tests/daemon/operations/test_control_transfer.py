"""Atomic control transfer: lineage, queue cancellation, cycle rejection and rollback."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import update

from tests.support.provider_controls import queued_followup
from theater.daemon.control_ownership import OwnershipConflict
from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.frontend.participant_mutation_handlers import participants_transfer_control
from theater.daemon.runtime.recovery import reconcile_public_control_operations
from theater.daemon.schema import jobs as jobs_table
from theater.frontend.capabilities import METHOD_CATALOG, ConnectionChannel, ConnectionRole
from theater.frontend.schemas import validator_for
from theater.harness.contracts.runtime import ControlDeliveryPhase
from theater.models import JobState, PublicOperationRecord, now


def _context() -> ConnectionContext:
    return ConnectionContext(
        client_id="operator-a",
        role=ConnectionRole.OPERATOR,
        channel=ConnectionChannel.RPC,
        api_major=1,
        api_minor=0,
        capabilities=frozenset(),
    )


async def test_transfer_is_atomic_preserves_lineage_and_cancels_only_queued(daemon) -> None:
    owner = daemon.registry.create_spawned(harness="codex", cwd="/tmp", has_prompt=False)
    target = daemon.registry.create_spawned(
        harness="codex",
        cwd="/tmp",
        parent_id=owner.id,
        resumed_from_id="historic-predecessor",
        has_prompt=False,
    )
    successor = daemon.registry.create_spawned(harness="codex", cwd="/tmp", has_prompt=False)
    queued_id, queued_job = queued_followup(daemon, target.id, "1")
    dispatched_id, dispatched_job = queued_followup(daemon, target.id, "2", dispatched=True)
    with daemon.store.write_unit() as unit:
        unit.connection.execute(
            update(jobs_table)
            .where(jobs_table.c.handle == queued_job)
            .values(
                response_format="json",
                structured_result='{"answer":"kept"}',
                structured_status="parsed",
            )
        )
    timestamp = now()
    with daemon.store.write_unit() as unit:
        daemon.store.operations.create(
            PublicOperationRecord(
                operation_id="public-1",
                kind="controls.queue_followup",
                actor_client_id="original-client",
                actor_participant_id=None,
                target_ids=(target.id,),
                state="running",
                phase="control_reserved",
                control_operation_id=queued_id,
                job_handle=queued_job,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
    await reconcile_public_control_operations(daemon)
    wake = daemon.jobs._events[queued_job]
    listeners: list[str] = []
    daemon.jobs.add_terminal_listener(listeners.append)
    finished_bus_before = [row for row in daemon.store.bus_tail() if row["kind"] == "job.finished"]

    result = await participants_transfer_control(
        daemon,
        _context(),
        {
            "participants": [
                {"participant_id": target.id, "expected_revision": target.control_revision}
            ],
            "new_owner": {"kind": "participant", "participant_id": successor.id},
        },
        idempotency_key="transfer-a",
    )
    validator_for(
        METHOD_CATALOG["frontend.participants.transfer_control"].result_schema_id
    ).validate(result)
    await asyncio.gather(*daemon.operation_service.owned_tasks)

    changed = daemon.registry.get(target.id)
    assert changed.parent_id == owner.id
    assert changed.resumed_from_id == "historic-predecessor"
    assert changed.control_owner_id == successor.id
    assert changed.control_revision == 1
    assert result["cancelled_job_handles"] == [queued_job]
    assert daemon.store.get_control_operation(queued_id).error_code == "control_transferred"
    cancelled_job = daemon.store.get_job(queued_job)
    assert cancelled_job.state == JobState.KILLED
    assert (
        cancelled_job.response_format,
        cancelled_job.structured_result,
        cancelled_job.structured_status,
    ) == ("json", '{"answer":"kept"}', "parsed")
    assert cancelled_job.actor_client_id == "original-client"
    assert wake.is_set() and listeners == []
    assert [row for row in daemon.store.bus_tail() if row["kind"] == "job.finished"] == (
        finished_bus_before
    )
    assert daemon.operation_service.get("public-1").state == "failed"
    assert daemon.store.get_control_operation(dispatched_id).delivery_phase is (
        ControlDeliveryPhase.DISPATCHED
    )
    assert daemon.store.get_job(dispatched_job).state == JobState.RUNNING
    assert daemon.store.get_job(dispatched_job).actor_client_id == "original-client"


async def test_transfer_rejects_cycle_and_stale_batch_without_partial_writes(daemon) -> None:
    first = daemon.registry.create_spawned(harness="codex", cwd="/tmp", has_prompt=False)
    second = daemon.registry.create_spawned(
        harness="codex", cwd="/tmp", parent_id=first.id, has_prompt=False
    )
    third = daemon.registry.create_spawned(
        harness="codex", cwd="/tmp", parent_id=second.id, has_prompt=False
    )
    with pytest.raises(OwnershipConflict, match="cycle"):
        await participants_transfer_control(
            daemon,
            _context(),
            {
                "participants": [
                    {"participant_id": first.id, "expected_revision": 0},
                    {"participant_id": second.id, "expected_revision": 0},
                ],
                "new_owner": {"kind": "participant", "participant_id": third.id},
            },
            idempotency_key="cycle",
        )
    assert daemon.registry.get(first.id).control_revision == 0

    with pytest.raises(OwnershipConflict, match="no participant"):
        await participants_transfer_control(
            daemon,
            _context(),
            {
                "participants": [
                    {"participant_id": first.id, "expected_revision": 0},
                    {"participant_id": "missing-participant", "expected_revision": 0},
                ],
                "new_owner": {"kind": "local_operator", "participant_id": None},
            },
            idempotency_key="missing",
        )
    assert daemon.registry.get(first.id).control_revision == 0
    assert daemon.registry.get(second.id).control_revision == 0

    with pytest.raises(OwnershipConflict, match="revision"):
        await participants_transfer_control(
            daemon,
            _context(),
            {
                "participants": [
                    {"participant_id": first.id, "expected_revision": 0},
                    {"participant_id": second.id, "expected_revision": 7},
                ],
                "new_owner": {"kind": "local_operator", "participant_id": None},
            },
            idempotency_key="stale",
        )
    assert daemon.registry.get(first.id).control_revision == 0


def test_resume_lineage_does_not_implicitly_transfer_descendants(daemon) -> None:
    coordinator = daemon.registry.create_spawned(harness="codex", cwd="/tmp", has_prompt=False)
    child = daemon.registry.create_spawned(
        harness="codex",
        cwd="/tmp",
        parent_id=coordinator.id,
        has_prompt=False,
    )
    successor = daemon.registry.create_spawned(
        harness="codex",
        cwd="/tmp",
        resumed_from_id=coordinator.id,
        has_prompt=False,
    )

    unchanged = daemon.registry.get(child.id)
    assert unchanged.control_owner_id == coordinator.id
    assert unchanged.control_revision == 0
    assert successor.resumed_from_id == coordinator.id


async def test_transfer_rolls_back_state_queue_and_journal_together(daemon, monkeypatch) -> None:
    target = daemon.registry.create_spawned(harness="codex", cwd="/tmp", has_prompt=False)
    _operation_id, handle = queued_followup(daemon, target.id, "3")
    before = daemon.store.journal.current_sequence()
    wake = daemon.jobs._events[handle]
    listeners: list[str] = []
    daemon.jobs.add_terminal_listener(listeners.append)
    finished_bus_before = [row for row in daemon.store.bus_tail() if row["kind"] == "job.finished"]

    def fail_append(*_args, **_kwargs):
        raise RuntimeError("crash before commit")

    monkeypatch.setattr(daemon.store.journal, "append_group", fail_append)
    with pytest.raises(RuntimeError, match="crash before commit"):
        await participants_transfer_control(
            daemon,
            _context(),
            {
                "participants": [{"participant_id": target.id, "expected_revision": 0}],
                "new_owner": {"kind": "local_operator", "participant_id": None},
            },
            idempotency_key="rollback",
        )
    assert daemon.registry.get(target.id).control_revision == 0
    assert daemon.store.get_control_operation(_operation_id).delivery_phase is (
        ControlDeliveryPhase.QUEUED
    )
    assert daemon.store.get_job(handle).state == JobState.RUNNING
    assert daemon.store.journal.current_sequence() == before
    assert not wake.is_set() and handle in daemon.jobs._events
    assert listeners == []
    assert [row for row in daemon.store.bus_tail() if row["kind"] == "job.finished"] == (
        finished_bus_before
    )
