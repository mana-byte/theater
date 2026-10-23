"""Startup and live recovery of persisted native runtime bindings.

Adopt only if pid + start identity verify; reconnect the exact session (never a cwd
guess); consume evidence; close ambiguity without replay.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

from theater import timing
from theater.daemon.harness_runtime.errors import BackendIdentityMismatch
from theater.daemon.harness_runtime.manager import RecoveryCallback, RuntimeFactory
from theater.daemon.observation.live import LiveRegistration
from theater.daemon.persistence.repositories.runtime_bindings import ParticipantRuntimeBinding
from theater.daemon.runtime.evidence import persist_buffered_evidence
from theater.daemon.runtime.launch_policy import decode_launch_policy
from theater.daemon.runtime.native_teardown import (
    sweep_dead_participant_backends,
    teardown_participant_runtime,
)
from theater.daemon.runtime.public_recovery import (
    prepare_provider_control_recovery,
    reconcile_public_control_operations,
)
from theater.daemon.spawning.frontend import (
    close_frontend_runtime,
    is_frontend_binding,
    restore_frontend_listener,
)
from theater.daemon.spawning.runtime_identity import validate_runtime_binding
from theater.harness import get as get_harness
from theater.harness.contracts.runtime import (
    HarnessRuntime,
    RuntimeBinding,
    RuntimeContext,
    RuntimeLifecyclePhase,
    RuntimeManifest,
    SessionOpenMode,
)
from theater.models import JobState, Participant, Status, now
from theater.observability.catalog import RUNTIME_RECONNECT
from theater.provenance import TranscriptProvenance

if TYPE_CHECKING:
    from pathlib import Path

    from theater.daemon.server import Daemon
    from theater.harness.contracts.harness import Harness

logger = logging.getLogger("theater.daemon.runtime")

_ORPHAN_BUS_KIND = "runtime.orphan"
_BACKEND_GONE_BUS_KIND = "runtime.backend_gone"
_BACKEND_GONE_ERROR_CODE = "backend_gone"


class _RecoveryLeaseRevoked(Exception):
    """An in-flight live recovery lost authority while awaiting native I/O."""


async def reconcile_runtime_bindings(daemon: Daemon) -> None:
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


async def _reconcile_one_binding(daemon: Daemon, binding: ParticipantRuntimeBinding) -> None:
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
            endpoint=cast(str, binding.endpoint),
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
    await daemon.controls.settle_pending_evidence([participant_id])
    await daemon.controls.reconcile_ambiguous_delivery(participant_id, now_ts=now())


async def _reconnect_runtime(
    daemon: Daemon, binding: ParticipantRuntimeBinding, participant: Participant
) -> tuple[HarnessRuntime, RuntimeManifest] | None:
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


def _runtime_factory(
    daemon: Daemon, binding: ParticipantRuntimeBinding, participant: Participant
) -> tuple[RuntimeManifest, RuntimeFactory] | None:
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
    launch_policy = decode_launch_policy(binding.launch_policy).fields
    io = daemon.runtime_io
    token_file = _runtime_token_file(daemon, binding, manifest)

    async def create() -> HarnessRuntime:
        context = RuntimeContext(
            participant_id=binding.participant_id,
            cwd=participant.cwd,
            io=io,
            backend_generation=binding.backend_generation,
            endpoint=binding.endpoint,
            token_file=token_file,
            approval=cast(str | None, launch_policy.get("approval")),
            model=cast(str | None, launch_policy.get("model")),
            reasoning_effort=cast(str | None, launch_policy.get("reasoning_effort")),
            native_session_id=binding.native_session_id,
            binary=cast("Harness", harness).binary,
        )
        return manifest.factory(context)

    return manifest, create


def _runtime_token_file(
    daemon: Daemon, binding: ParticipantRuntimeBinding, manifest: RuntimeManifest
) -> Path | None:
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
    daemon: Daemon,
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
    daemon: Daemon,
    participant_id: str,
    backend_generation: int,
    runtime: HarnessRuntime,
    expected_session: str,
) -> ParticipantRuntimeBinding | None:
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
    daemon: Daemon,
    recovery_owner: Callable[[str, int], Awaitable[bool]] | None,
) -> bool:
    """Whether a live-recovery attempt still owns the manager's callback lease."""
    return recovery_owner is None or daemon.runtime_manager.recovery_callback_is_current(
        recovery_owner
    )


