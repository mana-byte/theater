"""Participant activity: active-job selectors, idleness, and busy refusals."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from theater.daemon.controls._host import ControlHost
from theater.daemon.controls.busy import BusyAction, BusyOperation, BusyRefusal, busy_refusal
from theater.daemon.controls.routing import ControlRoute
from theater.daemon.persistence.repositories.control_operations import (
    ControlOperation,
    ControlOperationAmbiguityError,
)
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    ControlTransport,
    RuntimeCapability,
    RuntimeExecutionState,
    RuntimeSnapshot,
)
from theater.models import AwaitingDecision, Job, JobState

logger = logging.getLogger("theater.daemon.controls")


@dataclass(frozen=True, slots=True)
class BusyFacts:
    """The snapshot and store facts one busy-refusal walk consults."""

    idle: bool
    connected: bool
    identified: bool
    active: bool
    queued: int
    barrier: bool
    running_handle: str | None = None


class ActivityControls(ControlHost):
    """Answer what a participant is doing and refuse controls while busy."""

    def active_jobs(self, participant_id: str) -> list[Job]:
        """Running jobs actually delivered to the participant, oldest first."""
        return self._store.active_running_jobs_for_target(participant_id)

    def project_action(
        self,
        participant_id: str,
        capability: RuntimeCapability,
        *,
        route: ControlRoute,
        route_available: bool,
        alive: bool,
        presence: str,
        presence_detail: str | None = None,
        connection=None,
    ) -> dict[str, object]:
        """Project cached action availability without entering the mutation service."""
        return self._projection.project_action(
            participant_id,
            capability,
            route=route,
            route_available=route_available,
            alive=alive,
            presence=presence,
            presence_detail=presence_detail,
            connection=connection,
        )

    def active_job_for_native_turn(
        self,
        participant_id: str,
        *,
        backend_generation: int,
        native_session_id: str,
        native_turn_id: str,
        connection=None,
    ) -> Job | None:
        """The exact running job bound to one native turn, or ``None``."""
        operation = self._operation_for_turn(
            participant_id=participant_id,
            backend_generation=backend_generation,
            native_session_id=native_session_id,
            native_turn_id=native_turn_id,
            connection=connection,
        )
        if operation is None or operation.job_handle is None:
            return None
        job = self._store.get_job(operation.job_handle, connection=connection)
        if job is None or job.state != JobState.RUNNING:
            return None
        return job

    def queued_jobs(self, participant_id: str) -> list[Job]:
        """Pending followup jobs in FIFO order — for exclusion, never completion."""
        jobs: list[Job] = []
        for operation in self._store.queued_control_operations(participant_id):
            if operation.job_handle:
                job = self._store.get_job(operation.job_handle)
                if job is not None and job.state == JobState.RUNNING:
                    jobs.append(job)
        return jobs

    @staticmethod
    def _is_authoritatively_idle(snapshot: RuntimeSnapshot) -> bool:
        """Whether native facts prove it is safe to start a prompt."""
        return (
            snapshot.execution_state is RuntimeExecutionState.IDLE
            and snapshot.health in (ConnectionHealth.CONNECTED, ConnectionHealth.DEGRADED)
            and snapshot.native_session_id is not None
            and snapshot.native_turn_id is None
            and snapshot.pending_interaction is None
        )

    def _clear_execution_barriers_from_idle_snapshot(
        self, participant_id: str, snapshot: RuntimeSnapshot
    ) -> None:
        """Clear only barriers proven idle on their exact backend/session."""
        if not self._is_authoritatively_idle(snapshot):
            return
        for operation in self._store.execution_barrier_control_operations(participant_id):
            if (
                operation.transport is ControlTransport.NATIVE_RUNTIME
                and operation.backend_generation == snapshot.backend_generation
                and operation.native_session_id == snapshot.native_session_id
            ):
                self._clear_execution_barrier_for_operation(operation, preserve_deadline=True)

    def _clear_execution_barrier_for_operation(
        self, operation: ControlOperation, *, preserve_deadline: bool = False
    ) -> None:
        """Release a barrier from exact evidence or exact authoritative idle."""
        if not operation.execution_barrier:
            return
        self._store.set_control_execution_barrier(
            operation.operation_id,
            active=False,
            updated_at=operation.updated_at if preserve_deadline else self._clock(),
        )
        if not preserve_deadline:
            self._deadline_not_before.pop(operation.operation_id, None)

    def _reject_busy(
        self,
        participant_id: str,
        snapshot: RuntimeSnapshot,
        *,
        operation: BusyOperation,
        exclude: str | None = None,
    ) -> None:
        """Authoritative idle/busy check from the runtime snapshot and store."""
        if snapshot.pending_interaction is not None:
            raise AwaitingDecision(
                f"participant {participant_id!r} is waiting for a human to answer "
                f"a native {snapshot.pending_interaction.kind.value}; only the "
                "native UI may answer it — not Theater, not the caller"
            )
        refusal = self._busy_action(participant_id, snapshot, exclude=exclude)
        if refusal is None:
            return
        raise busy_refusal(
            participant_id,
            refusal,
            turn=snapshot.native_turn_id,
            operation=operation,
        )

    def _busy_action(
        self, participant_id: str, snapshot: RuntimeSnapshot, *, exclude: str | None
    ) -> BusyRefusal | None:
        """First applicable refusal, walking :class:`BusyAction`'s declared order.

        The loop is the ordering contract: reordering the enum reorders
        selection, and a new member must earn its place in the walk.
        """
        facts = self._busy_facts(participant_id, snapshot, exclude=exclude)
        for action in BusyAction:
            refusal = self._refusal_for(action, facts)
            if refusal is not None:
                return refusal
        return None

    def _busy_facts(
        self, participant_id: str, snapshot: RuntimeSnapshot, *, exclude: str | None
    ) -> BusyFacts:
        """Gather the walk's facts; barrier and job facts only on the idle path."""
        queued = self._store.queued_control_operation_count(participant_id)
        idle = self._is_authoritatively_idle(snapshot)
        barrier = False
        running_handle: str | None = None
        if idle and not queued:
            self._clear_execution_barriers_from_idle_snapshot(participant_id, snapshot)
            barrier = self._store.has_execution_barrier(participant_id)
            if not barrier:
                active = self._store.active_running_jobs_for_target(participant_id)
                if exclude is not None:
                    active = [job for job in active if job.handle != exclude]
                if active:
                    running_handle = active[0].handle
        return BusyFacts(
            idle=idle,
            connected=snapshot.health
            not in (ConnectionHealth.DISCONNECTED, ConnectionHealth.UNOPENED),
            identified=snapshot.native_session_id is not None,
            active=snapshot.execution_state is RuntimeExecutionState.ACTIVE,
            queued=queued,
            barrier=barrier,
            running_handle=running_handle,
        )

    def _refusal_for(self, action: BusyAction, facts: BusyFacts) -> BusyRefusal | None:
        """One action's applicability; the walk supplies the order."""
        applicable = {
            BusyAction.RESTORE_RUNTIME: not facts.idle and not facts.connected,
            BusyAction.RESTORE_IDENTITY: not facts.idle and not facts.identified,
            BusyAction.RESOLVE_UNKNOWN_STATE: not facts.idle and not facts.active,
            BusyAction.AWAIT_QUEUE: facts.queued > 0,
            BusyAction.AWAIT_TURN_END: not facts.idle,
            BusyAction.AWAIT_BARRIER: facts.idle and facts.barrier,
            BusyAction.AWAIT_JOBS: facts.running_handle is not None,
        }
        if not applicable[action]:
            return None
        return BusyRefusal(action, queued=facts.queued, running_handle=facts.running_handle)

    def _operation_for_snapshot_turn(self, participant_id: str, snapshot: RuntimeSnapshot):
        if snapshot.native_session_id is None or snapshot.native_turn_id is None:
            return None
        return self._operation_for_turn(
            participant_id=participant_id,
            backend_generation=snapshot.backend_generation,
            native_session_id=snapshot.native_session_id,
            native_turn_id=snapshot.native_turn_id,
        )

    def _operation_for_turn(
        self,
        *,
        participant_id: str,
        backend_generation: int,
        native_session_id: str,
        native_turn_id: str,
        connection=None,
    ):
        """The exact-turn lookup — the only job-to-turn mapping there is."""
        try:
            return self._store.control_operation_for_native_turn(
                participant_id=participant_id,
                backend_generation=backend_generation,
                native_session_id=native_session_id,
                native_turn_id=native_turn_id,
                connection=connection,
            )
        except ControlOperationAmbiguityError as exc:
            # A duplicate job-bearing mapping is a bug state; failing closed
            # means completing nothing, never guessing.
            logger.error(  # noqa: TRY400 - a controlled fail-closed, not a crash
                "native turn %s of %s maps to multiple job-bearing operations; failing closed: %s",
                native_turn_id,
                participant_id,
                exc,
            )
            return None
