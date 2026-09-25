"""Participant rows, artifacts, tmux-restart and transcript-bind facts."""

from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import insert, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.constants.daemon import (
    BUS_KIND_OPERATOR_TRANSCRIPT_BIND,
    BUS_KIND_OPERATOR_TRANSCRIPT_UNBIND,
    BUS_KIND_TMUX_SERVER_RESTART,
    TMUX_PROVIDER_IDENTITY_META_PREFIX,
    TMUX_SERVER_IDENTITY_META_KEY,
    TMUX_SERVER_RESTART_AFFECTED_IDS_LIMIT,
)
from theater.daemon.artifacts import OwnedArtifact
from theater.daemon.events.publication import next_revision, participant_event
from theater.daemon.persistence.repositories.participants import ParticipantRepository
from theater.daemon.persistence.store_parts._host import StoreHost
from theater.daemon.persistence.transactions import SQLiteWriteUnit
from theater.daemon.schema import bus, participants
from theater.models import Participant, Status, now


class ParticipantStore(StoreHost):
    """Store-facing participants methods; state lives on ``Store``."""

    def upsert_participant(self, p: Participant, *, connection=None) -> None:
        if connection is not None:
            self._participants.upsert(p, connection=connection)
            return
        with self.write_unit() as unit:
            before = self._participants.get(p.id, connection=unit.connection)
            before_payload = (
                None
                if before is None
                else participant_event(
                    self,
                    before,
                    unit.connection,
                    revision=0,
                    recorded_at=0,
                ).payload
            )
            self._participants.upsert(p, connection=unit.connection)
            persisted = self._participants.get(p.id, connection=unit.connection)
            assert persisted is not None
            persisted.name = p.name
            event = participant_event(
                self,
                persisted,
                unit.connection,
                revision=next_revision(self, unit.connection),
                recorded_at=now(),
            )
            if before_payload != event.payload:
                self.journal.append_group(unit, [event])

    def get_participant(self, pid: str, *, connection=None) -> Participant | None:
        return self._participants.get(pid, connection=connection)

    def find_by_pane(self, pane: str) -> Participant | None:
        return self._participants.find_by_pane(pane)

    def list_participants(
        self,
        *,
        include_dead: bool = False,
        ids: Sequence[str] | None = None,
        parent_id: str | None = None,
        after: tuple[float, str] | None = None,
        limit: int | None = None,
        connection=None,
    ) -> list[Participant]:
        return self._participants.list_all(
            include_dead=include_dead,
            ids=ids,
            parent_id=parent_id,
            after=after,
            limit=limit,
            connection=connection,
        )

    def add_participant_artifacts(
        self, participant_id: str, artifacts: Sequence[OwnedArtifact]
    ) -> None:
        self._artifacts.add_many(participant_id, artifacts)

    def participant_artifacts(self, participant_id: str) -> tuple[OwnedArtifact, ...]:
        return self._artifacts.list_for(participant_id)

    def participant_artifact_owner_ids(self) -> tuple[str, ...]:
        return self._artifacts.owner_ids()

    def delete_participant_artifacts(self, participant_id: str, *, connection=None) -> int:
        return self._artifacts.delete_for(participant_id, connection=connection)

    def list_recent_dead(
        self, *, limit: int = 20, exclude_session_ids: set[str] | None = None
    ) -> list[Participant]:
        return self._participants.list_recent_dead(
            limit=limit, exclude_session_ids=exclude_session_ids
        )

    def children_of(self, pid: str) -> list[Participant]:
        return self._participants.children_of(pid)

    def set_status(self, pid: str, status: Status) -> None:
        participant = self._participants.get(pid)
        if participant is None:
            return
        participant.status = status
        participant.last_activity = now()
        self.upsert_participant(participant)

    def stamp_live_tmux_server_identity(
        self,
        identity: str,
        *,
        participant_ids: Sequence[str] | None = None,
    ) -> int:
        return self._participants.stamp_live_tmux_server_identity(
            identity,
            participant_ids=participant_ids,
        )

    def record_tmux_server_restart(
        self,
        *,
        server_identity: str,
        affected_ids: Sequence[str],
        newly_owned_ids: Sequence[str],
        incident: str,
        terminated_at: float,
        provider_id: str | None = None,
        unit: SQLiteWriteUnit | None = None,
    ) -> int:
        payload = {
            "incident": incident,
            "affected_count": len(affected_ids),
            "affected_ids": list(affected_ids[:TMUX_SERVER_RESTART_AFFECTED_IDS_LIMIT]),
        }
        if provider_id is not None:
            payload["provider_id"] = provider_id
        listeners = tuple(self._bus_listeners)
        timestamp = now()
        meta_key = (
            TMUX_SERVER_IDENTITY_META_KEY
            if provider_id is None
            else f"{TMUX_PROVIDER_IDENTITY_META_PREFIX}{provider_id}"
        )

        def record(active_unit: SQLiteWriteUnit) -> int:
            conn = active_unit.connection
            changed = [
                participant
                for participant_id in dict.fromkeys(affected_ids)
                if (participant := self._participants.get(participant_id, connection=conn))
                is not None
                and participant.status is not Status.DEAD
            ]
            self._participants.mark_tmux_restarted(
                affected_ids,
                incident=incident,
                terminated_at=terminated_at,
                connection=conn,
            )
            self._participants.stamp_live_tmux_server_identity(
                server_identity,
                participant_ids=newly_owned_ids,
                connection=conn,
            )
            self._meta.set(
                meta_key,
                server_identity,
                connection=conn,
            )
            row_id = self._bus.append(
                BUS_KIND_TMUX_SERVER_RESTART,
                payload=payload,
                timestamp=timestamp,
                connection=conn,
            )
            if changed:
                first = next_revision(self, conn)
                persisted = []
                for participant in changed:
                    current = self._participants.get(participant.id, connection=conn)
                    assert current is not None
                    persisted.append(current)
                self.journal.append_group(
                    active_unit,
                    [
                        participant_event(
                            self,
                            participant,
                            conn,
                            revision=first + index,
                            recorded_at=terminated_at,
                        )
                        for index, participant in enumerate(persisted)
                    ],
                )
            if listeners:
                row = self._bus_row(
                    row_id,
                    timestamp,
                    None,
                    None,
                    BUS_KIND_TMUX_SERVER_RESTART,
                    json.dumps(payload),
                )
                active_unit.after_commit(lambda: self._notify_bus_listeners([row], listeners))
            return row_id

        if unit is not None:
            return record(unit)
        with self.write_unit() as owned_unit:
            return record(owned_unit)

    def touch(self, pid: str) -> None:
        self._participants.touch(pid)

    def clear_resume_floor(self, pid: str) -> None:
        """Clear the resume floor column without touching any other field."""
        self._participants.clear_resume_floor(pid)

    def set_source_checkpoint(self, pid: str, checkpoint: str) -> None:
        self._participants.set_source_checkpoint(pid, checkpoint)

    def reparent_participant(self, pid: str, *, new_parent_id: str) -> None:
        """Set the parent_id of a participant."""
        with self.write_unit() as unit:
            participant = self._participants.get(pid, connection=unit.connection)
            if participant is None or participant.parent_id == new_parent_id:
                return
            self._participants.reparent(
                pid,
                new_parent_id=new_parent_id,
                connection=unit.connection,
            )
            persisted = self._participants.get(pid, connection=unit.connection)
            assert persisted is not None
            self.journal.append_group(
                unit,
                [
                    participant_event(
                        self,
                        persisted,
                        unit.connection,
                        revision=next_revision(self, unit.connection),
                        recorded_at=now(),
                    )
                ],
            )

    def reparent_in_connection(self, pid: str, *, new_parent_id: str, connection) -> None:
        """Set parent_id inside a caller-owned write unit; the caller journals."""
        self._participants.reparent(pid, new_parent_id=new_parent_id, connection=connection)

    def live_participants_in_cwd(self, cwd: str) -> list[Participant]:
        return self._participants.live_in_cwd(cwd)

    def live_count(self) -> int:
        """Count of participants whose status is not DEAD."""
        return self._participants.live_count()

    def addressable_count(self) -> int:
        """Count of participants matching ``Participant.addressable`` exactly."""
        return self._participants.addressable_count()

    def bind_operator_transcript(
        self,
        *,
        target: Participant,
        prior_owner: Participant | None,
        audit_payload: dict,
    ) -> int:
        """Move transcript ownership and append the audit row atomically."""
        target_values = ParticipantRepository._participant_values(target)
        listeners = tuple(self._bus_listeners)
        listener_rows: list[dict] = []
        with self.write_unit() as unit:
            conn = unit.connection
            if prior_owner is not None:
                conn.execute(
                    update(participants)
                    .where(participants.c.id == prior_owner.id)
                    .values(
                        session_id=None,
                        session_correlation=None,
                        transcript_location=None,
                    )
                )
                unbind_payload = {
                    "actor_surface": "cli",
                    "target": prior_owner.id,
                    "transferred_to": target.id,
                    "path": audit_payload.get("path"),
                }
                unbind_ts = now()
                unbind_payload_text = json.dumps(unbind_payload)
                unbind_result = conn.execute(
                    insert(bus).values(
                        ts=unbind_ts,
                        from_id="cli",
                        to_id=prior_owner.id,
                        kind=BUS_KIND_OPERATOR_TRANSCRIPT_UNBIND,
                        payload=unbind_payload_text,
                    )
                )
                if listeners:
                    unbind_pk = unbind_result.inserted_primary_key
                    assert unbind_pk is not None
                    listener_rows.append(
                        self._bus_row(
                            unbind_pk[0],
                            unbind_ts,
                            "cli",
                            prior_owner.id,
                            BUS_KIND_OPERATOR_TRANSCRIPT_UNBIND,
                            unbind_payload_text,
                        )
                    )
            conn.execute(
                sqlite_insert(participants)
                .values(**target_values)
                .on_conflict_do_update(
                    index_elements=[participants.c.id],
                    set_={k: v for k, v in target_values.items() if k != "id"},
                )
            )
            bind_ts = now()
            bind_payload_text = json.dumps(audit_payload)
            result = conn.execute(
                insert(bus).values(
                    ts=bind_ts,
                    from_id="cli",
                    to_id=target.id,
                    kind=BUS_KIND_OPERATOR_TRANSCRIPT_BIND,
                    payload=bind_payload_text,
                )
            )
            pk = result.inserted_primary_key
            assert pk is not None
            if listeners:
                listener_rows.append(
                    self._bus_row(
                        pk[0],
                        bind_ts,
                        "cli",
                        target.id,
                        BUS_KIND_OPERATOR_TRANSCRIPT_BIND,
                        bind_payload_text,
                    )
                )
            event_participants = []
            if prior_owner is not None:
                current_prior = self._participants.get(prior_owner.id, connection=conn)
                if current_prior is not None:
                    event_participants.append(current_prior)
            current_target = self._participants.get(target.id, connection=conn)
            if current_target is not None:
                current_target.name = target.name
                event_participants.append(current_target)
            if event_participants:
                first = next_revision(self, conn)
                self.journal.append_group(
                    unit,
                    [
                        participant_event(
                            self,
                            participant,
                            conn,
                            revision=first + index,
                            recorded_at=bind_ts,
                        )
                        for index, participant in enumerate(event_participants)
                    ],
                )
            if listeners:
                unit.after_commit(lambda: self._notify_bus_listeners(listener_rows, listeners))
        return pk[0]