async def _require_recovery_owner(
    daemon: Daemon,
    recovery_owner: Callable[[str, int], Awaitable[bool]] | None,
    participant_id: str,
    runtime: HarnessRuntime | None = None,
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
    daemon: Daemon,
    participant_id: str,
    runtime: HarnessRuntime,
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


async def _require_cached_recovered_session(
    daemon: Daemon, participant_id: str, runtime: HarnessRuntime, binding: RuntimeBinding
) -> None:
    """Cache only the candidate that still owns this participant's runtime slot."""
    if daemon.runtime_manager.mark_session_open(participant_id, runtime, binding):
        return
    await _discard_recovered_candidate(daemon, participant_id, runtime)
    raise RuntimeError(
        f"runtime for {participant_id!r} changed before its recovered session was cached"
    )


def live_recovery_callback(daemon: Daemon) -> RecoveryCallback:
    """The generic recovery callback the daemon composition injects.

    The manager stays harness-neutral: it only reports ``(participant_id, generation)``.
    """

    async def recover(participant_id: str, backend_generation: int) -> bool:
        backend = daemon.runtime_manager.backend(participant_id)
        if backend is not None and not backend.alive():
            # The verified backend process exited mid-flight, so its open turns
            # can never produce native evidence (no idle marker, no terminal
            # event): settle affected jobs as crashed — never as success — via
            # the backend_gone path startup reconciliation uses, instead of
            # retrying a reconnect that can never land.
            binding = daemon.store.get_runtime_binding(participant_id)
            if binding is not None and binding.backend_generation == backend_generation:
                logger.warning(
                    "the native backend of %s (generation %s) exited while its "
                    "work was in flight; failing its running jobs as crashed",
                    participant_id,
                    backend_generation,
                )
                _backend_gone(daemon, binding)
            runtime = daemon.runtime_manager.get(participant_id)
            if runtime is not None:
                await _discard_recovered_candidate(
                    daemon, participant_id, runtime, recovery_owner=recover
                )
            return True
        return await recover_live_runtime(
            daemon,
            participant_id,
            backend_generation,
            recovery_owner=recover,
        )

    return recover


def _register_live(
    daemon: Daemon,
    binding: ParticipantRuntimeBinding,
    runtime: HarnessRuntime,
    manifest: RuntimeManifest,
) -> None:
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


def _backend_gone(daemon: Daemon, binding: ParticipantRuntimeBinding) -> None:
    """A persisted backend whose pid no longer verifies: fail affected work.

    The process exited (pid possibly reused), so nothing is signalled; jobs fail
    ``backend_gone`` and the participant follows its ordinary lifecycle. Used
    both at startup reconciliation and by the running health monitor, whose
    recovery callback hits this when the verified process died mid-flight.
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


def _orphan_diagnostic(daemon: Daemon, binding: ParticipantRuntimeBinding, detail: str) -> None:
    logger.warning("runtime orphan for %s: %s", binding.participant_id, detail)
    daemon.store.bus_append(
        _ORPHAN_BUS_KIND,
        to_id=binding.participant_id,
        payload={"reason": detail, "lifecycle": str(binding.lifecycle)},
    )


__all__ = [
    "live_recovery_callback",
    "prepare_provider_control_recovery",
    "reconcile_public_control_operations",
    "reconcile_runtime_bindings",
    "recover_live_runtime",
    "sweep_dead_participant_backends",
    "teardown_participant_runtime",
]
