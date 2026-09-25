"""Spawn and adoption reservation rollback."""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import select, update

from theater.daemon.operations.projection import operation_event_payload
from theater.daemon.schema import jobs as jobs_table
from theater.daemon.schema import launch_reservations
from theater.daemon.spawning.provider_launch_parts._host import ParticipantLaunchHost
from theater.daemon.worktrees.service import (
    WorkspaceReservation,
)
from theater.models import (
    Job,
    JobState,
    JournalEventRecord,
    ParticipantOrigin,
    Status,
    WorkspaceState,
    WorkspaceUsageHolderKind,
    now,
)


class SpawnRollback(ParticipantLaunchHost):
    async def _rollback_spawn_reservation(
        self,
        operation_id: str,
        participant_id: str,
        workspace_value: object,
        *,
        error_code: str,
        definitive_refusal: bool = False,
    ) -> None:
        timestamp = now()
        created_workspace_id: str | None = None
        with self.store.write_unit() as unit:
            launch = unit.connection.execute(
                select(launch_reservations).where(
                    launch_reservations.c.operation_id == operation_id,
                    launch_reservations.c.participant_id == participant_id,
                )
            ).first()
            if launch is None:
                raise RuntimeError("launch reservation disappeared during rollback")
            if launch._mapping["dispatch_marker"] is not None and not definitive_refusal:
                return
            operation = self._clear_provider_dispatch_target(
                operation_id, unit, timestamp=timestamp
            )
            events: list[JournalEventRecord] = [
                JournalEventRecord(
                    kind="operation.updated",
                    entity_id=operation.operation_id,
                    entity_revision=0,
                    payload=operation_event_payload(operation),
                    recorded_at=timestamp,
                )
            ]
            participant = self._participant_in_connection(participant_id, unit.connection)
            if participant is not None and participant.status is not Status.DEAD:
                participant.status = Status.DEAD
                participant.termination_reason = "spawn_failed"
                participant.terminated_at = timestamp
                participant.last_activity = timestamp
                self.registry.persist_in_connection(participant, unit.connection)
                events.append(
                    self._participant_event(
                        participant,
                        timestamp,
                        revision=0,
                        connection=unit.connection,
                    )
                )
            job_row = unit.connection.execute(
                select(jobs_table).where(jobs_table.c.handle == participant_id)
            ).first()
            if job_row is not None:
                job = Job.from_row(job_row._mapping)
                if job.state == JobState.RUNNING.value:
                    job = replace(
                        job,
                        state=JobState.CRASHED.value,
                        error_code=error_code,
                        finished_at=timestamp,
                    )
                    unit.connection.execute(
                        update(jobs_table)
                        .where(
                            jobs_table.c.handle == participant_id,
                            jobs_table.c.state == JobState.RUNNING.value,
                        )
                        .values(
                            state=JobState.CRASHED.value,
                            error_code=error_code,
                            finished_at=timestamp,
                        )
                    )
                    events.append(self._job_event(job, timestamp, revision=0))
            usage_id = launch._mapping["workspace_usage_id"]
            if usage_id is None and isinstance(workspace_value, WorkspaceReservation):
                usage_id = workspace_value.usage.usage_id
            if isinstance(usage_id, str):
                usage = self.store.workspaces.get_usage(usage_id, connection=unit.connection)
                if (
                    usage is not None
                    and usage.released_at is None
                    and usage.holder_kind == WorkspaceUsageHolderKind.RESERVATION.value
                    and usage.holder_id == operation_id
                    and self.store.workspaces.release_usage(
                        usage_id,
                        released_at=timestamp,
                        reason="launch_rolled_back",
                        connection=unit.connection,
                    )
                ):
                    released = replace(
                        usage,
                        released_at=timestamp,
                        release_reason="launch_rolled_back",
                    )
                    events.append(
                        self._workspace_usage_event(
                            released,
                            timestamp,
                            revision=0,
                            connection=unit.connection,
                        )
                    )
                    workspace = self.store.workspaces.get(
                        usage.workspace_id, connection=unit.connection
                    )
                    if (
                        workspace is not None
                        and workspace.creation_operation_id == operation_id
                        and workspace.state == WorkspaceState.ACTIVE.value
                    ):
                        created_workspace_id = workspace.workspace_id
            unit.connection.execute(
                update(launch_reservations)
                .where(launch_reservations.c.operation_id == operation_id)
                .values(phase="rolled_back", updated_at=timestamp)
            )
            first = self.store.journal.current_sequence(connection=unit.connection) + 1
            events = [
                replace(event, entity_revision=first + index) for index, event in enumerate(events)
            ]
            if events:
                self.store.journal.append_group(unit, events)

            def finish_job() -> None:
                self.daemon.jobs.finish(
                    participant_id,
                    state=JobState.CRASHED,
                    error_code=error_code,
                )

            unit.after_commit(finish_job)
            unit.after_commit(lambda: self.registry.mark_dead(participant_id))
        if created_workspace_id is not None:
            await self.workspaces.rollback_created_reservation(
                workspace_id=created_workspace_id,
                reservation_id=operation_id,
            )

    def _rollback_adoption_reservation(self, participant_id: str) -> None:
        timestamp = now()
        with self.store.write_unit() as unit:
            participant = self._participant_in_connection(participant_id, unit.connection)
            if participant is None or participant.origin is not ParticipantOrigin.ADOPTED:
                return
            if self.store.terminal_bindings.get(participant_id, connection=unit.connection):
                return
            participant.status = Status.DEAD
            participant.termination_reason = "adoption_failed"
            participant.terminated_at = timestamp
            participant.last_activity = timestamp
            self.registry.persist_in_connection(participant, unit.connection)
            revision = self.store.journal.current_sequence(connection=unit.connection) + 1
            self.store.journal.append_group(
                unit,
                [
                    self._participant_event(
                        participant,
                        timestamp,
                        revision=revision,
                        connection=unit.connection,
                    )
                ],
            )
            unit.after_commit(lambda: self.registry.mark_dead(participant_id))
