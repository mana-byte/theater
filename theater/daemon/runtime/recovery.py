"""Restart reconciliation and teardown of persisted runtime bindings.

Fixed order: fail undispatched work; adopt only if pid + start identity verify; reconnect
the exact session (never a cwd guess); consume evidence; close ambiguity without replay.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from theater import timing
from theater.daemon.harness_runtime.errors import BackendIdentityMismatch
from theater.daemon.observation.live import LiveRegistration
from theater.daemon.operations import DispatchIntent, OperationOutcome
from theater.daemon.runtime.evidence import persist_buffered_evidence
from theater.daemon.runtime.public_recovery import (
    fail_proven_undispatched,
    reconcile_workspace_lifecycle,
)
from theater.daemon.spawning.frontend import (
    close_frontend_runtime,
    is_frontend_binding,
    restore_frontend_listener,
)
from theater.daemon.spawning.runtime_identity import validate_runtime_binding
from theater.harness import get as get_harness
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    DeliveryResult,
    RuntimeContext,
    RuntimeLifecyclePhase,
    SessionOpenMode,
)
from theater.models import JobState, Participant, PublicOperationState, Status, now
from theater.observability.catalog import RUNTIME_RECONNECT
from theater.provenance import TranscriptProvenance

logger = logging.getLogger("theater.daemon.runtime")

_ORPHAN_BUS_KIND = "runtime.orphan"
_BACKEND_GONE_BUS_KIND = "runtime.backend_gone"
_BACKEND_GONE_ERROR_CODE = "backend_gone"


class _RecoveryLeaseRevoked(Exception):
    """An in-flight live recovery lost authority while awaiting native I/O."""


def prepare_provider_control_recovery(daemon) -> None:
    """Restore provider-only barriers before native or provider admission."""
    native_ids = {
        binding.participant_id for binding in daemon.store.runtime_bindings_for_recovery()
    }
    provider_ids = {
        participant.id
        for participant in daemon.registry.list()
        if daemon.store.terminal_bindings.get(participant.id) is not None
    }
    provider_only = sorted(provider_ids - native_ids)
    if provider_only:
        daemon.controls.fail_undelivered_followups(provider_only)


async def reconcile_public_control_operations(daemon) -> None:
    """Reconnect public operation state to durable control rows after a crash."""
    await reconcile_workspace_lifecycle(daemon)
    cursor: str | None = None
    while True:
        records, cursor = daemon.store.operations.list_page(
            cursor=cursor,
            limit=500,
            unsettled_only=True,
        )
        for operation in records:
            await _reconcile_public_control_operation(daemon, operation)
        if cursor is None:
            break


async def _reconcile_public_control_operation(daemon, operation) -> None:
    if operation.kind == "workspace_cleanup" and operation.state in {
        PublicOperationState.RUNNING.value,
        PublicOperationState.UNCERTAIN.value,
    }:
        if operation.state == PublicOperationState.RUNNING.value:
            _mark_operation_uncertain(
                daemon,
                operation.operation_id,
                "workspace_cleanup_recovery_pending",
                error_code="daemon_restarted",
                message=(
                    "the daemon restarted after workspace cleanup may have begun; "
                    "durable workspace evidence is required"
                ),
            )
        await daemon.operation_service.reconcile(operation.operation_id)
        return
    launch = (
        daemon.store.operations.get_launch(operation.operation_id)
        if operation.kind == "spawn"
        else None
    )
    if launch is not None and launch.dispatch_marker is not None:
        _restore_launch_provider_target(daemon, operation, launch)
        _mark_operation_uncertain(
            daemon, operation.operation_id, "terminal_create_recovery_pending"
        )
        return
    operation, control = _control_for_public_operation(daemon, operation)
    if control is None:
        if operation.state == PublicOperationState.ACCEPTED.value:
            if not await fail_proven_undispatched(daemon, operation):
                _mark_operation_uncertain(
                    daemon, operation.operation_id, "dispatch_recovery_pending"
                )
            return
        await _mark_crash_ambiguous_provider_operation(daemon, operation)
        return
    _reconcile_public_from_control(daemon, operation, control)


def _reconcile_public_from_control(daemon, operation, control) -> None:
    if control.delivery_phase not in {
        ControlDeliveryPhase.RESERVED,
        ControlDeliveryPhase.QUEUED,
    }:
        operation = _restore_control_dispatch_target(daemon, operation, control)
    if control.delivery_phase is ControlDeliveryPhase.QUEUED:
        operation = _ensure_running_for_recovery(daemon, operation)
        daemon.operation_service.resume(
            operation.operation_id,
            side_effect=lambda: _wait_for_recovered_control(daemon, control.operation_id),
        )
        return
    if control.delivery_phase is ControlDeliveryPhase.RESERVED:
        daemon.controls.fail_undelivered_followups([control.participant_id])
        settled = daemon.store.get_control_operation(control.operation_id)
        if settled is not None and settled.delivery_phase is ControlDeliveryPhase.SETTLED:
            if settled.delivery_result is DeliveryResult.REJECTED:
                _settle_public_from_control(
                    daemon, operation.operation_id, accepted=False, control=settled
                )
            else:
                _mark_operation_uncertain(
                    daemon, operation.operation_id, "delivery_recovery_pending"
                )
        return
    if control.delivery_phase is ControlDeliveryPhase.DISPATCHED:
        _mark_operation_uncertain(daemon, operation.operation_id, "delivery_recovery_pending")
        return
    if control.delivery_result is DeliveryResult.ACCEPTED:
        _settle_public_from_control(daemon, operation.operation_id, accepted=True, control=control)
    elif control.delivery_result is DeliveryResult.REJECTED:
        _settle_public_from_control(daemon, operation.operation_id, accepted=False, control=control)
    else:
        _mark_operation_uncertain(daemon, operation.operation_id, "delivery_recovery_pending")


def _control_for_public_operation(daemon, operation):
    control_id = operation.control_operation_id
    if control_id is None and operation.kind.startswith("controls."):
        candidate = f"{operation.operation_id}:control"
        if daemon.store.get_control_operation(candidate) is not None:
            if operation.state in {
                PublicOperationState.ACCEPTED.value,
                PublicOperationState.RUNNING.value,
            }:
                operation = daemon.operation_service.link(
                    operation.operation_id,
                    phase="control_recovered",
                    control_operation_id=candidate,
                )
            control_id = candidate
    control = None if control_id is None else daemon.store.get_control_operation(control_id)
    return operation, control


def _restore_launch_provider_target(daemon, operation, launch) -> None:
    generation = launch.launch_facts.get("provider_generation")
    if operation.dispatch_provider_id is not None or type(generation) is not int or generation < 0:
        return
    current = _ensure_running_for_recovery(daemon, operation)
    daemon.operation_service.mark_provider_dispatch_target(
        current.operation_id,
        provider_id=launch.provider_id,
        provider_generation=generation,
        phase="terminal_create_dispatch_recovered",
    )


def _restore_control_dispatch_target(daemon, operation, control):
    if operation.state == PublicOperationState.ACCEPTED.value:
        binding = daemon.store.terminal_bindings.get(control.participant_id)
        exact_terminal = binding is not None and (
            binding.provider_id,
            binding.provider_generation,
            binding.terminal_id,
            binding.terminal_incarnation,
        ) == (
            control.provider_id,
            control.provider_generation,
            control.terminal_id,
            control.terminal_incarnation,
        )
        return daemon.operation_service.mark_dispatch_intent(
            operation.operation_id,
            DispatchIntent(
                phase="control_dispatch_recovered",
                provider_id=control.provider_id,
                provider_generation=control.provider_generation,
                terminal_id=control.terminal_id if exact_terminal else None,
                terminal_incarnation=(control.terminal_incarnation if exact_terminal else None),
                occupant_evidence=(binding.occupant_evidence if exact_terminal else None),
                process_facts=(binding.process_facts if exact_terminal else None),
                backend_generation=control.backend_generation,
                native_session_id=control.native_session_id,
                native_turn_id=control.native_turn_id,
            ),
        )
    if (
        operation.dispatch_provider_id is not None
        or control.provider_id is None
        or control.provider_generation is None
    ):
        return operation
    current = _ensure_running_for_recovery(daemon, operation)
    return daemon.operation_service.mark_provider_dispatch_target(
        current.operation_id,
        provider_id=control.provider_id,
        provider_generation=control.provider_generation,
        phase="control_dispatch_recovered",
    )


def _ensure_running_for_recovery(daemon, operation):
    if operation.state == PublicOperationState.ACCEPTED.value:
        return daemon.operation_service.mark_running(
            operation.operation_id, phase="dispatch_recovered"
        )
    return operation


async def _mark_crash_ambiguous_provider_operation(daemon, operation) -> None:
    if operation.state != PublicOperationState.RUNNING.value:
        return
    dispatched = operation.dispatch_provider_id is not None
    if operation.kind == "spawn":
        launch = daemon.store.operations.get_launch(operation.operation_id)
        dispatched = launch is not None and launch.dispatch_marker is not None
        if not dispatched:
            await fail_proven_undispatched(daemon, operation)
            return
    if operation.kind == "adopt":
        participant_id = operation.target_ids[0] if len(operation.target_ids) == 1 else None
        binding = (
            daemon.store.terminal_bindings.get(participant_id)
            if participant_id is not None
            else None
        )
        if binding is not None:
            daemon.operation_service.succeed(
                operation.operation_id,
                phase="terminal_adopted_recovered",
                result={"participant_id": participant_id},
            )
        else:
            await fail_proven_undispatched(daemon, operation)
        return
    if dispatched:
        _mark_operation_uncertain(daemon, operation.operation_id, "provider_recovery_pending")
        return
    _mark_operation_uncertain(
        daemon,
        operation.operation_id,
        "mutation_recovery_pending",
        error_code="daemon_restarted",
        message=(
            "the daemon restarted after mutation execution may have begun; "
            "authoritative completion evidence is required"
        ),
    )


def _mark_operation_uncertain(
    daemon,
    operation_id: str,
    phase: str,
    *,
    error_code: str = "provider_unavailable",
    message: str = "the daemon restarted after dispatch; exact provider evidence is required",
) -> None:
    current = daemon.operation_service.get(operation_id)
    current = _ensure_running_for_recovery(daemon, current)
    if current.state == PublicOperationState.RUNNING.value:
        daemon.operation_service.mark_uncertain(
            operation_id,
            phase=phase,
            error={
                "code": error_code,
                "message": message,
            },
        )


def _settle_public_from_control(daemon, operation_id: str, *, accepted: bool, control) -> None:
    current = daemon.operation_service.get(operation_id)
    if accepted:
        current = _ensure_running_for_recovery(daemon, current)
    if current.state not in {
        PublicOperationState.ACCEPTED.value,
        PublicOperationState.RUNNING.value,
        PublicOperationState.UNCERTAIN.value,
    }:
        return
    if accepted:
        daemon.operation_service.succeed(
            operation_id,
            phase="delivery_recovered",
            result={"delivery": "accepted"},
        )
        return
    daemon.operation_service.fail(
        operation_id,
        phase="delivery_recovered",
        error={
            "code": (control.error_code or "dispatch_failed")[:512],
            "message": (control.error or "the control was rejected before restart")[:8192],
        },
    )


async def _wait_for_recovered_control(daemon, control_id: str) -> OperationOutcome:
    control = await daemon.controls.wait_control_settled(control_id)
    if control is None:
        return OperationOutcome.failed(
            phase="control_missing",
            error={"code": "internal", "message": "control reservation disappeared"},
        )
    if control.delivery_result is DeliveryResult.ACCEPTED:
        return OperationOutcome.succeeded(
            phase="delivery_recovered", result={"delivery": "accepted"}
        )
    if control.delivery_result is DeliveryResult.REJECTED:
        return OperationOutcome.failed(
            phase="delivery_recovered",
            error={
                "code": (control.error_code or "dispatch_failed")[:512],
                "message": (control.error or "the control was rejected")[:8192],
            },
        )
    return OperationOutcome.uncertain(
        phase="delivery_recovery_pending",
        error={
            "code": (control.error_code or "delivery_unknown")[:512],
            "message": (control.error or "the delivery outcome remains unknown")[:8192],
        },
    )


async def reconcile_runtime_bindings(daemon) -> None:
    """Startup: reconcile persisted runtime bindings before ordinary observation."""
    bindings = daemon.store.runtime_bindings_for_recovery()
    if not bindings:
        return
    logger.info(
        "reconciling %d persisted runtime binding(s) before ordinary observation",
        len(bindings),
    )
    frontend = [binding.participant_id for binding in bindings if is_frontend_binding(binding)]
    detached = [binding.participant_id for binding in bindings if not is_frontend_binding(binding)]
    if detached:
        daemon.controls.fail_undelivered_followups(detached)
    if frontend:
        daemon.controls.fail_undelivered_followups(frontend, preserve_legacy_queued=True)
    for binding in bindings:
        try:
            await _reconcile_one_binding(daemon, binding)
        except Exception:
            logger.exception(
                "runtime binding reconciliation failed for %s; the binding is "
                "kept for the next reconciliation",
                binding.participant_id,
            )


async def _reconcile_one_binding(daemon, binding) -> None:
    store = daemon.store
    participant_id = binding.participant_id
    participant = store.get_participant(participant_id)
    if is_frontend_binding(binding):
        if participant is None or participant.status is Status.DEAD:
            await close_frontend_runtime(daemon, participant_id)
            store.delete_runtime_binding(participant_id)
            return
        await restore_frontend_listener(daemon, binding, participant)
        return
    if participant is None or participant.status is Status.DEAD:
        # A dead participant owns no live backend: adopt only to terminate.
        await teardown_participant_runtime(daemon, participant_id, caller_id="cli")
        return
    if binding.backend_pid is None or binding.backend_started_at is None:
        # Pre-identity crash residue: the backend, if any, was launched without
        # its pid ever being persisted, so it can never be safely identified,
        # adopted, or signalled — and no second UI is launched for it.
        _orphan_diagnostic(
            daemon,
            binding,
            "the daemon died before this backend's process identity was "
            "persisted; the backend cannot be safely identified, adopted, or "
            "signalled, and Theater will not launch a second UI for it — "
            f"inspect the private endpoint {binding.endpoint or '(unknown)'} manually",
        )
        return
    try:
        await daemon.runtime_manager.adopt_backend(
            participant_id,
            backend_generation=binding.backend_generation,
            pid=binding.backend_pid,
            started_at=binding.backend_started_at,
            endpoint=binding.endpoint,
        )
    except BackendIdentityMismatch:
        _backend_gone(daemon, binding)
        return
    if binding.native_session_id is None:
        # The backend is alive and verified, but the daemon died before the
        # exact session identity was persisted: the thread it holds cannot be
        # named safely. Ownership is adopted so a later kill can terminate it;
        # no second UI, no cwd-guessing attach, no fabricated session.
        _orphan_diagnostic(
            daemon,
            binding,
            "the verified backend is alive but no native session identity was "
            "ever persisted; its thread cannot be named safely, so Theater "
            "adopts the backend for ownership only and never launches a "
            "second UI for it",
        )
        return
    reconnected = await _reconnect_runtime(daemon, binding, participant)
    if reconnected is None:
        await daemon.controls.reconcile_ambiguous_delivery(participant_id, now_ts=now())
        return
    runtime, manifest = reconnected
    try:
        opened_binding = await runtime.open_session(
            mode=SessionOpenMode.RECONNECT, native_session_id=binding.native_session_id
        )
        validate_runtime_binding(
            store,
            participant_id,
            opened_binding,
            binding.backend_generation,
        )
    except Exception as exc:
        # Exact-session re-adoption failed closed; the backend stays alive and
        # the binding stays for diagnostics. Affected jobs resolve through
        # ambiguous-delivery reconciliation — never by replaying a prompt.
        logger.warning(
            "native session %s of %s could not be re-adopted on the verified "
            "backend: %s; failing closed without a cwd guess",
            binding.native_session_id,
            participant_id,
            exc,
        )
        daemon.store.bus_append(
            _ORPHAN_BUS_KIND,
            to_id=participant_id,
            payload={
                "reason": "native session could not be re-adopted",
                "native_session_id": binding.native_session_id,
                "detail": str(exc),
            },
        )
        # Fail closed but retryable: discard the candidate in place (identity-conditional) so
        # it reads DISCONNECTED and the monitor retries; a stale completion spares the successor.
        if (
            _current_recovery_binding(
                daemon,
                participant_id,
                binding.backend_generation,
                runtime,
                binding.native_session_id,
            )
            is not None
        ):
            await _discard_recovered_candidate(daemon, participant_id, runtime)
        await daemon.controls.reconcile_ambiguous_delivery(participant_id, now_ts=now())
        return
    # Revalidate after the awaited open: a stale success is as stale as a failure and returns
    # without registering or mutating anything the successor owns.
    if (
        _current_recovery_binding(
            daemon,
            participant_id,
            binding.backend_generation,
            runtime,
            binding.native_session_id,
        )
        is None
    ):
        logger.warning(
            "startup re-adoption of %s completed after backend generation %s "
            "was replaced; the stale completion registers nothing",
            participant_id,
            binding.backend_generation,
        )
        return
    await _require_cached_recovered_session(daemon, participant_id, runtime, opened_binding)
    # Register live wiring before consuming stored evidence so held evidence reconciles via the
    # live sink. Failure discards the candidate in place so the monitor retries, like a failed open.
    try:
        _register_live(daemon, binding, runtime, manifest)
    except Exception:
        if (
            _current_recovery_binding(
                daemon,
                participant_id,
                binding.backend_generation,
                runtime,
                binding.native_session_id,
            )
            is not None
        ):
            await _discard_recovered_candidate(daemon, participant_id, runtime)
        raise
    participant.session_id = binding.native_session_id
    participant.session_correlation = str(TranscriptProvenance.EXACT)
    store.upsert_participant(participant)
    # Consume stored evidence first (the recoverable crash window between the
    # evidence commit and the job finish), then reconcile ambiguous delivery.
    daemon.controls.finish_jobs_from_pending_evidence([participant_id])
    await daemon.controls.reconcile_ambiguous_delivery(participant_id, now_ts=now())


async def _reconnect_runtime(daemon, binding, participant: Participant):
    """Create the participant's one runtime for the adopted generation, with its manifest.

    ``None`` (manifest gone): keep the backend and binding, expose a diagnostic, and let
    jobs resolve via the ambiguous-delivery deadline — never by replaying a prompt.
    """
    with timing.span(RUNTIME_RECONNECT, id=binding.participant_id, source="startup_recovery"):
        factory = _runtime_factory(daemon, binding, participant)
        if factory is None:
            _orphan_diagnostic(
                daemon,
                binding,
                f"harness {binding.harness!r} no longer provides a runtime manifest, "
                "so the verified backend cannot be reconnected; the backend is kept "
                "alive and the binding is kept for diagnostics",
            )
            return None
        manifest, create = factory
        runtime = await daemon.runtime_manager.get_or_create(
            binding.participant_id,
            backend_generation=binding.backend_generation,
            create=create,
        )
        return runtime, manifest


def _runtime_factory(daemon, binding, participant: Participant):
    """The manifest plus create callback for one persisted binding.

    Shared by startup and live recovery so every reconnect rebuilds the exact persisted
    context the same way. ``None`` means no runtime manifest anymore.
    """
    from theater.daemon.runtime import wiring as wiring_mod

    try:
        harness = get_harness(binding.harness)
    except Exception:
        harness = None
    manifest = wiring_mod.runtime_manifest_of(harness) if harness is not None else None
    if manifest is None:
        return None
    launch_policy = _launch_policy(binding.launch_policy)
    io = daemon.runtime_io
    token_file = _runtime_token_file(daemon, binding, manifest)

    async def create():
        context = RuntimeContext(
            participant_id=binding.participant_id,
            cwd=participant.cwd,
            io=io,
            backend_generation=binding.backend_generation,
            endpoint=binding.endpoint,
            token_file=token_file,
            approval=launch_policy.get("approval"),
            model=launch_policy.get("model"),
            reasoning_effort=launch_policy.get("reasoning_effort"),
            native_session_id=binding.native_session_id,
        )
        return manifest.factory(context)

    return manifest, create


def _runtime_token_file(daemon, binding, manifest):
    """Locate this binding's persisted runtime credential, if declared.

    Recovery never mints a secret: a missing record fails closed (the backend holds the old one).
    """
    from pathlib import Path

    from theater.daemon.artifacts import ArtifactKind, validate_persisted_path
    from theater.harness.contracts.channels import ChannelKind
    from theater.models import BadRequest

    declaration = manifest.runtime_credential
    if declaration is None:
        return None
    record = daemon.store.get_channel_credential(
        binding.participant_id, ChannelKind.RUNTIME, declaration.channel_id
    )
    if record is None or not getattr(record, "token_path", None):
        raise BadRequest(
            f"the runtime for participant {binding.participant_id!r} declares "
            f"credential {declaration.channel_id!r} but no persisted credential "
            "record exists; recovery cannot mint a new secret the running "
            "backend would reject — inspect the daemon database before resuming"
        )
    token_path = Path(record.token_path)
    validate_persisted_path(token_path, owner_id=binding.participant_id, kind=ArtifactKind.FILE)
    return token_path


async def recover_live_runtime(
    daemon,
    participant_id: str,
    backend_generation: int,
    *,
    recovery_owner: Callable[[str, int], Awaitable[bool]] | None = None,
) -> bool:
    """Same-runtime live recovery after the notification stream disconnected.

    Reuses the backend (never relaunched or signalled), re-adopts the exact session, no replay.
    Ownership is rechecked after every await; a stale completion returns ``False`` untouched.
    """
    store = daemon.store
    binding = store.get_runtime_binding(participant_id)
    if binding is None:
        return False
    if binding.backend_generation != backend_generation:
        # A stale monitor must never recover into the replacement generation.
        return False
    if binding.native_session_id is None:
        return False  # no exact session identity: never guess a re-attach
    participant = store.get_participant(participant_id)
    if participant is None or participant.status is Status.DEAD:
        return False
    expected_session = binding.native_session_id
    with timing.span(RUNTIME_RECONNECT, id=participant_id, source="live_recovery"):
        try:
            await _require_recovery_owner(daemon, recovery_owner, participant_id)
            factory = _runtime_factory(daemon, binding, participant)
            if factory is None:
                return False
            manifest, create = factory
            registration = daemon.observer.live.registration_for(participant_id)
            runtime = await daemon.runtime_manager.reconnect(
                participant_id,
                backend_generation=binding.backend_generation,
                create=create,
            )
            await _require_recovery_owner(daemon, recovery_owner, participant_id, runtime)
            # Revalidate after the awaited reconnect: a replacement may have
            # taken the participant while the candidate was being built.
            binding = _current_recovery_binding(
                daemon, participant_id, backend_generation, runtime, expected_session
            )
            if binding is None:
                return False

            async def validate_evidence_owner() -> None:
                await _require_recovery_owner(daemon, recovery_owner, participant_id, runtime)
                if (
                    _current_recovery_binding(
                        daemon, participant_id, backend_generation, runtime, expected_session
                    )
                    is None
                ):
                    raise _RecoveryLeaseRevoked  # noqa: TRY301

            try:
                await persist_buffered_evidence(
                    registration,
                    backend_generation=backend_generation,
                    native_session_id=expected_session,
                    validate_owner=validate_evidence_owner,
                )
                opened_binding = await runtime.open_session(
                    mode=SessionOpenMode.RECONNECT, native_session_id=expected_session
                )
                validate_runtime_binding(
                    store,
                    participant_id,
                    opened_binding,
                    backend_generation,
                )
            except Exception:
                # The candidate is installed but unusable: disconnect it in
                # place so the monitor retries on its bounded cadence.
                await _discard_recovered_candidate(
                    daemon,
                    participant_id,
                    runtime,
                    recovery_owner=recovery_owner,
                )
                raise
            # Revalidate after the awaited session open and immediately
            # before registration: a stale completion must never register,
            # close, or unregister the successor.
            await _require_recovery_owner(daemon, recovery_owner, participant_id, runtime)
            binding = _current_recovery_binding(
                daemon, participant_id, backend_generation, runtime, expected_session
            )
            if binding is None:
                return False
            if not daemon.runtime_manager.mark_session_open(
                participant_id, runtime, opened_binding
            ):
                await _discard_recovered_candidate(
                    daemon,
                    participant_id,
                    runtime,
                    recovery_owner=recovery_owner,
                )
                return False
            try:
                _register_live(daemon, binding, runtime, manifest)
            except Exception:
                # Registration failed with the candidate current: discard it
                # in place for a bounded retry, never a silent dead runtime.
                await _discard_recovered_candidate(
                    daemon,
                    participant_id,
                    runtime,
                    recovery_owner=recovery_owner,
                )
                raise
        except _RecoveryLeaseRevoked:
            return False
        except Exception as exc:
            logger.warning(
                "live recovery of %s (backend generation %s) failed against the "
                "persisted binding; the backend is kept alive and the monitor "
                "retries on its bounded cadence: %s",
                participant_id,
                backend_generation,
                exc,
            )
            return False
    return True


def _current_recovery_binding(
    daemon, participant_id: str, backend_generation: int, runtime, expected_session: str
):
    """The persisted binding iff the recovered candidate is still exact.

    ``None`` means a replacement owns the participant: never register, close, or unregister.
    """
    binding = daemon.store.get_runtime_binding(participant_id)
    if binding is None:
        return None
    if binding.backend_generation != backend_generation:
        return None
    if binding.native_session_id != expected_session:
        return None
    if daemon.runtime_manager.get(participant_id) is not runtime:
        return None
    return binding


def _recovery_owner_is_current(
    daemon,
    recovery_owner: Callable[[str, int], Awaitable[bool]] | None,
) -> bool:
    """Whether a live-recovery attempt still owns the manager's callback lease."""
    return recovery_owner is None or daemon.runtime_manager.recovery_callback_is_current(
        recovery_owner
    )


