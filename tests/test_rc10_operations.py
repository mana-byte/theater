"""Focused RC10 durable operation lifecycle and public-handler tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from theater.daemon.frontend.operation_handlers import (
    operations_await,
    operations_get,
    operations_list,
    operations_reconcile,
)
from theater.daemon.operations import (
    IDEMPOTENCY_RETENTION_SECONDS,
    DispatchIntent,
    InvalidOperationTransition,
    OperationNotifier,
    OperationOutcome,
    OperationService,
    PreparedOperation,
    ReconcileEvidence,
    operation_to_wire,
)
from theater.daemon.persistence.database import Database
from theater.daemon.persistence.repositories._json import decode_json
from theater.daemon.persistence.repositories.journal import JournalRepository
from theater.daemon.persistence.repositories.operations import OperationRepository
from theater.daemon.schema import orchestration_events
from theater.frontend.capabilities import METHOD_CATALOG
from theater.frontend.dto import Operation
from theater.frontend.schemas import validator_for
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
    clock: _Clock,
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


@pytest.mark.asyncio
async def test_detached_side_effect_survives_request_cancel_and_replays_handle(
    tmp_path: Path,
) -> None:
    store = _Store(tmp_path / "detached.db")
    clock = _Clock()
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
    store = _Store(tmp_path / "dispatch-validation.db")
    clock = _Clock()
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
    store = _Store(tmp_path / "wait.db")
    clock = _Clock()
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
    store = _Store(tmp_path / "lost-outcome.db")
    clock = _Clock()
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
    store = _Store(tmp_path / "handlers.db")
    clock = _Clock()
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
