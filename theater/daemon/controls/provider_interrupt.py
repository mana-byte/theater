"""Provider interrupt planning without assuming one key works for every harness."""

from __future__ import annotations

from theater.daemon.controls.gates import ControlGates
from theater.daemon.persistence.store import Store
from theater.models import BadRequest, Status


def interrupt_action(
    store: Store, gates: ControlGates, participant_id: str
) -> dict[str, object] | None:
    participant = store.get_participant(participant_id)
    if (
        participant is not None
        and participant.status is Status.IDLE
        and not store.active_running_jobs_for_target(participant_id)
        and not store.has_execution_barrier(participant_id)
    ):
        return None
    plan = gates.terminal_interrupt_plan(participant_id)
    if plan is None:
        raise BadRequest(
            f"participant {participant_id!r} has no harness-declared terminal interrupt sequence; "
            "use a supported native runtime or stop the participant explicitly"
        )
    return {
        "keys": list(plan.keys),
        "inter_key_delay_seconds": plan.inter_key_delay_seconds or 0.0,
    }