async def _require_recovery_owner(
    daemon,
    recovery_owner: Callable[[str, int], Awaitable[bool]] | None,
    participant_id: str,
    runtime=None,
) -> None:
    """Reject revoked recovery and disconnect only its exact candidate."""
    if _recovery_owner_is_current(daemon, recovery_owner):
        return
    if runtime is not None:
        await _discard_recovered_candidate(
            daemon,
            participant_id,
            runtime,
            recovery_owner=recovery_owner,
        )
    raise _RecoveryLeaseRevoked


async def _discard_recovered_candidate(
    daemon,
    participant_id: str,
    runtime,
    *,
    recovery_owner: Callable[[str, int], Awaitable[bool]] | None = None,
) -> None:
    """Disconnect one failed recovery candidate in place: fail-closed, retryable.

    Left installed (removing it would race a replacement) but reading DISCONNECTED so the
    monitor retries; identity-conditional and disconnect-only.
    """
    try:
        if daemon.runtime_manager.get(participant_id) is not runtime:
            return  # a successor owns the participant; the candidate is inert
        await runtime.aclose()
        if _recovery_owner_is_current(daemon, recovery_owner):
            daemon.runtime_manager.mark_disconnected(participant_id, runtime)
    except Exception:
        logger.warning(
            "discarding a failed recovery candidate of %s failed",
            participant_id,
            exc_info=True,
        )


