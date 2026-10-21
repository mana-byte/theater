"""Durable operations: idempotent claims, lifecycle, dispatch identity and public handlers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, select

from tests.support.clock import Clock
from tests.support.operation_store import OperationStore
from theater.daemon.frontend.operation_handlers import (
    operations_await,
    operations_get,
    operations_list,
    operations_reconcile,
)
from theater.daemon.operations import (
    IDEMPOTENCY_RETENTION_SECONDS,
    DispatchIntent,
    IdempotencyConflict,
    InvalidOperationTransition,
    OperationNotifier,
    OperationOutcome,
    OperationService,
    PreparedOperation,
    ReconcileEvidence,
    operation_to_wire,
    request_digest,
)
from theater.daemon.persistence.repositories._json import decode_json
from theater.daemon.schema import idempotency_records, meta, orchestration_events, public_operations
from theater.frontend.capabilities import METHOD_CATALOG
from theater.frontend.dto import Operation
from theater.frontend.schemas import validator_for
from theater.models import PublicOperationRecord


def _prepare(
    clock: Clock,
    *,
    participant_id: str = "participant-a",
    job_handle: str = "job-a",
) -> Callable:
    def prepare(operation_id, _unit):
        return PreparedOperation(
            PublicOperationRecord(
                operation_id=operation_id,
                kind="send",
                actor_client_id="client-a",
                actor_participant_id="caller-a",
                target_ids=(participant_id,),
                state="accepted",
                phase="accepted",
                job_handle=job_handle,
                created_at=clock.value,
                updated_at=clock.value,
            ),
            {
                "operation_id": operation_id,
                "state": "accepted",
                "participant_id": participant_id,
                "job_handle": job_handle,
            },
        )

    return prepare


def _accept(
    service: OperationService,
    clock: Clock,
    *,
    key: str = "send-a",
    participant_id: str = "participant-a",
):
    return service.accept_operation(
        client_id="client-a",
        idempotency_key=key,
        method="frontend.controls.send",
        params={"participant_id": participant_id, "prompt": "Review this."},
        prepare=_prepare(clock, participant_id=participant_id),
    )


def _assert_terminal_dispatch(record: PublicOperationRecord) -> None:
    assert (
        record.dispatch_provider_id,
        record.dispatch_provider_generation,
        record.dispatch_terminal_id,
        record.dispatch_terminal_incarnation,
    ) == ("provider-a", 7, "terminal-a", "incarnation-a")
    assert record.dispatch_terminal_occupant_evidence == {
        "occupant_id": "occupant-a",
        "pid": 4242,
    }
    assert record.dispatch_terminal_process_facts == {
        "pid": 4242,
        "started_at": 99.0,
        "executable": "/usr/bin/agent",
    }
    wire = operation_to_wire(record)
    dispatch_wire = wire["dispatch_identity"]
    assert isinstance(dispatch_wire, dict)
    assert "provider_id" not in dispatch_wire
    assert dispatch_wire["terminal"] == {
        "provider_id": "provider-a",
        "provider_generation": 7,
        "terminal_id": "terminal-a",
        "terminal_incarnation": "incarnation-a",
        "occupant": {"occupant_id": "occupant-a", "pid": 4242},
        "process": {
            "pid": 4242,
            "started_at": 99.0,
            "executable": "/usr/bin/agent",
        },
    }
    public_operation = Operation.from_wire(wire)
    assert public_operation.dispatch_identity is not None
    assert public_operation.dispatch_identity.terminal is not None
    assert public_operation.dispatch_identity.terminal.provider_id == "provider-a"
    assert public_operation.dispatch_identity.terminal.occupant["occupant_id"] == "occupant-a"


def test_provider_target_projects_before_terminal_identity_is_known() -> None:
    record = PublicOperationRecord(
        operation_id="operation-a",
        kind="spawn",
        actor_client_id="client-a",
        actor_participant_id=None,
        target_ids=("participant-a",),
        state="running",
        phase="terminal_create_pending",
        dispatch_provider_id="provider-a",
        dispatch_provider_generation=7,
        created_at=100.0,
        updated_at=101.0,
    )

    wire = operation_to_wire(record)
    validator_for("https://theater.dev/schemas/frontend/1.0/common.json#/$defs/operation").validate(
        wire
    )
    operation = Operation.from_wire(wire)

    assert operation.dispatch_identity is not None
    assert operation.dispatch_identity.terminal is None
    assert operation.dispatch_identity.provider_id == "provider-a"
    assert operation.dispatch_identity.provider_generation == 7


@pytest.mark.asyncio
async def test_detached_side_effect_survives_request_cancel_and_replays_handle(
    tmp_path: Path,
) -> None:
    store = OperationStore(tmp_path / "detached.db")
    clock = Clock()
    service = OperationService(store, clock=clock, id_factory=lambda: "operation-a")
    accepted = asyncio.Event()
    effect_started = asyncio.Event()
    release_effect = asyncio.Event()
    calls = 0

    async def effect() -> OperationOutcome:
        nonlocal calls
        calls += 1
        effect_started.set()
        await release_effect.wait()
        return OperationOutcome.succeeded(phase="delivery_acknowledged", result={"delivered": True})

    async def request_handler() -> None:
        service.submit(
            client_id="client-a",
            idempotency_key="send-a",
            method="frontend.controls.send",
            params={"participant_id": "participant-a", "prompt": "Review this."},
            prepare=_prepare(clock),
            dispatch=DispatchIntent(
                phase="dispatch_intent",
                provider_id="provider-a",
                provider_generation=7,
                terminal_id="terminal-a",
                terminal_incarnation="incarnation-a",
                occupant_evidence={"occupant_id": "occupant-a", "pid": 4242},
                process_facts={
                    "pid": 4242,
                    "started_at": 99.0,
                    "executable": "/usr/bin/agent",
                },
            ),
            side_effect=effect,
        )
        accepted.set()
        await asyncio.Future()

    try:
        request = asyncio.create_task(request_handler())
        await accepted.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        await effect_started.wait()

        dispatched = service.get("operation-a")
        assert dispatched.state == "running"
        _assert_terminal_dispatch(dispatched)

        replay = _accept(service, clock)
        assert replay.replayed
        assert replay.record.operation_id == "operation-a"
        assert calls == 1

        release_effect.set()
        await asyncio.gather(*service.owned_tasks)
        finished = service.get("operation-a")
        assert finished.state == "succeeded"
        assert finished.job_handle == "job-a"
        assert finished.result == {"delivered": True}
        claim = store.operations.get_idempotency("client-a", "send-a")
        assert claim is not None
        assert claim.retain_until == finished.settled_at + IDEMPOTENCY_RETENTION_SECONDS
    finally:
        await service.close()
        store.close()


def test_terminal_dispatch_requires_exact_identity_without_claiming_a_key(tmp_path: Path) -> None:
    store = OperationStore(tmp_path / "dispatch-validation.db")
    clock = Clock()
    service = OperationService(store, clock=clock, id_factory=lambda: "operation-a")

    async def effect() -> OperationOutcome:
        return OperationOutcome.succeeded(phase="delivered")

    try:
        with pytest.raises(ValueError, match="occupant"):
            service.submit(
                client_id="client-a",
                idempotency_key="send-a",
                method="frontend.controls.send",
                params={"participant_id": "participant-a", "prompt": "Review this."},
                prepare=_prepare(clock),
                dispatch=DispatchIntent(
                    phase="dispatch_intent",
                    provider_id="provider-a",
                    provider_generation=7,
                    terminal_id="terminal-a",
                    terminal_incarnation="incarnation-a",
                ),
                side_effect=effect,
            )
        with pytest.raises(ValueError, match="cannot target terminal and native routes"):
            service.submit(
                client_id="client-a",
                idempotency_key="send-a",
                method="frontend.controls.send",
                params={"participant_id": "participant-a", "prompt": "Review this."},
                prepare=_prepare(clock),
                dispatch=DispatchIntent(
                    phase="dispatch_intent",
                    provider_id="provider-a",
                    provider_generation=7,
                    terminal_id="terminal-a",
                    terminal_incarnation="incarnation-a",
                    occupant_evidence={"occupant_id": "occupant-a"},
                    backend_generation=3,
                    native_session_id="session-a",
                ),
                side_effect=effect,
            )
        assert store.operations.get_idempotency("client-a", "send-a") is None
        assert store.operations.get("operation-a") is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_wait_closes_race_and_timeout_never_changes_state(tmp_path: Path) -> None:
    store = OperationStore(tmp_path / "wait.db")
    clock = Clock()
    service: OperationService

    class RacingNotifier(OperationNotifier):
        raced = False

        def subscribe(self, operation_id: str):
            subscription = super().subscribe(operation_id)
            if not self.raced:
                self.raced = True
                clock.value += 1
                service.mark_running(operation_id, phase="prepared")
            return subscription

    notifier = RacingNotifier()
    service = OperationService(
        store,
        clock=clock,
        id_factory=lambda: "operation-a",
        notifier=notifier,
    )
    try:
        _accept(service, clock)
        waiter = asyncio.create_task(service.wait("operation-a", wait_seconds=1))
        await asyncio.sleep(0)
        clock.value += 1
        service.succeed("operation-a", phase="delivery_acknowledged", result={"ok": True})
        record, timed_out = await waiter
        assert record.state == "succeeded"
        assert not timed_out

        other = OperationService(store, clock=clock, id_factory=lambda: "operation-b")
        _accept(other, clock, key="send-b", participant_id="participant-b")
        other.mark_running("operation-b", phase="dispatch_intent")
        other.mark_uncertain(
            "operation-b",
            phase="receipt_missing",
            error={"code": "internal", "message": "receipt unavailable"},
        )
        before = other.get("operation-b")
        observed, timed_out = await other.wait("operation-b", wait_seconds=0)
        assert timed_out
        assert observed == before
        assert observed.state == "uncertain"
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_lost_outcome_becomes_uncertain_and_dispatch_cannot_repeat(tmp_path: Path) -> None:
    store = OperationStore(tmp_path / "lost-outcome.db")
    clock = Clock()
    service = OperationService(store, clock=clock, id_factory=lambda: "operation-a")
    calls = 0

    async def effect() -> OperationOutcome:
        nonlocal calls
        calls += 1
        return OperationOutcome.succeeded(phase="")

    try:
        acceptance = service.submit(
            client_id="client-a",
            idempotency_key="send-a",
            method="frontend.controls.send",
            params={"participant_id": "participant-a", "prompt": "Review this."},
            prepare=_prepare(clock),
            dispatch=DispatchIntent(phase="dispatch_intent"),
            side_effect=effect,
        )
        await asyncio.gather(*service.owned_tasks)

        record = service.get(acceptance.record.operation_id)
        assert record.state == "uncertain"
        assert record.phase == "outcome_persistence_error"
        with pytest.raises(InvalidOperationTransition):
            service.start(
                record.operation_id,
                dispatch=DispatchIntent(phase="dispatch_intent"),
                side_effect=effect,
            )
        assert calls == 1
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_operation_handlers_page_validate_and_reconcile_only_from_evidence(
    tmp_path: Path,
) -> None:
    store = OperationStore(tmp_path / "handlers.db")
    clock = Clock()
    ids = iter(("operation-a", "operation-b"))

    async def reconcile(record: PublicOperationRecord) -> ReconcileEvidence:
        return ReconcileEvidence(
            observed_updated_at=record.updated_at,
            outcome=OperationOutcome.succeeded(
                phase="receipt_verified", result={"evidence": "provider-receipt-a"}
            ),
        )

    service = OperationService(
        store,
        clock=clock,
        id_factory=lambda: next(ids),
        reconciler=reconcile,
    )
    daemon = SimpleNamespace(operation_service=service)
    try:
        _accept(service, clock, key="send-a", participant_id="participant-a")
        clock.value += 1
        _accept(service, clock, key="send-b", participant_id="participant-b")
        service.link(
            "operation-a",
            phase="control_reserved",
            control_operation_id="control-a",
            job_handle="job-a",
        )
        service.mark_running("operation-a", phase="dispatch_intent")
        service.mark_uncertain(
            "operation-a",
            phase="receipt_missing",
            error={"code": "internal", "message": "receipt unavailable"},
        )

        first_page = await operations_list(daemon, None, {"limit": 1})
        validator_for(METHOD_CATALOG["frontend.operations.list"].result_schema_id).validate(
            first_page
        )
        assert [item["operation_id"] for item in first_page["items"]] == ["operation-b"]
        second_page = await operations_list(
            daemon, None, {"limit": 1, "cursor": first_page["next_cursor"]}
        )
        assert [item["operation_id"] for item in second_page["items"]] == ["operation-a"]

        filtered = await operations_list(
            daemon, None, {"target_id": "participant-a", "unsettled_only": True}
        )
        assert [item["operation_id"] for item in filtered["items"]] == ["operation-a"]
        fetched = await operations_get(daemon, None, {"operation_id": "operation-a"})
        validator_for(METHOD_CATALOG["frontend.operations.get"].result_schema_id).validate(fetched)
        assert fetched["control_operation_id"] == "control-a"
        assert fetched["job_handle"] == "job-a"

        waited = await operations_await(
            daemon, None, {"operation_id": "operation-a", "wait_seconds": 0}
        )
        assert waited["timed_out"] is True
        assert waited["operation"]["state"] == "uncertain"

        reconciled = await operations_reconcile(daemon, None, {"operation_id": "operation-a"})
        validator_for(METHOD_CATALOG["frontend.operations.reconcile"].result_schema_id).validate(
            reconciled
        )
        assert reconciled["operation_id"] == "operation-a"
        assert reconciled["state"] == "succeeded"
        assert reconciled["result"] == {"evidence": "provider-receipt-a"}

        events = store.db.conn.execute(
            select(
                orchestration_events.c.kind,
                orchestration_events.c.entity_id,
                orchestration_events.c.entity_revision,
                orchestration_events.c.payload,
            ).order_by(orchestration_events.c.sequence)
        ).all()
        assert all(kind == "operation.updated" for kind, _, _, _ in events)
        assert [revision for _, _, revision, _ in events] == list(range(1, len(events) + 1))
        payload = decode_json(events[-1].payload)
        assert payload["operation_id"] == "operation-a"
        assert payload["actor"] == {"client_id": "client-a", "participant_id": "caller-a"}
        assert payload["state"] == "succeeded"
    finally:
        await service.close()
        store.close()


def _recording_prepare(
    clock: Clock,
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
    store = OperationStore(database_path)
    clock = Clock()
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
            prepare=_recording_prepare(clock, calls),
        )
        replay = service.accept_operation(
            client_id="client-a",
            idempotency_key="send-a",
            method="frontend.controls.send",
            params={"prompt": "Review this.", "participant_id": "participant-a"},
            prepare=_recording_prepare(clock, calls),
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
                prepare=_recording_prepare(clock, calls),
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

    reopened = OperationStore(database_path)
    try:
        durable = OperationService(reopened, clock=clock, id_factory=lambda: "operation-unused")
        replay = durable.accept_operation(
            client_id="client-a",
            idempotency_key="send-a",
            method="frontend.controls.send",
            params={"participant_id": "participant-a", "prompt": "Review this."},
            prepare=_recording_prepare(clock, calls),
        )
        assert replay.replayed
        assert replay.record.operation_id == "operation-a"
        assert calls == ["operation-a"]
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_concurrent_first_claims_converge_on_one_operation(tmp_path: Path) -> None:
    store = OperationStore(tmp_path / "converge.db")
    clock = Clock()
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
            prepare=_recording_prepare(clock, calls),
        )

    try:
        left, right = await asyncio.gather(submit(), submit())
        assert left.record.operation_id == right.record.operation_id == "operation-a"
        assert {left.replayed, right.replayed} == {False, True}
        assert calls == ["operation-a"]
    finally:
        store.close()


def test_failed_admission_claim_rolls_back_and_completed_results_retain(tmp_path: Path) -> None:
    store = OperationStore(tmp_path / "rollback.db")
    clock = Clock()
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


def test_replacement_in_outer_unit_rolls_back_and_keeps_cas_and_event_order(
    tmp_path: Path,
) -> None:
    store = OperationStore(tmp_path / "replacement-unit.db")
    clock = Clock()
    ids = iter(("operation-a", "operation-b"))
    service = OperationService(store, clock=clock, id_factory=lambda: next(ids))
    try:
        first = _accept(service, clock, key="send-a", participant_id="participant-a").record
        second = _accept(service, clock, key="send-b", participant_id="participant-b").record
        first = service.fail(
            first.operation_id,
            phase="preexisting_failure",
            error={"code": "internal", "message": "already settled"},
        )
        first_claim = store.operations.get_idempotency("client-a", "send-a")
        assert first_claim is not None
        dispatch = replace(
            first,
            phase="dispatch_target",
            updated_at=first.updated_at + 1,
        )
        settled_at = second.updated_at + 2
        failed = replace(
            second,
            state="failed",
            phase="recovery_failed",
            updated_at=settled_at,
            settled_at=settled_at,
            error_code="daemon_restarted",
            error={"code": "daemon_restarted", "message": "not dispatched"},
        )
        cursor = store.journal.current_sequence()
        notifications: list[int] = []
        wakes: list[str] = []
        store.journal.register_listener(notifications.append)

        def write_group(unit) -> None:
            first_event = service.write_replacement_in_unit(
                unit,
                expected_state=first.state,
                expected_updated_at=first.updated_at,
                replacement=dispatch,
                idempotency_settled_at=None,
            )
            second_event = service.write_replacement_in_unit(
                unit,
                expected_state=second.state,
                expected_updated_at=second.updated_at,
                replacement=failed,
                idempotency_settled_at=settled_at,
            )
            assert first_event is not None and second_event is not None
            revision = store.journal.current_sequence(connection=unit.connection) + 1
            store.journal.append_group(
                unit,
                [
                    replace(first_event, entity_revision=revision),
                    replace(second_event, entity_revision=revision + 1),
                ],
            )
            unit.after_commit(lambda: wakes.append("committed"))

        with pytest.raises(RuntimeError, match="abort"), store.write_unit() as unit:
            write_group(unit)
            raise RuntimeError("abort")
        assert store.operations.get(first.operation_id) == first
        assert store.operations.get(second.operation_id) == second
        assert store.operations.get_idempotency("client-a", "send-b").settled_at is None
        assert store.journal.current_sequence() == cursor
        assert notifications == wakes == []

        with store.write_unit() as unit:
            assert (
                service.write_replacement_in_unit(
                    unit,
                    expected_state="running",
                    expected_updated_at=first.updated_at,
                    replacement=dispatch,
                    idempotency_settled_at=None,
                )
                is None
            )
        assert store.journal.current_sequence() == cursor

        with store.write_unit() as unit:
            write_group(unit)
        groups = store.journal.groups_after(cursor, limit=10)
        assert len(groups) == 1
        assert [(event.entity_id, event.entity_revision) for event in groups[0].events] == [
            (first.operation_id, cursor + 1),
            (second.operation_id, cursor + 2),
        ]
        assert notifications == [cursor + 2] and wakes == ["committed"]
        assert store.operations.get(first.operation_id) == dispatch
        assert store.operations.get(second.operation_id) == failed
        assert store.operations.get_idempotency("client-a", "send-a") == first_claim
        claim = store.operations.get_idempotency("client-a", "send-b")
        assert claim.settled_at == settled_at
        assert claim.retain_until == settled_at + IDEMPOTENCY_RETENTION_SECONDS
    finally:
        store.close()
