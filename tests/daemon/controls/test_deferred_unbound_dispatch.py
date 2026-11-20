"""Deferred-unbound queue rows: late binding to the first verified route, with fences.

A queued row admitted while its native runtime was disconnected carries the explicit
``deferred_unbound`` marker. It late-binds to the current route only after fresh presence and
modal gates, labels provider delivery ``PROVIDER_TERMINAL`` (so execution barriers stay
visible), and never crosses a known native session fence.
"""

from __future__ import annotations

import dataclasses
import json

from tests.support.control_harness import Harness
from theater.daemon.controls import ControlService
from theater.daemon.controls._common import QUEUE_DEFERRED_UNBOUND
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.daemon.persistence.store import Store
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
)
from theater.models import AwaitingDecision, JobState, TerminalBindingRecord, now


def _provider_harness(
    store: Store,
    participant_id: str = "p1",
    *,
    modal_check=None,
    delivery: str = "accepted",
):
    """A participant whose only route is a healthy provider terminal, with a recording dispatch."""
    harness = Harness(store, {})
    timestamp = now()
    with store.write_unit() as unit:
        store.terminal_bindings.bind(
            TerminalBindingRecord(
                participant_id=participant_id,
                provider_id="provider-a",
                provider_generation=1,
                terminal_id="terminal-a",
                terminal_incarnation="incarnation-a",
                occupant_evidence={"occupant_id": "occupant-a", "harness": "codex"},
                process_facts={"pid": 42, "started_at": 1.0, "executable": "/bin/agent"},
                health="healthy",
                report_revision=1,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
    deliveries: list[tuple[str, object]] = []

    async def dispatch(provider_id, generation, method, params):
        deliveries.append((method, params["action"]))
        return {"delivery": delivery}

    gates = dataclasses.replace(
        harness.gates_recorder.gates(),
        provider_health=lambda *_args: "online",
        provider_dispatch=dispatch,
        provider_modal_check=modal_check,
    )
    harness.service = ControlService(
        store=store,
        jobs=harness.jobs,
        runtime_for=harness.runtimes.get,
        gates=gates,
    )
    return harness, deliveries


def _queue_row(
    store: Store,
    harness: Harness,
    *,
    participant_id: str = "p1",
    prompt: str = "ping",
    deferred: bool = True,
    session: str | None = None,
    generation: int | None = 1,
    suffix: str = "q",
) -> tuple[str, str]:
    """Create one queued job + control row, optionally carrying the deferred marker."""
    handle = f"{participant_id}#{suffix}"
    harness.jobs.create(
        handle=handle,
        caller_id="caller",
        target_id=participant_id,
        kind="send",
        prompt=prompt,
        cwd=None,
    )
    payload = json.dumps({QUEUE_DEFERRED_UNBOUND: True, "origin": "send"}) if deferred else None
    store.reserve_control_operation(
        ControlOperation(
            operation_id=f"{handle}:queue_followup",
            participant_id=participant_id,
            kind=ControlKind.QUEUE_FOLLOWUP,
            transport=ControlTransport.NATIVE_RUNTIME,
            delivery_phase=ControlDeliveryPhase.QUEUED,
            job_handle=handle,
            backend_generation=generation,
            native_session_id=session,
            queue_sequence=1,
            payload=payload,
            created_at=now(),
            updated_at=now(),
        )
    )
    return f"{handle}:queue_followup", handle


async def test_marked_deferred_intent_late_binds_to_provider_on_startup_fallback(store: Store):
    """A never-connected intent (generation set, session unknown) adopts the provider route."""
    harness, deliveries = _provider_harness(store)
    operation_id, handle = _queue_row(store, harness, session=None, generation=1)

    outcome = await harness.service.dispatch_queue("p1")

    assert outcome.dispatched == (handle,)
    assert outcome.failed == ()
    assert deliveries == [("terminal.deliver", {"kind": "submit_text", "text": "ping"})]
    op = store.get_control_operation(operation_id)
    assert op.delivery_phase is ControlDeliveryPhase.SETTLED
    assert op.delivery_result is DeliveryResult.ACCEPTED
    # Labeled PROVIDER_TERMINAL, not TERMINAL_FALLBACK, so execution barriers stay visible.
    assert op.transport is ControlTransport.PROVIDER_TERMINAL
    assert op.provider_id == "provider-a"
    assert op.terminal_id == "terminal-a"
    # The provenance marker and the (unknown) native identity survive the route change.
    assert json.loads(op.payload or "{}")[QUEUE_DEFERRED_UNBOUND] is True
    assert op.native_session_id is None


async def test_provider_late_bind_keeps_execution_barrier_visible(store: Store):
    """An unknown provider delivery of a late-bound row sets a visible execution barrier."""
    harness, _deliveries = _provider_harness(store, delivery="unknown")
    operation_id, handle = _queue_row(store, harness, session=None, generation=1)

    outcome = await harness.service.dispatch_queue("p1")

    assert outcome.dispatched == (handle,)
    op = store.get_control_operation(operation_id)
    assert op.transport is ControlTransport.PROVIDER_TERMINAL
    assert op.delivery_result is DeliveryResult.UNKNOWN
    # has_execution_barrier only recognizes NATIVE_RUNTIME/PROVIDER_TERMINAL transports.
    assert store.has_execution_barrier("p1") is True


async def test_marked_intent_with_known_session_never_fails_over_to_provider(store: Store):
    """A row bound to a known native session is refused, not delivered into another session."""
    harness, deliveries = _provider_harness(store)
    operation_id, handle = _queue_row(store, harness, session="thread-a", generation=1)

    outcome = await harness.service.dispatch_queue("p1")

    assert outcome.dispatched == ()
    assert outcome.failed == ((handle, "stale_target"),)
    assert deliveries == []
    assert store.get_job(handle).state == JobState.CRASHED
    op = store.get_control_operation(operation_id)
    assert op.delivery_result is DeliveryResult.REJECTED
    # The stored native session is preserved, never erased by provider dispatch.
    assert op.native_session_id == "thread-a"


async def test_unmarked_queued_row_does_not_late_bind_to_provider(store: Store):
    """Historical unpinned rows keep their behavior: no inferred late-bind permission."""
    harness, deliveries = _provider_harness(store)
    operation_id, handle = _queue_row(store, harness, deferred=False, session=None, generation=1)

    outcome = await harness.service.dispatch_queue("p1")

    assert outcome.dispatched == ()
    assert outcome.failed == ((handle, "not_addressable"),)
    assert deliveries == []
    op = store.get_control_operation(operation_id)
    assert op.transport is ControlTransport.TERMINAL_FALLBACK


async def test_provider_modal_dialog_still_defers_a_late_bound_intent(store: Store):
    """The provider modal guard is untouched: a trust dialog defers, never delivers."""

    async def modal_check(participant_id: str) -> None:
        raise AwaitingDecision(f"dialog open at {participant_id!r}")

    harness, deliveries = _provider_harness(store, modal_check=modal_check)
    operation_id, handle = _queue_row(store, harness, session=None, generation=1)

    outcome = await harness.service.dispatch_queue("p1")

    assert outcome.dispatched == () and outcome.failed == ()
    assert outcome.deferred is True
    assert deliveries == []
    op = store.get_control_operation(operation_id)
    assert op.delivery_phase is ControlDeliveryPhase.QUEUED
    assert op.transport is ControlTransport.PROVIDER_TERMINAL
    assert store.get_job(handle).state == JobState.RUNNING


async def test_bound_provider_row_still_never_fails_over_on_terminal_change(store: Store):
    """A row pinned to a different provider terminal still fails instead of replaying."""
    harness, deliveries = _provider_harness(store)
    operation_id, handle = _queue_row(store, harness, deferred=False, session=None, generation=1)
    with store.write_unit() as unit:
        store.bind_queued_provider_identity(
            operation_id,
            provider_id="provider-old",
            provider_generation=9,
            terminal_id="terminal-old",
            terminal_incarnation="incarnation-old",
            updated_at=now(),
            connection=unit.connection,
        )

    outcome = await harness.service.dispatch_queue("p1")

    assert outcome.dispatched == ()
    assert outcome.failed == ((handle, "stale_target"),)
    assert deliveries == []
    assert store.get_job(handle).state == JobState.CRASHED
