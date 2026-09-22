"""Focused RC10 canonical idempotency and atomic-claim tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import func, insert, select

from theater.daemon.operations import (
    IDEMPOTENCY_RETENTION_SECONDS,
    IdempotencyConflict,
    OperationService,
    PreparedOperation,
    request_digest,
)
from theater.daemon.persistence.database import Database
from theater.daemon.persistence.repositories.journal import JournalRepository
from theater.daemon.persistence.repositories.operations import OperationRepository
from theater.daemon.schema import idempotency_records, meta, orchestration_events, public_operations
from theater.models import PublicOperationRecord


class _Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _Store:
    def __init__(self, path: Path) -> None:
        self.db = Database(path)
        self.operations = OperationRepository(self.db)
        self.journal = JournalRepository(self.db)

    def write_unit(self):
        return self.db.write_unit()

    def close(self) -> None:
        self.db.close()


def _prepare(
    clock: _Clock,
    calls: list[str],
    *,
    participant_id: str = "participant-a",
) -> Callable:
    def prepare(operation_id, _unit):
        calls.append(operation_id)
        record = PublicOperationRecord(
            operation_id=operation_id,
            kind="send",
            actor_client_id="client-a",
            actor_participant_id="caller-a",
            target_ids=(participant_id,),
            state="accepted",
            phase="accepted",
            job_handle="job-a",
            created_at=clock.value,
            updated_at=clock.value,
        )
        return PreparedOperation(
            record,
            {
                "operation_id": operation_id,
                "state": "accepted",
                "participant_id": participant_id,
                "job_handle": "job-a",
            },
        )

    return prepare


def test_canonical_claim_replays_and_conflicts_without_side_effects(tmp_path: Path) -> None:
    database_path = tmp_path / "idempotency.db"
    store = _Store(database_path)
    clock = _Clock()
    ids = iter(("operation-a", "operation-b"))
    service = OperationService(store, clock=clock, id_factory=lambda: next(ids))
    calls: list[str] = []
    try:
        params = {"participant_id": "participant-a", "prompt": "Review this."}
        assert request_digest("frontend.controls.send", params) == request_digest(
            "frontend.controls.send", {"prompt": "Review this.", "participant_id": "participant-a"}
        )
        first = service.accept_operation(
            client_id="client-a",
            idempotency_key="send-a",
            method="frontend.controls.send",
            params=params,
            prepare=_prepare(clock, calls),
        )
        replay = service.accept_operation(
            client_id="client-a",
            idempotency_key="send-a",
            method="frontend.controls.send",
            params={"prompt": "Review this.", "participant_id": "participant-a"},
            prepare=_prepare(clock, calls),
        )

        assert not first.replayed
        assert replay.replayed
        assert replay.record.operation_id == first.record.operation_id == "operation-a"
        assert replay.response == first.response
        assert calls == ["operation-a"]

        with pytest.raises(IdempotencyConflict) as raised:
            service.accept_operation(
                client_id="client-a",
                idempotency_key="send-a",
                method="frontend.controls.send",
                params={"participant_id": "participant-a", "prompt": "Different."},
                prepare=_prepare(clock, calls),
            )
        assert raised.value.code == "idempotency_conflict"
        assert calls == ["operation-a"]
        assert (
            store.db.conn.execute(select(func.count()).select_from(public_operations)).scalar() == 1
        )
        assert (
            store.db.conn.execute(select(func.count()).select_from(orchestration_events)).scalar()
            == 1
        )
    finally:
        store.close()

    reopened = _Store(database_path)
    try:
        durable = OperationService(reopened, clock=clock, id_factory=lambda: "operation-unused")
        replay = durable.accept_operation(
            client_id="client-a",
            idempotency_key="send-a",
            method="frontend.controls.send",
            params={"participant_id": "participant-a", "prompt": "Review this."},
            prepare=_prepare(clock, calls),
        )
        assert replay.replayed
        assert replay.record.operation_id == "operation-a"
        assert calls == ["operation-a"]
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_concurrent_first_claims_converge_on_one_operation(tmp_path: Path) -> None:
    store = _Store(tmp_path / "converge.db")
    clock = _Clock()
    ids = iter(("operation-a", "operation-b"))
    service = OperationService(store, clock=clock, id_factory=lambda: next(ids))
    calls: list[str] = []

    async def submit():
        await asyncio.sleep(0)
        return service.accept_operation(
            client_id="client-a",
            idempotency_key="send-a",
            method="frontend.controls.send",
            params={"participant_id": "participant-a", "prompt": "Review this."},
            prepare=_prepare(clock, calls),
        )

    try:
        left, right = await asyncio.gather(submit(), submit())
        assert left.record.operation_id == right.record.operation_id == "operation-a"
        assert {left.replayed, right.replayed} == {False, True}
        assert calls == ["operation-a"]
    finally:
        store.close()


def test_failed_admission_claim_rolls_back_and_completed_results_retain(tmp_path: Path) -> None:
    store = _Store(tmp_path / "rollback.db")
    clock = _Clock()
    service = OperationService(store, clock=clock, id_factory=lambda: "operation-a")

    def rejected(_operation_id, unit):
        unit.connection.execute(
            insert(meta).values(key="admission-side-effect", value="rolled-back")
        )
        raise RuntimeError("admission refused")

    try:
        with pytest.raises(RuntimeError, match="admission refused"):
            service.accept_operation(
                client_id="client-a",
                idempotency_key="send-a",
                method="frontend.controls.send",
                params={"participant_id": "participant-a", "prompt": "Review this."},
                prepare=rejected,
            )
        assert store.operations.get_idempotency("client-a", "send-a") is None
        assert (
            store.db.conn.execute(select(meta).where(meta.c.key == "admission-side-effect")).first()
            is None
        )

        action_calls = 0

        def completed(unit):
            nonlocal action_calls
            action_calls += 1
            unit.connection.execute(insert(meta).values(key="sync-result", value="committed"))
            return {"key": "note-a"}

        result = service.execute_idempotent(
            client_id="client-a",
            idempotency_key="scratch-a",
            method="frontend.scratchpad.write",
            params={"namespace": "shared", "value": "hello"},
            action=completed,
        )
        replay = service.execute_idempotent(
            client_id="client-a",
            idempotency_key="scratch-a",
            method="frontend.scratchpad.write",
            params={"value": "hello", "namespace": "shared"},
            action=completed,
        )
        record = store.operations.get_idempotency("client-a", "scratch-a")
        assert result.value == replay.value == {"key": "note-a"}
        assert not result.replayed and replay.replayed
        assert action_calls == 1
        assert record is not None
        assert record.settled_at == clock.value
        assert record.retain_until == clock.value + IDEMPOTENCY_RETENTION_SECONDS

        clock.value += IDEMPOTENCY_RETENTION_SECONDS + 1
        replacement = service.execute_idempotent(
            client_id="client-a",
            idempotency_key="scratch-a",
            method="frontend.scratchpad.write",
            params={"namespace": "shared", "value": "hello"},
            action=lambda _unit: {"key": "note-b"},
        )
        assert replacement.value == {"key": "note-b"}
        assert not replacement.replayed
        assert (
            store.db.conn.execute(select(func.count()).select_from(idempotency_records)).scalar()
            == 1
        )
    finally:
        store.close()
