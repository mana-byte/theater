"""Read-only action projection from cached route, participant, and queue facts."""

from collections.abc import Callable

from theater.constants.daemon import CONTROL_QUEUE_MAX_PENDING
from theater.daemon.control_projection import project_control_action
from theater.daemon.controls.gates import ControlGates
from theater.daemon.controls.routing import ControlRoute
from theater.daemon.persistence.store import Store
from theater.harness.contracts.runtime import (
    RuntimeCapability,
    RuntimeExecutionState,
    RuntimeSettingField,
)
from theater.models import Job, Status


class ControlActionProjector:
    def __init__(
        self,
        store: Store,
        gates: ControlGates,
        active_job_for_native_turn: Callable[..., Job | None],
    ) -> None:
        self._store = store
        self._gates = gates
        self._active_job_for_native_turn = active_job_for_native_turn

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
        """Project one action from the same cached gates mutation paths enforce."""
        reason, detail = self._project_action_block(
            participant_id, capability, route, connection=connection
        )
        return project_control_action(
            route,
            capability,
            route_available=route_available,
            alive=alive,
            presence=presence,
            presence_detail=presence_detail,
            blocked_reason=reason,
            blocked_detail=detail,
        )

    def _project_action_block(
        self,
        participant_id: str,
        capability: RuntimeCapability,
        route: ControlRoute,
        *,
        connection=None,
    ) -> tuple[str | None, str | None]:
        """Return a fail-closed action-specific block without runtime I/O or mutation."""
        if capability is RuntimeCapability.SEND:
            participant = self._store.get_participant(participant_id, connection=connection)
            if participant is not None:
                reason, detail = self._gates.project_send_preflight(participant)
                if reason is not None:
                    return reason, detail
            return self._project_idle_action_block(participant_id, route, connection=connection)
        if capability is RuntimeCapability.QUEUE_FOLLOWUP:
            return self._project_queue_block(participant_id, connection=connection)
        if capability is RuntimeCapability.STEER:
            return self._project_steer_block(participant_id, route, connection=connection)
        if capability is RuntimeCapability.INTERRUPT:
            return None, None
        if capability is RuntimeCapability.SETTINGS_UPDATE and route.is_native:
            admission = route.native_admission
            supported = None if admission is None else admission.get("supported_settings")
            if not isinstance(supported, (set, frozenset)) or not supported:
                return "unsupported", "the runtime exposes no mutable settings fields"
            blocked = self._project_idle_action_block(participant_id, route, connection=connection)
            if blocked[0] is not None:
                return blocked
            participant = self._store.get_participant(participant_id, connection=connection)
            allowlists = (
                None
                if participant is None
                else self._gates.settings_allowlists(participant.harness)
            )
            if allowlists is not None:
                models, reasoning = allowlists
                has_allowed_field = (RuntimeSettingField.MODEL in supported and bool(models)) or (
                    RuntimeSettingField.REASONING_EFFORT in supported and bool(reasoning)
                )
                if not has_allowed_field:
                    return (
                        "unsupported",
                        "no runtime-supported setting field has configured allowable values",
                    )
            return None, None
        return self._project_idle_action_block(participant_id, route, connection=connection)

    def _project_queue_block(
        self, participant_id: str, *, connection=None
    ) -> tuple[str | None, str | None]:
        if (
            self._store.queued_control_operation_count(participant_id, connection=connection)
            >= CONTROL_QUEUE_MAX_PENDING
        ):
            return "busy", "the participant followup queue is full"
        return None, None

    def _project_steer_block(
        self, participant_id: str, route: ControlRoute, *, connection=None
    ) -> tuple[str | None, str | None]:
        if route.is_native:
            admission = route.native_admission
            if admission is None or admission.get("native_turn_id") is None:
                return "stale_target", "there is no current native turn to steer"
            binding = self._store.get_runtime_binding(participant_id, connection=connection)
            if binding is None or binding.native_session_id is None:
                return "stale_target", "the native session identity is unavailable"
            job = self._active_job_for_native_turn(
                participant_id,
                backend_generation=binding.backend_generation,
                native_session_id=binding.native_session_id,
                native_turn_id=str(admission["native_turn_id"]),
                connection=connection,
            )
            if job is None:
                return "stale_target", "the active native turn has no running Theater job"
        elif (
            route.is_provider
            and len(
                self._store.active_running_jobs_for_target(participant_id, connection=connection)
            )
            != 1
        ):
            return "stale_target", "there is not exactly one running Theater job to steer"
        return None, None

    def _project_idle_action_block(
        self, participant_id: str, route: ControlRoute, *, connection=None
    ) -> tuple[str | None, str | None]:
        if route.is_native:
            admission = route.native_admission
            if admission is None:
                return "busy", "the native execution state has not been observed"
            if admission.get("pending_interaction") is not None:
                return "awaiting_decision", "the native UI is waiting for a human decision"
            if (
                admission.get("execution_state") is not RuntimeExecutionState.IDLE
                or admission.get("native_turn_id") is not None
            ):
                return "busy", "the native runtime has not authoritatively reported idle"
        else:
            participant = self._store.get_participant(participant_id, connection=connection)
            if participant is not None and participant.status is Status.WORKING:
                return "busy", "the participant is working"
        queued = self._store.queued_control_operation_count(participant_id, connection=connection)
        if queued:
            return "busy", "queued followups must drain before this action"
        if self._store.has_execution_barrier(participant_id, connection=connection):
            return "busy", "an unresolved delivery still blocks this action"
        if self._store.active_running_jobs_for_target(participant_id, connection=connection):
            return "busy", "a running send job still blocks this action"
        return None, None
