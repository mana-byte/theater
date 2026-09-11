"""The UI-first native launch sequence.

One order, never renegotiated (the accepted Wave 0 refinement), executed
entirely through the frozen runtime contracts — no harness branch anywhere
in it:

1. Persist launch intent (binding in ``INTENDED``) — done by ``reserve``
   before this module runs.
2. Launch the detached backend (its lifetime never depends on daemon pipes).
3. Persist the verified pid + strong start identity (``STARTED``).
4. Wait until the backend's private Unix endpoint accepts connections —
   a reachability probe only, never a protocol exchange — because the stock
   backend binds measurably after exec and the runtime's first connect must
   not race that bind.
5. Initialize the observer/runtime connection and launch the promptless
   native UI; the UI creates the session (``frontend_plan`` completes the
   native handshake before the pane exists, so the eager ``thread/start`` a
   fresh UI emits can never race past the observer).
6. Wait via ``open_session`` for the exact UI-created session — or, for a
   fork, open the predecessor's exact session first and then attach the UI
   to the exact returned id.
7. Persist the exact native identity (``BOUND``) and record UI/event
   readiness from the same evidence — no blind fixed sleep.
8. Submit the initial prompt exactly once through the control service
   (``ACTIVE``). The frontend/backend argv never contain the prompt.

Failure rule: a startup failure before the initial prompt's transmission may
have begun cleans only verified participant-owned resources — the backend the
daemon just launched (terminated through the manager's generation-guarded
teardown) and the pane the daemon just created. Once transmission may have
begun, nothing is resent, relaunched, or cleaned: evidence is preserved and
the uncertain outcome stays visible.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace
from pathlib import Path

from theater import timing
from theater.daemon import workers
from theater.daemon.harness_runtime import wait_for_unix_endpoint
from theater.daemon.observation.live import LiveRegistration
from theater.daemon.spawning.models import NativeSpawnSelection, Reservation
from theater.daemon.spawning.planning import overlay_backend_mcp
from theater.harness.base import LaunchPlan
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    DeliveryResult,
    RuntimeContext,
    RuntimeLifecyclePhase,
    RuntimePlan,
    RuntimePlanningContext,
    RuntimeProbeContext,
    RuntimeWiring,
    SessionOpenMode,
)
from theater.models import BadRequest, Participant, Status, TheaterError, now
from theater.observability.catalog import SPAWN_LAUNCH
from theater.provenance import TranscriptProvenance
from theater.tmux import client as tmux

logger = logging.getLogger("theater.spawner")

#: The plan's default startup deadline bounding the whole pre-dispatch
#: sequence (backend launch, handshake, UI attach, session discovery).
NATIVE_LAUNCH_DEADLINE_SECONDS = 30.0


async def select_native_wiring(
    spawner,
    req,
    harness,
    participant: Participant,
    resume_predecessor: Participant | None,
) -> NativeSpawnSelection | None:
    """Resolve the effective wiring for one spawn; ``None`` means legacy.

    Explicit ``LEGACY`` opts out before anything is probed. Explicit
    ``NATIVE`` fails diagnostically when the harness has no runtime manifest,
    the compatibility probe refuses, or a fork has no persisted native
    predecessor identity. ``AUTO`` selects native only for a
    Theater-verified-compatible harness on the pinned verified release; the
    verified rollout is enabled, so this is the default path, and a disabled
    rollout constant (rollback) or a refused probe selects legacy with the
    recorded reason. Existing participants and local plugins without a
    runtime manifest are legacy by construction.
    """
    from theater.daemon.runtime import wiring as wiring_mod

    if req.wiring is RuntimeWiring.LEGACY:
        return None
    manifest = wiring_mod.runtime_manifest_of(harness)
    if manifest is None:
        if req.wiring is RuntimeWiring.NATIVE:
            raise BadRequest(
                f"harness {req.harness!r} provides no runtime manifest; spawn it "
                "with wiring='legacy'"
            )
        return None
    fork_parent = None
    if resume_predecessor is not None:
        predecessor_binding = spawner.registry.store.get_runtime_binding(resume_predecessor.id)
        if predecessor_binding is None or predecessor_binding.native_session_id is None:
            if req.wiring is RuntimeWiring.NATIVE:
                raise BadRequest(
                    f"cannot resume participant {resume_predecessor.id!r} with "
                    "native wiring: the predecessor has no persisted native "
                    "session identity; resume it with wiring='legacy'"
                )
            return None
        fork_parent = predecessor_binding.native_session_id
    if req.wiring is RuntimeWiring.AUTO and not wiring_mod.NATIVE_AUTO_SELECTION_ENABLED:
        return None
    compatibility = await workers.to_thread(
        manifest.probe,
        RuntimeProbeContext(
            participant_id=participant.id, binary=harness.binary, cwd=participant.cwd
        ),
        label="spawn.runtime_probe",
    )
    if not compatibility.supported:
        if req.wiring is RuntimeWiring.NATIVE:
            raise BadRequest(
                f"native wiring requested for {req.harness!r}, but the "
                f"compatibility probe refused: {compatibility.reason}"
            )
        logger.info("wiring=auto selects legacy for %s: %s", req.harness, compatibility.reason)
        return None
    return NativeSpawnSelection(
        runtime=manifest,
        endpoint=wiring_mod.native_endpoint(participant.id),
        backend_generation=wiring_mod.RUNTIME_BACKEND_GENERATION_INITIAL,
        compatibility=compatibility,
        fork_parent_session=fork_parent,
    )


async def launch_native(spawner, reservation: Reservation) -> Participant:
    """Run the UI-first sequence for one natively-wired spawn.

    The whole sequence is bounded by the startup deadline; a timeout is a
    startup failure that follows the same pre-dispatch cleanup path as every
    other startup failure — backend teardown first, then the pane, then the
    binding, and only then the generic reservation cleanup — and only after
    that cleanup is the timeout converted to its diagnostic error. The
    ambiguous-dispatch boundary is still checked on the way out: if the
    initial prompt's transmission had already begun when the deadline fired,
    nothing is cleaned, resent, or relaunched.

    If the verified backend teardown itself fails during that cleanup, the
    worktree, pane, and binding ownership are preserved — a backend may
    still be running in them — and a diagnostic failure is raised instead of
    the generic reservation cleanup.
    """
    participant = reservation.participant
    native = reservation.native
    assert native is not None
    store = spawner.registry.store
    pid = participant.id
    try:
        return await asyncio.wait_for(
            _launch_native_sequence(spawner, reservation, native, participant),
            NATIVE_LAUNCH_DEADLINE_SECONDS,
        )
    except BaseException as exc:
        if not _dispatch_may_have_begun(store, pid):
            cleaned = await _cleanup_failed_native(spawner, participant, native.backend_generation)
            if cleaned:
                await spawner.cleanup_reservation(participant)
            else:
                raise TheaterError(
                    f"native spawn of {pid!r} failed and its backend teardown also "
                    "failed; the runtime binding, pane, and worktree are preserved "
                    "for the next reconciliation — inspect the backend process "
                    f"(generation {native.backend_generation}) before retrying"
                ) from exc
        if isinstance(exc, TimeoutError):
            raise TheaterError(
                f"native spawn of {pid!r} did not complete within "
                f"{NATIVE_LAUNCH_DEADLINE_SECONDS:.0f}s — the backend, UI, or session "
                "discovery stalled; inspect the participant's runtime logs before retrying"
            ) from exc
        raise


async def _launch_native_sequence(
    spawner,
    reservation: Reservation,
    native: NativeSpawnSelection,
    participant: Participant,
) -> Participant:
    store = spawner.registry.store
    req = reservation.req
    pid = participant.id
    generation = native.backend_generation

    if spawner.runtime_manager is None or spawner.controls is None or spawner.runtime_io is None:
        raise BadRequest(
            f"native wiring for {pid!r} requires the daemon runtime manager, "
            "I/O, and control service; this spawner was composed without them"
        )

    # ---- 2. detached backend ------------------------------------------
    plan = native.runtime.plan(
        RuntimePlanningContext(
            participant_id=pid,
            cwd=reservation.child_cwd,
            endpoint=native.endpoint,
            approval=req.approval,
            model=req.model,
            reasoning_effort=req.reasoning_effort,
        )
    )
    if not isinstance(plan, RuntimePlan):
        raise TypeError("runtime manifest planner must return a RuntimePlan")
    # The backend receives Theater's participant-scoped MCP configuration
    # through the harness's generic overlay seam; its plan files are written
    # by the detached-backend launch before the process starts.
    plan = replace(plan, backend=overlay_backend_mcp(plan.backend, participant))
    identity = await spawner.runtime_manager.launch_backend(
        pid,
        backend_generation=generation,
        plan=plan,
        cwd=Path(reservation.child_cwd),
    )
    # ---- 3. persist the verified pid + strong start identity ---------
    if not store.mark_runtime_backend_started(
        pid,
        backend_generation=generation,
        pid=identity.pid,
        started_at=identity.started_at,
    ):
        raise TheaterError(
            f"runtime binding generation for {pid!r} changed during launch; "
            "failing closed instead of recording another generation's backend"
        )

    # ---- 4. endpoint readiness before any runtime connection ----------
    # The backend's endpoint binds measurably after exec; the runtime's
    # first connect (frontend_plan/open_session) must not race that bind.
    # A reachability probe only — it never speaks the native protocol. Its
    # budget is the single startup deadline: a backend that binds late
    # within the whole-sequence bound is accepted, and the existing outer
    # asyncio.wait_for still enforces that one true deadline.
    await wait_for_unix_endpoint(native.endpoint, timeout=NATIVE_LAUNCH_DEADLINE_SECONDS)

    # ---- 5. one runtime instance, observer connection, UI plan --------
    runtime = await spawner.runtime_manager.get_or_create(
        pid,
        backend_generation=generation,
        create=_runtime_factory(spawner, native, reservation, participant),
    )
    fork_parent = native.fork_parent_session
    if fork_parent is not None:
        # FORK: open the predecessor's exact session first, then attach the
        # UI to the exact returned id (the frozen fork order).
        binding = await runtime.open_session(
            mode=SessionOpenMode.FORK, native_session_id=fork_parent
        )
        _bind_identity(store, participant.id, binding, generation)
        _register_live_wiring(spawner, native, participant.id, runtime, binding)
        pane_plan = await runtime.frontend_plan(native_session_id=binding.native_session_id)
    else:
        # NEW: the promptless fresh-native-UI plan completes the observer
        # handshake before the pane exists; the UI creates the session.
        pane_plan = await runtime.frontend_plan(native_session_id=None)

    attached, _created = await _launch_native_pane(spawner, reservation, pane_plan)

    if fork_parent is None:
        # ---- 6. wait for the exact UI-created session -----------------
        binding = await runtime.open_session(mode=SessionOpenMode.NEW)
        _bind_identity(store, participant.id, binding, generation)
        _register_live_wiring(spawner, native, participant.id, runtime, binding)

    # ---- 7. readiness verified from evidence (thread/started observed
    # by open_session); no blind fixed sleep ----------------------------
    if not store.set_runtime_lifecycle(
        pid,
        RuntimeLifecyclePhase.ATTACHED,
        backend_generation=generation,
        updated_at=now(),
    ):
        raise TheaterError(
            f"runtime binding generation for {pid!r} changed during launch; "
            "failing closed instead of advancing another generation's lifecycle"
        )

    # ---- 8. the initial prompt exactly once, through the control service
    if req.prompt:
        # ``job_handle`` binds the spawn RPC's job (its handle is the
        # participant id) to the native terminal evidence the runtime will
        # report, so no second send job exists.
        await spawner.controls.send(
            pid,
            caller_id=req.parent_id or "cli",
            prompt=req.prompt,
            response_format=req.response_format,
            job_handle=pid,
        )
        if not store.set_runtime_lifecycle(
            pid,
            RuntimeLifecyclePhase.ACTIVE,
            backend_generation=generation,
            updated_at=now(),
        ):
            raise TheaterError(
                f"runtime binding generation for {pid!r} changed during launch; "
                "failing closed instead of advancing another generation's lifecycle"
            )
    # A promptless spawn completes after successful attachment.
    return attached


def _runtime_factory(
    spawner,
    native: NativeSpawnSelection,
    reservation: Reservation,
    participant: Participant,
):
    """Bind the manifest factory to one immutable context and injected I/O."""

    async def create():
        context = RuntimeContext(
            participant_id=participant.id,
            cwd=participant.cwd,
            io=spawner.runtime_io,
            backend_generation=native.backend_generation,
            endpoint=native.endpoint,
            approval=reservation.req.approval,
            model=reservation.req.model,
            reasoning_effort=reservation.req.reasoning_effort,
        )
        return native.runtime.factory(context)

    return create


def _bind_identity(store, participant_id: str, binding, generation: int) -> None:
    """Persist the exact native identity before any prompt is transmitted."""
    if binding.native_session_id is None:
        raise TheaterError(
            f"the native runtime for {participant_id!r} reported no native "
            "session id; refusing to bind an unnamed session"
        )
    updated = store.bind_runtime_identity(
        participant_id,
        backend_generation=generation,
        native_session_id=binding.native_session_id,
        protocol=binding.protocol,
        protocol_version=binding.protocol_version,
        native_version=binding.native_version,
        compatibility_policy=binding.compatibility_policy,
        updated_at=now(),
    )
    if not updated:
        raise TheaterError(
            f"runtime binding generation for {participant_id!r} changed during "
            "launch; failing closed instead of binding another generation's identity"
        )
    # The native session id is the resume identity: an exact, spawned-by-
    # construction correlation, exactly like a legacy plan's session id.
    # Re-read the row first: attaching the pane may have advanced it since
    # the reservation captured its participant object.
    current = store.get_participant(participant_id)
    if current is None:
        raise TheaterError(f"participant {participant_id!r} vanished during its native launch")
    current.session_id = binding.native_session_id
    current.session_correlation = str(TranscriptProvenance.EXACT)
    store.upsert_participant(current)


def _register_live_wiring(
    spawner,
    native: NativeSpawnSelection,
    participant_id: str,
    runtime,
    binding,
) -> None:
    """Register the runtime's live channel with the observer hub.

    Runs immediately after the exact native identity is bound, so evidence
    the runtime reports before the initial prompt is transmitted can still
    reconcile. Composition is generic: the manifest's declared channel, the
    runtime's single live ``Source``, and the control-service callables that
    own exact job completion — never harness internals.
    """
    hub = spawner.live_hub
    if hub is None:
        return
    hub.register(
        LiveRegistration(
            participant_id=participant_id,
            live_source=runtime.live_source(),
            channel=native.runtime.channel,
            backend_generation=native.backend_generation,
            native_session_id=binding.native_session_id,
            evidence_sink=spawner.controls.record_terminal_evidence,
            active_job_for_turn=spawner.controls.active_job_for_native_turn,
        )
    )


async def _launch_native_pane(spawner, reservation: Reservation, pane_plan: LaunchPlan):
    """Create the tmux window running the promptless native UI."""
    participant = reservation.participant

    async def _pane():
        with timing.span(SPAWN_LAUNCH, id=participant.id, harness=participant.harness):
            return await tmux.new_window_with_identity(
                session=reservation.session,
                name=reservation.name,
                cwd=reservation.child_cwd,
                command=pane_plan.argv,
                env={**pane_plan.env, "THEATER_ID": participant.id},
                background=reservation.req.background,
            )

    if spawner._tmux_reconcile_lock is None:
        created = await _pane()
    else:
        async with spawner._tmux_reconcile_lock:
            created = await _pane()
    attached = spawner.registry.attach_pane(
        participant.id,
        created.pane_id,
        pane_pid=created.pane_pid,
        tmux_server_identity=created.server_identity,
    )
    if spawner._reconcile_tmux is not None:
        await spawner._reconcile_tmux()
        attached = spawner.registry.get(participant.id)
    if attached.status is Status.DEAD:
        raise TheaterError("tmux server restarted or the new pane exited during spawn")
    return attached, created


def _dispatch_may_have_begun(store, participant_id: str) -> bool:
    """Whether any control operation's transmission may have begun.

    ``DISPATCHED`` is persisted before transmission begins, and a settled
    ACCEPTED/UNKNOWN delivery is transmission that happened: both mean the
    spawn crossed the ambiguous-dispatch boundary, after which nothing is
    resent, relaunched, or cleaned.
    """
    if store.dispatched_control_operations(participant_id):
        return True
    for operation in store.control_operations_in_phases(
        participant_id, (ControlDeliveryPhase.SETTLED,)
    ):
        if operation.delivery_result in (DeliveryResult.ACCEPTED, DeliveryResult.UNKNOWN):
            return True
    return False


async def _cleanup_failed_native(
    spawner,
    participant: Participant,
    generation: int,
) -> bool:
    """Clean only verified participant-owned resources after a pre-dispatch failure.

    The backend the daemon just launched is terminated through the manager's
    generation-guarded teardown (which verifies process identity before any
    signal); the pane the daemon just created is killed with the identity
    facts the launch itself recorded; the binding row and the live-channel
    registration go with them.

    Returns whether the cleanup verified. A failed backend teardown is a
    ``False`` return — not a raise and not best-effort: the binding and the
    pane ownership stay with the participant, because a backend that may
    still be running must keep everything it may still be using, and the
    caller raises the diagnostic failure instead of running the generic
    reservation cleanup.
    """
    pid = participant.id
    try:
        await spawner.runtime_manager.teardown(pid, backend_generation=generation)
    except Exception:
        logger.exception(
            "backend teardown after failed native spawn of %s failed; the "
            "binding, pane, and worktree are kept — a backend may still be "
            "running in them",
            pid,
        )
        return False
    with contextlib.suppress(Exception):
        current = spawner.registry.store.get_participant(pid)
        if current is not None and current.tmux_pane and current.status is not Status.DEAD:
            await spawner.kill_pane(
                pid,
                expected_server_identity=current.tmux_server_identity,
                expected_pane_pid=current.pid,
            )
    spawner.registry.store.delete_runtime_binding(pid)
    if spawner.live_hub is not None:
        spawner.live_hub.unregister(pid)
    return True
