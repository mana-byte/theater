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
from theater.harness import get as get_harness
from theater.harness.contracts.runtime import (
    RuntimeContext,
    RuntimeLifecyclePhase,
    SessionOpenMode,
)
from theater.models import JobState, Participant, Status, now
from theater.observability.catalog import RUNTIME_RECONNECT
from theater.provenance import TranscriptProvenance

logger = logging.getLogger("theater.daemon.runtime")

_ORPHAN_BUS_KIND = "runtime.orphan"
_BACKEND_GONE_BUS_KIND = "runtime.backend_gone"
_BACKEND_GONE_ERROR_CODE = "backend_gone"


async def reconcile_runtime_bindings(daemon) -> None:
    """Startup: reconcile persisted runtime bindings before ordinary observation."""
    bindings = daemon.store.runtime_bindings_for_recovery()
    if not bindings:
        return
    logger.info(
        "reconciling %d persisted runtime binding(s) before ordinary observation",
        len(bindings),
    )
    # Never-dispatched queued/reserved work fails for every bound participant
    # before anything is adopted or reconnected: it is definitively not
    # delivered, never replayed, and safe for the caller to re-queue.
    daemon.controls.fail_undelivered_followups([binding.participant_id for binding in bindings])
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

    async def create():
        context = RuntimeContext(
            participant_id=binding.participant_id,
            cwd=participant.cwd,
            io=io,
            backend_generation=binding.backend_generation,
            endpoint=binding.endpoint,
            approval=launch_policy.get("approval"),
            model=launch_policy.get("model"),
            reasoning_effort=launch_policy.get("reasoning_effort"),
            native_session_id=binding.native_session_id,
        )
        return manifest.factory(context)

    return manifest, create


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
    to retry.
    """
    binding = daemon.store.get_runtime_binding(participant_id)
    if binding is None:
        return True
    if daemon.runtime_manager.get(participant_id) is not None:
        try:
            await daemon.controls.interrupt(participant_id, caller_id=caller_id)
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

    This sweep is also the retry that completes a preserved retirement: a
    confirmed exit that could not prove its backend stopped kept the
    worktree and binding, and once the backend is verified stopped here,
    the worktree is reclaimed with the confirmed-exit branch policy
    (preserved, like a self-exit).
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
        try:
            await daemon.spawner.retire(participant, delete_branch=False)
        except Exception:
            logger.exception(
                "retire after verified backend teardown failed for %s; "
                "the participant remains dead",
                participant.id,
            )