async def _require_cached_recovered_session(daemon, participant_id: str, runtime, binding) -> None:
    """Cache only the candidate that still owns this participant's runtime slot."""
    if daemon.runtime_manager.mark_session_open(participant_id, runtime, binding):
        return
    await _discard_recovered_candidate(daemon, participant_id, runtime)
    raise RuntimeError(
        f"runtime for {participant_id!r} changed before its recovered session was cached"
    )


def live_recovery_callback(daemon) -> Callable[[str, int], Awaitable[bool]]:
    """The generic recovery callback the daemon composition injects.

    The manager stays harness-neutral: it only reports ``(participant_id, generation)``.
    """

    async def recover(participant_id: str, backend_generation: int) -> bool:
        return await recover_live_runtime(
            daemon,
            participant_id,
            backend_generation,
            recovery_owner=recover,
        )

    return recover


def _register_live(daemon, binding, runtime, manifest) -> None:
    """Register the reconnected runtime's live channel with the observer hub.

    Without a live hub the daemon keeps its durable-only behaviour.
    """
    hub = getattr(daemon.observer, "live", None)
    if hub is None:
        return
    hub.register(
        LiveRegistration(
            participant_id=binding.participant_id,
            live_source=runtime.live_source(),
            channel=manifest.channel,
            backend_generation=binding.backend_generation,
            native_session_id=binding.native_session_id,
            evidence_sink=daemon.controls.record_terminal_evidence,
            active_job_for_turn=daemon.controls.active_job_for_native_turn,
        )
    )


