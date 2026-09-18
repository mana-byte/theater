"""Restart reconciliation and teardown of persisted runtime bindings.

On daemon startup this runs *before* ordinary observation assumes a backend
is missing, and from the reaper it sweeps bindings whose participant is
already dead. The order is the plan's, never renegotiated:

1. Fail never-dispatched queued/reserved work (``daemon_restarted``) — no
   prompt is ever replayed.
2. Adopt a persisted backend only when pid + ``backend_started_at`` verify
   against the live process; a mismatch is a dead backend, never a signal to
   whatever recycled the pid.
3. Reconnect the exact persisted native session (RECONNECT); identity
   mismatch fails closed — never a cwd guess.
4. Consume stored terminal evidence (finish the same job once).
5. Reconcile ambiguous delivery without replay: stored evidence or the
   authoritative snapshot, and the 30-second deadline closes the rest.

Orphan diagnostics are exposed (bus + log) for a live backend or thread whose
identity cannot safely bind: a pre-identity crash leaves a backend Theater
may not be able to name, and a started backend without a persisted session
holds a thread only the native UI can answer. In both cases Theater adopts
what it can verify, never launches a second UI, and never signals a process
it cannot positively identify.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from theater import timing
from theater.daemon.harness_runtime.errors import BackendIdentityMismatch
from theater.daemon.observation.live import LiveRegistration
from theater.daemon.operations import DispatchIntent, OperationOutcome
from theater.daemon.runtime.public_recovery import fail_proven_undispatched
from theater.daemon.spawning.frontend import (
    close_frontend_runtime,
    is_frontend_binding,
    restore_frontend_listener,
)
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


def reconcile_public_control_operations(daemon) -> None:
    """Reconnect public operation state to durable control rows after a crash."""
    cursor: str | None = None
    while True:
        records, cursor = daemon.store.operations.list_page(
            cursor=cursor,
            limit=500,
            unsettled_only=True,
        )
        for operation in records:
            _reconcile_public_control_operation(daemon, operation)
        if cursor is None:
            break


def _reconcile_public_control_operation(daemon, operation) -> None:
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
            if not fail_proven_undispatched(daemon, operation):
                _mark_operation_uncertain(
                    daemon, operation.operation_id, "dispatch_recovery_pending"
                )
            return
        _mark_crash_ambiguous_provider_operation(daemon, operation)
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


def _mark_crash_ambiguous_provider_operation(daemon, operation) -> None:
    if operation.state != PublicOperationState.RUNNING.value:
        return
    dispatched = operation.dispatch_provider_id is not None
    if operation.kind == "spawn":
        launch = daemon.store.operations.get_launch(operation.operation_id)
        dispatched = launch is not None and launch.dispatch_marker is not None
        if not dispatched:
            fail_proven_undispatched(daemon, operation)
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
            fail_proven_undispatched(daemon, operation)
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
        await runtime.open_session(
            mode=SessionOpenMode.RECONNECT, native_session_id=binding.native_session_id
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
        # Fail-closed and retryable: the installed candidate is unusable, so
        # it is discarded in place — the persisted generation/session is
        # revalidated after the awaited open, and the discard is conditional
        # on exact runtime identity — making its snapshot read DISCONNECTED
        # so the manager's health monitor retries the exact persisted session
        # on its bounded cadence instead of a connected-but-unusable runtime
        # suppressing it. A stale completion (a replacement generation took
        # the participant mid-open) registers nothing and never closes,
        # unregisters, or discards the successor.
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
    # Revalidate after the awaited open and immediately before registration:
    # a replacement generation may have taken the participant while the
    # open was in flight, and a successful stale completion is as stale as a
    # failed one. It returns without registering — and without closing,
    # unregistering, or otherwise mutating anything the successor owns.
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
    # The live wiring is registered right after the exact session open —
    # before stored evidence is consumed — so terminal evidence the runtime
    # already holds can reconcile through the same sink as a live turn's.
    # A registration failure is fail-closed and retryable, exactly like a
    # failed session open: the candidate is discarded in place (generation
    # and session revalidated, identity-conditional) so the manager's health
    # monitor retries instead of a connected runtime with no live wiring
    # suppressing it; the exception keeps its existing propagation to the
    # per-binding reconciliation logger.
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
    """Create the participant's one runtime for the adopted generation.

    ``None`` means the harness can no longer provide a runtime manifest (a
    local plugin replaced a shipped one, or the harness vanished): the
    adopted backend and binding are kept, a diagnostic is exposed, and
    affected jobs resolve through the ambiguous-delivery deadline — never by
    replaying a prompt. Otherwise the runtime and the manifest it came from
    are returned together, so the caller can register the manifest's
    declared live channel without a second harness lookup. The timing span
    is instrumentation only: it measures the startup reconnect and changes
    none of its adoption or identity rules.
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

    Shared seam between startup reconciliation and same-runtime live
    recovery: the create callback rebuilds the exact persisted context —
    the verified endpoint and backend generation, the persisted launch
    policy, the persisted native session id, and the daemon's one shared
    runtime I/O — so every reconnect constructs the runtime the same way.
    ``None`` means the harness can no longer provide a runtime manifest.
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

    Recovery never mints a secret: the record and its 0600 file were
    persisted at reservation, and a missing record for a declared need is
    a fail-closed error, not a fresh token (the backend holds the old one).
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


