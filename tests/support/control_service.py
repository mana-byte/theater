"""Builders and row readers shared by the control service test modules."""

from __future__ import annotations

from sqlalchemy import select

from tests.support.control_harness import (
    Harness,
    state_of,
)
from tests.support.fake_runtime import FakeRuntimeState
from tests.support.waiting import drain
from theater.daemon.persistence.repositories.native_evidence import NativeTerminalEvidence
from theater.daemon.persistence.store import Store
from theater.daemon.schema import control_operations as control_operations_table
from theater.harness.contracts.runtime import (
    ControlKind,
    NativeHumanInteraction,
    NativeInteractionKind,
    NativeTurnOutcome,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
)
from theater.models import (
    now,
)

PENDING_INTERACTION = NativeHumanInteraction(kind=NativeInteractionKind.APPROVAL)


BUSY_TURN = "turn-keeps-queue-pending"


async def queue_pending(harness: Harness, prompts: list[str], *, caller_id="caller"):
    """Queue followups that stay pending: busy while queued, then idle."""
    state = state_of(harness, "p1")
    state.native_turn_id = BUSY_TURN
    try:
        jobs = [
            await harness.service.queue_followup("p1", caller_id=caller_id, prompt=prompt)
            for prompt in prompts
        ]
        await drain()
    finally:
        state.native_turn_id = None
    return jobs


def native_outcome(
    state: FakeRuntimeState,
    *,
    terminal: NativeTurnTerminal = NativeTurnTerminal.COMPLETED,
    result: str | None = "the answer",
    turn: str | None = None,
    session: str | None = None,
) -> NativeTurnOutcome:
    return NativeTurnOutcome(
        native_session_id=session or state.native_session_id,
        native_turn_id=turn or state.native_turn_id,
        terminal=terminal,
        result=result,
        completeness=ResultCompleteness.COMPLETE,
        provenance=ResultProvenance.NATIVE_EVIDENCE,
    )


def terminal_evidence_row(
    state: FakeRuntimeState, turn: str, *, terminal=NativeTurnTerminal.COMPLETED
):
    return NativeTerminalEvidence(
        participant_id=state.participant_id,
        backend_generation=state.backend_generation,
        native_session_id=state.native_session_id,
        native_turn_id=turn,
        terminal=terminal,
        result="the answer",
        completeness=ResultCompleteness.COMPLETE,
        provenance=ResultProvenance.NATIVE_EVIDENCE,
        recorded_at=now(),
    )


def operation_rows(store: Store, participant_id: str, kind: ControlKind) -> list:
    return [
        dict(row._mapping)
        for row in store.conn.execute(
            select(control_operations_table)
            .where(control_operations_table.c.participant_id == participant_id)
            .where(control_operations_table.c.kind == str(kind))
        ).fetchall()
    ]