def _launch_policy(raw: str | None) -> dict:
    """The persisted launch-policy facts (approval/model selection, no secrets)."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _backend_gone(daemon, binding) -> None:
    """A persisted backend whose pid no longer verifies: fail affected work.

    The process exited (pid possibly reused), so nothing is signalled; jobs fail
    ``backend_gone`` and the participant follows its ordinary lifecycle.
    """
    participant_id = binding.participant_id
    hub = getattr(daemon.observer, "live", None)
    if hub is not None:
        hub.unregister(participant_id)
    for job in daemon.store.running_jobs_for_target(participant_id):
        daemon.jobs.finish(
            job.handle,
            state=JobState.CRASHED,
            result=(
                "The participant's native backend is gone: its verified "
                "process identity no longer matches a live process, so this "
                "job can never complete natively. The work may need to be "
                "re-sent once the participant is healthy."
            ),
            error_code=_BACKEND_GONE_ERROR_CODE,
        )
    store_updated = daemon.store.set_runtime_lifecycle(
        participant_id,
        RuntimeLifecyclePhase.FAILED,
        backend_generation=binding.backend_generation,
        updated_at=now(),
    )
    if not store_updated:
        logger.warning(
            "could not mark the runtime binding of %s failed: the persisted "
            "generation changed; leaving the row for diagnostics",
            participant_id,
        )
    daemon.store.bus_append(
        _BACKEND_GONE_BUS_KIND,
        to_id=participant_id,
        payload={
            "reason": "persisted backend identity no longer verifies",
            "backend_pid": binding.backend_pid,
        },
    )


def _orphan_diagnostic(daemon, binding, detail: str) -> None:
    logger.warning("runtime orphan for %s: %s", binding.participant_id, detail)
    daemon.store.bus_append(
        _ORPHAN_BUS_KIND,
        to_id=binding.participant_id,
        payload={"reason": detail, "lifecycle": str(binding.lifecycle)},
    )


async def teardown_participant_runtime(daemon, participant_id: str, *, caller_id: str) -> bool:
    """Explicit kill / confirmed exit: stop the verified backend; ``True`` allows pane retirement.

    ``False`` keeps the binding for retry (the backend may still use pane/worktree); a pid
    without strong start identity is not ours and never signalled.
    """
    binding = daemon.store.get_runtime_binding(participant_id)
    if binding is None:
        return True
    if is_frontend_binding(binding):
        await close_frontend_runtime(daemon, participant_id)
        daemon.store.delete_runtime_binding(participant_id)
        return True
    try:
        # Gate-free queue cancel: the pane is already gone here, so a presence refresh could only
        # refuse on kill wake churn or pass on stale facts. The backend stops below anyway.
        await daemon.controls.cancel_queued_followups(participant_id)
    except Exception:
        logger.exception(
            "queue cancellation for %s failed; backend teardown continues",
            participant_id,
        )
    if binding.backend_pid is None or binding.backend_started_at is None:
        logger.warning(
            "cannot terminate the backend of %s: no verified process identity was "
            "ever persisted, so no signal may be sent and no retirement may "
            "proceed; the binding is kept and any live process is left for "
            "manual inspection",
            participant_id,
        )
        return False
    if daemon.runtime_manager.backend(participant_id) is None:
        try:
            await daemon.runtime_manager.adopt_backend(
                participant_id,
                backend_generation=binding.backend_generation,
                pid=binding.backend_pid,
                started_at=binding.backend_started_at,
                endpoint=binding.endpoint,
            )
        except BackendIdentityMismatch:
            # The persisted identity is the authority: a pid that no longer
            # verifies is a backend that exited, positively — safe.
            _unregister_live(daemon, participant_id)
            daemon.store.delete_runtime_binding(participant_id)
            return True
        except Exception:
            logger.exception(
                "could not adopt the persisted backend of %s for teardown; "
                "the binding is kept and retirement must not proceed",
                participant_id,
            )
            return False
    try:
        await daemon.runtime_manager.teardown(
            participant_id, backend_generation=binding.backend_generation
        )
    except Exception:
        logger.exception(
            "backend teardown for %s failed; the binding is kept and "
            "retirement must not proceed — a backend may still be running",
            participant_id,
        )
        return False
    _unregister_live(daemon, participant_id)
    daemon.store.delete_runtime_binding(participant_id)
    return True


def _unregister_live(daemon, participant_id: str) -> None:
    """Return a retired participant to durable-only observation."""
    hub = getattr(daemon.observer, "live", None)
    if hub is not None:
        hub.unregister(participant_id)


async def sweep_dead_participant_backends(daemon) -> None:
    """Reaper pass: a dead participant owns no live backend.

    Catches endings outside the kill flow and retries failed teardowns (in-flight kills are
    left to it); releases workspace usage once a stop is proved, files stay until cleanup.
    """
    for binding in daemon.store.runtime_bindings_for_recovery():
        if binding.participant_id in daemon._explicit_kills:
            continue
        participant = daemon.store.get_participant(binding.participant_id)
        if participant is not None and participant.status is not Status.DEAD:
            continue
        stopped = await teardown_participant_runtime(
            daemon, binding.participant_id, caller_id="cli"
        )
        if not stopped or participant is None:
            continue
        daemon.spawner.release_workspace_usage(participant, reason="participant_exit")
