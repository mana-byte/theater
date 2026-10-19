"""Participant metadata and status mutations over caller-owned units."""

from __future__ import annotations

from dataclasses import replace

from theater.constants.refusals import NEXT_NOT_YOUR_CHILD
from theater.daemon.events.publication import next_revision, participant_event
from theater.daemon.presence import access as presence_access
from theater.models import (
    BadRequest,
    ControlOwnerKind,
    NotYourChild,
    Participant,
    Status,
    normalize_participant_description,
    now,
)


def authorize_participant_mutation(target: Participant, caller_id: str) -> None:
    """Apply current control ownership, keeping the local operator privileged."""
    if caller_id == "cli":
        return
    owner_kind = target.control_owner_kind or (
        ControlOwnerKind.PARTICIPANT
        if target.parent_id is not None
        else ControlOwnerKind.LOCAL_OPERATOR
    )
    owner_id = target.control_owner_id or target.parent_id
    if caller_id != target.id and (
        owner_kind is not ControlOwnerKind.PARTICIPANT or owner_id != caller_id
    ):
        raise NotYourChild(
            f"refusing to mutate {target.id!r}: its current control owner is "
            f"{owner_id or owner_kind.value!r}, not you ({caller_id!r})",
            details={"next": NEXT_NOT_YOUR_CHILD.format(owner=owner_id or owner_kind.value)},
        )


def update_participant_metadata(
    daemon,
    participant_id: str,
    *,
    caller_id: str,
    name: str | None,
    description: str | None,
    unit=None,
) -> Participant:
    target = daemon.registry.resolve(participant_id)
    authorize_participant_mutation(target, caller_id)
    if unit is not None:
        if target.status is Status.DEAD:
            raise BadRequest(f"cannot update participant {target.id!r}: it is dead")
        normalized_description = (
            normalize_participant_description(description) if description is not None else None
        )
        if name is not None:
            daemon.registry._validate_name(target.id, name)
        updated = replace(target, name=name, description=normalized_description)
        daemon.registry.persist_in_connection(updated, unit.connection)
        # The live-name table is what a daemon restart reloads; the `participants` row has no name.
        if name is None:
            daemon.store.delete_participant_name(target.id, connection=unit.connection)
        else:
            daemon.store.set_participant_name(target.id, name, connection=unit.connection)
        event = participant_event(
            daemon.store,
            updated,
            unit.connection,
            revision=next_revision(daemon.store, unit.connection),
            recorded_at=now(),
        )
        daemon.store.journal.append_group(unit, [event])

        def update_live_name() -> None:
            if name is None:
                daemon.registry._names.pop(target.id, None)
            else:
                daemon.registry._names[target.id] = name

        unit.after_commit(update_live_name)
        return updated
    return daemon.registry.update_metadata(
        target.id,
        name=name,
        description=description,
    )


async def update_participant_status(
    daemon, participant_id: str, *, caller_id: str, status: Status
) -> Participant:
    target = daemon.registry.resolve(participant_id)
    authorize_participant_mutation(target, caller_id)
    await presence_access.require_absent(daemon, target.id)
    daemon.registry.set_status(target.id, status)
    return daemon.registry.get(target.id)


def persist_participant_status(daemon, participant_id: str, *, status: Status, unit) -> Participant:
    current = daemon.registry.get(participant_id)
    updated = replace(current, status=status, last_activity=now())
    daemon.registry.persist_in_connection(updated, unit.connection)
    daemon.store.journal.append_group(
        unit,
        [
            participant_event(
                daemon.store,
                updated,
                unit.connection,
                revision=next_revision(daemon.store, unit.connection),
                recorded_at=updated.last_activity,
            )
        ],
    )
    return updated
