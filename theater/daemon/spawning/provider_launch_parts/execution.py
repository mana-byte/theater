"""Detached provider launch execution and terminal binding."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace

from sqlalchemy import select, update

from theater import timing
from theater.daemon.operations import (
    DispatchIntent,
    OperationAcceptance,
    OperationOutcome,
)
from theater.daemon.persistence.repositories._json import decode_json, encode_json
from theater.daemon.persistence.transactions import WriteUnit
from theater.daemon.schema import jobs as jobs_table
from theater.daemon.schema import launch_reservations
from theater.daemon.spawning.models import (
    ProviderLaunchOutcome,
    ProviderLaunchSelection,
    Reservation,
    SpawnRequest,
)
from theater.daemon.spawning.planning import resolve_launch_command
from theater.daemon.spawning.provider_launch_parts._host import ParticipantLaunchHost
from theater.daemon.terminals import ProviderUnavailable, TerminalIdentityMismatch
from theater.daemon.worktrees.service import (
    WorkspaceReservation,
)
from theater.harness.base import ResumeLaunchOverlay
from theater.models import (
    BadRequest,
    Job,
    JobState,
    Participant,
    PublicOperationRecord,
    WorkspaceUsageHolderKind,
    WorkspaceUsageRecord,
    new_id,
    now,
)
from theater.observability.catalog import LIFECYCLE_STAGE


class SpawnExecution(ParticipantLaunchHost):
    def _start_spawn(
        self,
        acceptance: OperationAcceptance,
        captured: dict[str, object],
        params: Mapping[str, object],
        participant: Participant,
    ) -> None:
        provider_generation = captured["provider_generation"]
        assert type(provider_generation) is int

        async def side_effect() -> OperationOutcome:
            try:
                workspace = captured["workspace_reservation"]
                assert isinstance(workspace, WorkspaceReservation)
                with timing.span(
                    LIFECYCLE_STAGE,
                    action="spawn",
                    stage="workspace",
                    id=participant.id,
                    operation_id=acceptance.record.operation_id,
                ):
                    workspace = await self.workspaces.materialize_creation(
                        workspace, reservation_id=acceptance.record.operation_id
                    )
                captured["workspace_reservation"] = workspace
                self._mark_workspace_ready(acceptance.record.operation_id, workspace)
                self.daemon.jobs.replace_touch_accumulator(
                    participant.id, cwd=workspace.workspace.path
                )
                request = captured["request"]
                assert isinstance(request, SpawnRequest)
                request = replace(request, cwd=workspace.workspace.path, worktree=False)
                resume_predecessor = captured["resume_predecessor"]
                assert resume_predecessor is None or isinstance(resume_predecessor, Participant)
                resume_overlay = captured["resume_overlay"]
                assert resume_overlay is None or isinstance(resume_overlay, ResumeLaunchOverlay)
                provider = ProviderLaunchSelection(
                    provider_id=str(captured["provider_id"]),
                    provider_generation=provider_generation,
                    operation_id=acceptance.record.operation_id,
                    launch_id=acceptance.record.operation_id,
                    terminal_service=self.terminals,
                    mark_dispatched=lambda: self._mark_launch_dispatched(
                        acceptance.record.operation_id,
                        provider_generation,
                    ),
                    bind_terminal=lambda terminal: self._bind_spawn(
                        acceptance.record.operation_id,
                        participant.id,
                        workspace.usage.usage_id,
                        terminal,
                    ),
                )
                with timing.span(
                    LIFECYCLE_STAGE,
                    action="spawn",
                    stage="prepare",
                    id=participant.id,
                    operation_id=acceptance.record.operation_id,
                ):
                    reservation = await self.spawner.prepare_provider_launch(
                        request,
                        participant,
                        child_cwd=workspace.workspace.path,
                        provider=provider,
                        workspace_usage_id=workspace.usage.usage_id,
                        resume_predecessor=resume_predecessor,
                        resume_overlay=resume_overlay,
                        prevalidated=True,
                    )
                self._record_launch_plan(acceptance.record.operation_id, reservation)
                self._persist_provider_dispatch_target(
                    acceptance.record.operation_id,
                    str(captured["provider_id"]),
                    provider_generation,
                )
                with timing.span(
                    LIFECYCLE_STAGE,
                    action="spawn",
                    stage="launch",
                    id=participant.id,
                    operation_id=acceptance.record.operation_id,
                ):
                    attached = await self.spawner.launch(reservation)
                return OperationOutcome.succeeded(
                    phase="terminal_bound",
                    result={"participant_id": attached.id, "job_handle": attached.id},
                )
            except ProviderLaunchOutcome as exc:
                if exc.outcome.state == "failed":
                    await self._rollback_spawn_reservation(
                        acceptance.record.operation_id,
                        participant.id,
                        captured.get("workspace_reservation"),
                        error_code=self._outcome_error_code(exc.outcome),
                        definitive_refusal=True,
                    )
                return exc.outcome
            except (BadRequest, ProviderUnavailable) as exc:
                if not self._launch_was_dispatched(acceptance.record.operation_id):
                    await self._rollback_spawn_reservation(
                        acceptance.record.operation_id,
                        participant.id,
                        captured.get("workspace_reservation"),
                        error_code=exc.code,
                    )
                    return OperationOutcome.failed(
                        phase="launch_refused",
                        error={"code": exc.code, "message": str(exc)},
                    )
                return OperationOutcome.uncertain(
                    phase="terminal_create_outcome_unknown",
                    error={
                        "code": exc.code,
                        "message": "terminal creation may have executed; reconcile before retrying",
                    },
                )
            except TerminalIdentityMismatch as exc:
                return OperationOutcome.uncertain(
                    phase="provider_identity_uncertain",
                    error={"code": exc.code, "message": str(exc)},
                )
            except Exception as exc:
                if not self._launch_was_dispatched(acceptance.record.operation_id):
                    await self._rollback_spawn_reservation(
                        acceptance.record.operation_id,
                        participant.id,
                        captured.get("workspace_reservation"),
                        error_code="internal",
                    )
                    return OperationOutcome.failed(
                        phase="launch_preparation_failed",
                        error={"code": "internal", "message": str(exc)},
                    )
                return OperationOutcome.uncertain(
                    phase="terminal_create_outcome_unknown",
                    error={
                        "code": "internal",
                        "message": "terminal creation may have executed; reconcile before retrying",
                        "details": {"reason": type(exc).__name__},
                    },
                )
            except asyncio.CancelledError:
                raise

        self.operations.start(
            acceptance.record.operation_id,
            dispatch=DispatchIntent(phase="launch_preparing"),
            side_effect=side_effect,
        )

    def _record_launch_plan(self, operation_id: str, reservation: Reservation) -> None:
        plan = reservation.plan
        provider = reservation.provider
        assert provider is not None
        artifacts = tuple(str(path) for path in (*plan.files.keys(), *plan.private_files.keys()))
        with self.store.write_unit() as unit:
            encoded_facts = unit.connection.execute(
                select(launch_reservations.c.launch_facts).where(
                    launch_reservations.c.operation_id == operation_id
                )
            ).scalar_one()
            stored_facts = decode_json(str(encoded_facts))
            if not isinstance(stored_facts, Mapping):
                raise TypeError("stored launch facts are not an object")
            facts = {
                **stored_facts,
                "provider_generation": provider.provider_generation,
                "cwd": reservation.child_cwd,
                "argv": list(resolve_launch_command(plan)),
                "environment_keys": sorted({*plan.env, "THEATER_ID"}),
                "native": reservation.native is not None,
                "workspace_id": reservation.participant.workspace_id,
            }
            unit.connection.execute(
                update(launch_reservations)
                .where(launch_reservations.c.operation_id == operation_id)
                .values(
                    phase="plan_ready",
                    launch_facts=encode_json(facts),
                    artifact_refs=encode_json(list(artifacts)),
                    updated_at=now(),
                )
            )

    def _mark_workspace_ready(self, operation_id: str, reservation: WorkspaceReservation) -> None:
        with self.store.write_unit() as unit:
            changed = unit.connection.execute(
                update(launch_reservations)
                .where(
                    launch_reservations.c.operation_id == operation_id,
                    launch_reservations.c.dispatch_marker.is_(None),
                )
                .values(
                    workspace_usage_id=reservation.usage.usage_id,
                    phase="workspace_ready",
                    updated_at=now(),
                )
            )
            if changed.rowcount != 1:
                raise RuntimeError("workspace launch reservation changed before preparation")

    def _persist_provider_dispatch_target(
        self, operation_id: str, provider_id: str, generation: int
    ) -> None:
        """Fence terminal creation to its provider before the callback write."""
        self.operations.mark_provider_dispatch_target(
            operation_id,
            provider_id=provider_id,
            provider_generation=generation,
            phase="terminal_create_pending",
        )

    def _clear_provider_dispatch_target(
        self, operation_id: str, unit: WriteUnit, *, timestamp: float
    ) -> PublicOperationRecord:
        operation = self.store.operations.get(operation_id, connection=unit.connection)
        if operation is None:
            raise RuntimeError("public operation disappeared during launch rollback")
        updated = replace(
            operation,
            dispatch_provider_id=None,
            dispatch_provider_generation=None,
            updated_at=timestamp,
        )
        if not self.store.operations.replace(
            updated,
            expected_state=operation.state,
            expected_updated_at=operation.updated_at,
            connection=unit.connection,
        ):
            raise RuntimeError("public operation changed during launch rollback")
        return updated

    def _launch_was_dispatched(self, operation_id: str) -> bool:
        marker = self.store.conn.execute(
            select(launch_reservations.c.dispatch_marker).where(
                launch_reservations.c.operation_id == operation_id
            )
        ).scalar_one_or_none()
        return marker is not None

    @staticmethod
    def _outcome_error_code(outcome: OperationOutcome) -> str:
        if outcome.error is not None and isinstance(outcome.error.get("code"), str):
            return str(outcome.error["code"])
        return "provider_unavailable"

    def _mark_launch_dispatched(self, operation_id: str, generation: int) -> None:
        with self.store.write_unit() as unit:
            changed = unit.connection.execute(
                update(launch_reservations)
                .where(
                    launch_reservations.c.operation_id == operation_id,
                    launch_reservations.c.dispatch_marker.is_(None),
                )
                .values(
                    phase="terminal_create_dispatched",
                    dispatch_marker=f"generation:{generation}",
                    updated_at=now(),
                )
            )
            if changed.rowcount != 1:
                raise RuntimeError("launch reservation was already dispatched")

    def _bind_spawn(
        self,
        operation_id: str,
        participant_id: str,
        reservation_usage_id: str,
        terminal: Mapping[str, object],
    ) -> Participant:
        with self.store.write_unit() as unit:
            timestamp = now()
            participant = self._participant_in_connection(participant_id, unit.connection)
            if participant is None:
                raise RuntimeError("reserved participant disappeared before terminal binding")
            binding = self._binding(participant_id, terminal)
            self._ensure_terminal_unbound(binding, unit.connection)
            self.store.terminal_bindings.bind(binding, connection=unit.connection)
            usage = self.store.workspaces.get_usage(
                reservation_usage_id, connection=unit.connection
            )
            if usage is None:
                raise RuntimeError("workspace reservation disappeared before terminal binding")
            participant_usage = WorkspaceUsageRecord(
                usage_id=new_id(),
                workspace_id=usage.workspace_id,
                holder_kind=WorkspaceUsageHolderKind.PARTICIPANT.value,
                holder_id=participant_id,
                acquired_at=now(),
            )
            self.store.workspaces.handoff_usage(
                reservation_usage_id=reservation_usage_id,
                participant_usage=participant_usage,
                handed_off_at=participant_usage.acquired_at,
                connection=unit.connection,
            )
            participant.workspace_id = usage.workspace_id
            self.registry.persist_in_connection(participant, unit.connection)
            completed_job = self._finish_promptless_job(
                participant_id,
                timestamp=timestamp,
                connection=unit.connection,
            )
            operation = self._persist_dispatch_identity(operation_id, terminal, unit)
            unit.connection.execute(
                update(launch_reservations)
                .where(launch_reservations.c.operation_id == operation_id)
                .values(phase="terminal_bound", updated_at=now())
            )
            self._append_binding_events(
                unit,
                participant,
                binding,
                participant_usage,
                operation=operation,
                completed_job=completed_job,
            )
            if completed_job is not None:
                unit.after_commit(lambda: self.daemon.jobs.notify_committed_finish(completed_job))
        return participant

    def _finish_promptless_job(
        self,
        participant_id: str,
        *,
        timestamp: float,
        connection,
    ) -> Job | None:
        row = connection.execute(
            select(jobs_table).where(jobs_table.c.handle == participant_id)
        ).first()
        if row is None:
            return None
        job = Job.from_row(row._mapping)
        if job.state != JobState.RUNNING.value or job.prompt:
            return None
        completed = replace(job, state=JobState.DONE.value, result="", finished_at=timestamp)
        changed = connection.execute(
            update(jobs_table)
            .where(
                jobs_table.c.handle == participant_id,
                jobs_table.c.state == JobState.RUNNING.value,
            )
            .values(state=JobState.DONE.value, result="", finished_at=timestamp)
        )
        if changed.rowcount != 1:
            raise RuntimeError("promptless spawn job changed before terminal binding")
        return completed