async def recover_live_runtime(daemon, participant_id: str, backend_generation: int) -> bool:
    """Same-runtime live recovery after the notification stream disconnected.

    Called by the runtime manager's health monitor when an installed
    runtime's connection is DISCONNECTED — including a transport
    notification overflow surfaced as a disconnect — while the verified
    backend stays alive. Recovery is bounded and exact: only the persisted
    binding's exact generation and native session are recovered, the live
    backend is reused (never relaunched, never signalled), the manager's
    ``reconnect`` replaces only the controlling runtime — preserving its
    generation checks and close-without-kill semantics — and
    ``open_session(RECONNECT)`` re-adopts the exact persisted session, where
    an identity mismatch fails closed instead of guessing. The manifest's
    declared live source is re-registered through the same hub seam as
    startup reconciliation, so the existing observer machinery transfers
    the old source's buffered terminal evidence through the bounded
    synchronous snapshot hook. No prompt is replayed and no
    ambiguous-delivery resolution runs here: live registration and observer
    persistence happen first.

    Ownership is revalidated after every awaited boundary — the manager
    reconnect, the session open, and immediately before registration: the
    persisted binding must still name this exact participant, backend
    generation, and native session, and the manager's current runtime must
    be exactly the recovered candidate. A stale completion (a replacement
    generation or runtime took the participant mid-recovery) returns
    ``False`` and never registers, closes, or unregisters anything the
    successor owns. A failed session open or registration discards the
    failed candidate in place — identity-checked, disconnect-only — so the
    candidate reads DISCONNECTED and the monitor retries on its bounded
    cadence instead of leaving a connected-but-unusable runtime suppressing
    it. ``False`` — a stale generation, a missing binding or session
    identity, a dead participant, or a failed attempt — leaves every
    generation rule intact for that retry.
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
            factory = _runtime_factory(daemon, binding, participant)
            if factory is None:
                return False
            manifest, create = factory
            runtime = await daemon.runtime_manager.reconnect(
                participant_id,
                backend_generation=binding.backend_generation,
                create=create,
            )
            # Revalidate after the awaited reconnect: a replacement may have
            # taken the participant while the candidate was being built.
            binding = _current_recovery_binding(
                daemon, participant_id, backend_generation, runtime, expected_session
            )
            if binding is None:
                return False
            try:
                await runtime.open_session(
                    mode=SessionOpenMode.RECONNECT, native_session_id=expected_session
                )
            except Exception:
                # The candidate is installed but unusable: disconnect it in
                # place so the monitor retries on its bounded cadence.
                await _discard_recovered_candidate(daemon, participant_id, runtime)
                raise
            # Revalidate after the awaited session open and immediately
            # before registration: a stale completion must never register,
            # close, or unregister the successor.
            binding = _current_recovery_binding(
                daemon, participant_id, backend_generation, runtime, expected_session
            )
            if binding is None:
                return False
            try:
                _register_live(daemon, binding, runtime, manifest)
            except Exception:
                # Registration failed with the candidate current: discard it
                # in place for a bounded retry, never a silent dead runtime.
                await _discard_recovered_candidate(daemon, participant_id, runtime)
                raise
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

    The authority after every awaited recovery boundary: the persisted
    binding must still name this exact participant, backend generation, and
    native session, and the manager's current runtime must be exactly the
    recovered candidate. ``None`` means a replacement generation or runtime
    owns the participant — a stale completion that must never register,
    close, or unregister anything.
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


async def _discard_recovered_candidate(daemon, participant_id: str, runtime) -> None:
    """Disconnect one failed recovery candidate in place: fail-closed, retryable.

    The candidate stays installed in the manager — removing or closing it
    through the manager would race a concurrent replacement — but its
    connection is closed, so its snapshot reads DISCONNECTED and the health
    monitor retries on its bounded cadence instead of leaving a
    connected-but-unusable runtime suppressing it. The disconnect is
    conditional on exact runtime identity (a concurrently installed
    successor is never touched) and closing a recovery-owned instance is
    disconnect-only.
    """
    try:
        if daemon.runtime_manager.get(participant_id) is not runtime:
            return  # a successor owns the participant; the candidate is inert
        await runtime.aclose()
    except Exception:
        logger.warning(
            "discarding a failed recovery candidate of %s failed",
            participant_id,
            exc_info=True,
        )


def live_recovery_callback(daemon) -> Callable[[str, int], Awaitable[bool]]:
    """The generic recovery callback the daemon composition injects.

    Closes over the composed daemon (store, runtime manager, controls,
    observer, shared runtime I/O); the manager stays harness-neutral and
    never learns what recovery means — it only reports
    ``(participant_id, backend_generation)`` as disconnected.
    """

    async def recover(participant_id: str, backend_generation: int) -> bool:
        return await recover_live_runtime(daemon, participant_id, backend_generation)

    return recover


def _register_live(daemon, binding, runtime, manifest) -> None:
    """Register the reconnected runtime's live channel with the observer hub.

    Generic composition only: the manifest's declared channel, the runtime's
    single live ``Source``, and the control-service callables that own exact
    job completion. A daemon composed without the observation live hub keeps
    its durable-only behaviour.
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

    The stored identity is the authority — a dead pid or a changed start
    identity means the backend exited (and the pid may have been reused), so
    nothing is signalled. Affected running jobs fail ``backend_gone`` and the
    participant follows its ordinary lifecycle policy (its pane, if any, is
    reconciled by the observer/reaper as usual).
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
    """Explicit kill / confirmed participant exit: stop the verified backend.

    Returns whether the participant's backend is proven stopped — or never
    had an identity Theatre may act on. ``True`` authorizes pane/worktree
    retirement. ``False`` means ownership could not be proven or termination
    failed: the binding and the live wiring are retained and the caller must
    not retire the pane or worktree, because a backend may still be using
    them. Only a backend whose identity verifies is signalled; a persisted pid
    without a strong start identity is a process Theatre does not own, so
    the binding is retained (with a diagnostic) instead of being dropped.
    The binding row survives a failed teardown for the next reconciliation
    to retry. The caller authorizes before the pane dies; ``caller_id`` is
    kept for that contract — the queue cancellation inside never gates on
    presence, because no pane exists to protect by then.
    """
    binding = daemon.store.get_runtime_binding(participant_id)
    if binding is None:
        return True
    if is_frontend_binding(binding):
        await close_frontend_runtime(daemon, participant_id)
        daemon.store.delete_runtime_binding(participant_id)
        return True
    try:
        # The queue is Theater-owned, so cancel it through the gate-free path
        # instead of the full interrupt. Every caller reaches here with the
        # pane already gone — the kill confirmed it, exits observed it, the
        # reaper only ever sees dead rows — so a presence refresh can only
        # refuse on wake churn (the kill itself fires after-kill-pane and
        # window-unlinked wakes) or pass on stale pre-kill facts. The active
        # turn needs no interrupt either: the backend stops right below, and
        # the job rows are already terminal.
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

    Covers every path that ends a participant without the kill flow — tmux
    restarts, failed spawns with a kept binding — and retries teardowns that
    failed earlier. Explicit in-flight kills are left alone: the kill flow
    owns those.

    This sweep also releases durable workspace usage once a previously
    uncertain backend stop is proved. Workspace files remain until an
    explicit cleanup operation removes them.
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
