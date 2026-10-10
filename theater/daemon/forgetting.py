"""Forget an unbound orphan: retire a participant no terminal or process can still embody.

No terminal I/O and no presence gate: with no binding there is no pane to inject into.
Every precondition is re-read inside the write unit that marks the participant dead.
"""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import exists, func, select

from theater.constants.refusals import NEXT_ASK_OPERATOR, NEXT_NOT_YOUR_CHILD
from theater.daemon.events.publication import next_revision, participant_event
from theater.daemon.schema import launch_reservations, public_operations
from theater.harness.contracts.runtime import RuntimeCapability
from theater.models import (
    BadRequest,
    ControlOwnerKind,
    JobState,
    NoSelfKill,
    NotYourChild,
    Participant,
    Status,
    TheaterError,
    now,
)
from theater.proc import pid_alive

FORGOTTEN_REASON = "forgotten"
_SETTLED_LAUNCH_PHASES = frozenset({"terminal_bound", "rolled_back"})
_IN_FLIGHT_SPAWN_STATES = ("accepted", "running")
_NEXT_NORMAL_KILL = "kill without forget; it goes through the terminal and presence gate"


def _is_dead(daemon, participant_id: str) -> bool:
    row = daemon.store.get_participant(participant_id)
    return row is None or row.status is Status.DEAD


def authorize_forget(daemon, target: Participant, caller_id: str) -> None:
    """Operator, current owner or parent; any live participant once both are dead."""
    if caller_id == "cli":
        return
    if caller_id == target.id:
        raise NoSelfKill(f"refusing to forget {target.id!r}: that is you, not your child")
    candidates = [target.parent_id]
    if target.control_owner_kind is not ControlOwnerKind.LOCAL_OPERATOR:
        candidates.append(target.control_owner_id)
    owners = {owner for owner in candidates if owner is not None}
    if caller_id in owners:
        return
    operator_owned = target.control_owner_kind is ControlOwnerKind.LOCAL_OPERATOR or not owners
    orphan = not operator_owned and all(_is_dead(daemon, owner) for owner in owners)
    if orphan and not _is_dead(daemon, caller_id):
        return
    owner = target.control_owner_id or target.parent_id or ControlOwnerKind.LOCAL_OPERATOR.value
    raise NotYourChild(
        f"refusing to forget {target.id!r}: its current control owner is {owner!r}, "
        f"not you ({caller_id!r}), and it is not an orphan",
        details={"next": NEXT_NOT_YOUR_CHILD.format(owner=owner)},
    )


def _launch_state(connection, participant_id: str) -> tuple[str | None, set[str]]:
    """The launch reservation phase and the states of spawns targeting the participant."""
    phase = connection.execute(
        select(launch_reservations.c.phase).where(
            launch_reservations.c.participant_id == participant_id
        )
    ).scalar()
    targets = func.json_each(public_operations.c.target_ids).table_valued("value")
    states = connection.execute(
        select(public_operations.c.state).where(
            public_operations.c.kind == "spawn",
            exists(select(1).select_from(targets).where(targets.c.value == participant_id)),
        )
    ).scalars()
    return phase, set(states)


def forget_refusal(
    daemon, target: Participant, caller_id: str, connection=None
) -> TheaterError | None:
    """Why ``target`` cannot be forgotten by ``caller_id`` now, or None when it can."""
    pid = target.id
    if daemon.store.terminal_bindings.get(pid, connection=connection) is not None:
        return BadRequest(
            f"refusing to forget {pid!r}: it has a terminal binding",
            details={"next": _NEXT_NORMAL_KILL},
        )
    if daemon.controls.route_for(pid, RuntimeCapability.SEND).route_available:
        return BadRequest(
            f"refusing to forget {pid!r}: it is addressable through a verified route",
            details={"next": _NEXT_NORMAL_KILL},
        )
    runtime = daemon.store.get_runtime_binding(pid)
    pids = [target.pid, None if runtime is None else runtime.backend_pid]
    live = [value for value in pids if value is not None and pid_alive(value)]
    if live:
        return BadRequest(
            f"refusing to forget {pid!r}: recorded process {live[0]} is alive "
            "(a reused pid also refuses, on purpose)",
            details={"next": _NEXT_NORMAL_KILL},
        )
    phase, spawn_states = _launch_state(connection or daemon.store.conn, pid)
    if spawn_states & set(_IN_FLIGHT_SPAWN_STATES):
        return BadRequest(
            f"refusing to forget {pid!r}: its spawn is still in flight",
            details={"next": "await the spawn operation, then kill"},
        )
    settled = phase is None or phase in _SETTLED_LAUNCH_PHASES
    if (not settled or "uncertain" in spawn_states) and caller_id != "cli":
        # A dispatched create with no receipt may own a terminal whose pid Theater never saw.
        return BadRequest(
            f"refusing to forget {pid!r}: its spawn outcome is uncertain, so a terminal "
            "may exist; only the local operator may forget it",
            details={"next": NEXT_ASK_OPERATOR},
        )
    return None


async def forget_participant(daemon, target: Participant, *, caller_id: str) -> dict:
    """Retire an unbound orphan in one write unit, then release what it held."""
    pid = target.id
    authorize_forget(daemon, target, caller_id)
    timestamp = now()
    with daemon.store.write_unit() as unit:
        current = daemon.store.get_participant(pid, connection=unit.connection)
        if current is None or current.status is Status.DEAD:
            return {"id": pid, "killed": False, "reason": "already_dead"}
        refusal = forget_refusal(daemon, current, caller_id, connection=unit.connection)
        if refusal is not None:
            raise refusal
        retired = replace(
            current,
            status=Status.DEAD,
            termination_reason=FORGOTTEN_REASON,
            terminated_at=timestamp,
            last_activity=timestamp,
        )
        daemon.registry.persist_in_connection(retired, unit.connection)
        event = participant_event(
            daemon.store,
            retired,
            unit.connection,
            revision=next_revision(daemon.store, unit.connection),
            recorded_at=timestamp,
        )
        daemon.store.journal.append_group(unit, [event])
    await daemon.controls.cancel_queued_followups(pid)
    for job in daemon.store.running_jobs_for_target(pid):
        daemon.jobs.finish(
            job.handle, state=JobState.KILLED, result="target forgotten", error_code="killed"
        )
    daemon.store.delete_runtime_binding(pid)
    await daemon.spawner.teardown(target)
    from theater.daemon.worktrees.retirement import cleanup_killed_workspace

    result: dict = {"id": pid, "killed": True, "forgotten": True}
    cleanup = await cleanup_killed_workspace(daemon, target)
    if cleanup is not None:
        result["workspace_cleanup"] = cleanup
    return result
