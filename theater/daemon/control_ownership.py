"""Atomic current-control ownership transfer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from theater.daemon.events.publication import (
    control_event,
    job_event,
    next_revision,
    participant_event,
)
from theater.daemon.persistence.transactions import WriteUnit
from theater.models import (
    ControlOwnerKind,
    Participant,
    Status,
    TheaterError,
    now,
)


class OwnershipConflict(TheaterError):
    code = "ownership_conflict"


class ControlTransferService:
    """Validate and commit one bounded ownership batch without rewriting lineage."""

    def __init__(self, daemon) -> None:
        self._store = daemon.store
        self._registry = daemon.registry
        self._controls = daemon.controls

    def transfer(
        self,
        participants: Sequence[Mapping[str, object]],
        new_owner: Mapping[str, object],
        *,
        unit: WriteUnit,
        actor_participant_id: str | None = None,
    ) -> dict[str, object]:
        requested = self._requested(participants)
        ids = [participant_id for participant_id, _revision in requested]
        all_participants = {
            participant.id: participant
            for participant in self._store.list_participants(
                include_dead=True, connection=unit.connection
            )
        }
        targets = self._validated_targets(requested, all_participants, actor_participant_id)
        owner_kind, owner_id = self._validated_owner(new_owner, all_participants)

        proposed = {
            participant.id: self._owner_id(participant) for participant in all_participants.values()
        }
        for target in targets:
            proposed[target.id] = owner_id
        self._reject_cycles(proposed, ids)

        timestamp = now()
        updated = [
            replace(
                target,
                control_owner_kind=owner_kind,
                control_owner_id=owner_id,
                control_revision=target.control_revision + 1,
            )
            for target in targets
        ]
        for participant in updated:
            self._registry.persist_in_connection(participant, unit.connection)
        queued = [
            operation
            for participant_id in ids
            for operation in self._store.queued_control_operations(
                participant_id, connection=unit.connection
            )
        ]
        cancelled = self._controls.cancel_queued_for_control_transfer(
            ids, unit=unit, timestamp=timestamp
        )

        first = next_revision(self._store, unit.connection)
        events = [
            participant_event(
                self._store,
                participant,
                unit.connection,
                revision=first + index,
                recorded_at=timestamp,
                kind="participant.owner_changed",
            )
            for index, participant in enumerate(updated)
        ]
        for operation in queued:
            current = self._store.get_control_operation(
                operation.operation_id, connection=unit.connection
            )
            if current is None:
                continue
            event = control_event(
                self._store,
                current,
                unit.connection,
                revision=first + len(events),
            )
            if event is not None:
                events.append(event)
        first_job_revision = first + len(events)
        events.extend(
            job_event(
                job,
                revision=first_job_revision + index,
                recorded_at=timestamp,
            )
            for index, job in enumerate(cancelled)
        )
        self._store.journal.append_group(unit, events)
        return {
            "participants": [
                {
                    "participant_id": participant.id,
                    "owner": {
                        "kind": owner_kind.value,
                        "participant_id": owner_id,
                        "revision": participant.control_revision,
                    },
                }
                for participant in updated
            ],
            "cancelled_job_handles": [job.handle for job in cancelled],
        }

    @staticmethod
    def _requested(
        participants: Sequence[Mapping[str, object]],
    ) -> list[tuple[str, int]]:
        if not 1 <= len(participants) <= 500:
            raise OwnershipConflict("control transfer requires between 1 and 500 participants")
        requested: list[tuple[str, int]] = []
        for item in participants:
            revision = item["expected_revision"]
            if type(revision) is not int:
                raise OwnershipConflict("expected control revisions must be integers")
            requested.append((str(item["participant_id"]), revision))
        requested.sort()
        if len(requested) != len({participant_id for participant_id, _ in requested}):
            raise OwnershipConflict("control transfer participant IDs must be unique")
        return requested

    @classmethod
    def _validated_targets(
        cls,
        requested: Sequence[tuple[str, int]],
        all_participants: Mapping[str, Participant],
        actor_participant_id: str | None,
    ) -> list[Participant]:
        targets: list[Participant] = []
        for participant_id, expected_revision in requested:
            target = all_participants.get(participant_id)
            if target is None:
                raise OwnershipConflict(f"no participant {participant_id!r} exists")
            if target.status is Status.DEAD:
                raise OwnershipConflict(f"participant {participant_id!r} is dead")
            if target.control_revision != expected_revision:
                raise OwnershipConflict(
                    f"participant {participant_id!r} control revision is "
                    f"{target.control_revision}, not {expected_revision}"
                )
            cls._authorize(target, actor_participant_id)
            targets.append(target)
        return targets

    @staticmethod
    def _validated_owner(
        new_owner: Mapping[str, object], all_participants: Mapping[str, Participant]
    ) -> tuple[ControlOwnerKind, str | None]:
        owner_kind = ControlOwnerKind(str(new_owner["kind"]))
        owner_id_value = new_owner.get("participant_id")
        owner_id = str(owner_id_value) if owner_id_value is not None else None
        if owner_kind is ControlOwnerKind.LOCAL_OPERATOR:
            return owner_kind, None
        owner = all_participants.get(owner_id or "")
        if owner is None or owner.status is Status.DEAD:
            raise OwnershipConflict(f"new control owner {owner_id!r} is not live")
        return owner_kind, owner_id

    @staticmethod
    def _owner_id(participant: Participant) -> str | None:
        kind = participant.control_owner_kind or (
            ControlOwnerKind.PARTICIPANT
            if participant.parent_id is not None
            else ControlOwnerKind.LOCAL_OPERATOR
        )
        if kind is ControlOwnerKind.LOCAL_OPERATOR:
            return None
        return participant.control_owner_id or participant.parent_id

    @staticmethod
    def _authorize(target: Participant, actor_participant_id: str | None) -> None:
        if actor_participant_id is None:
            return
        if ControlTransferService._owner_id(target) != actor_participant_id:
            raise OwnershipConflict(
                f"participant {target.id!r} is not controlled by {actor_participant_id!r}"
            )

    @staticmethod
    def _reject_cycles(owners: Mapping[str, str | None], changed_ids: Sequence[str]) -> None:
        for participant_id in changed_ids:
            seen: set[str] = set()
            current: str | None = participant_id
            while current is not None:
                if current in seen:
                    raise OwnershipConflict("control transfer would create an ownership cycle")
                seen.add(current)
                current = owners.get(current)


__all__ = ["ControlTransferService", "OwnershipConflict"]
