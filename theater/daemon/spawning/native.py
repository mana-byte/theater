"""The detached native launch sequence, one fixed order through frozen contracts only.

Intent, backend, identity, endpoint probe, runtime, promptless UI, binding, then the prompt
once (never in argv). Once transmission may have begun, nothing is resent or cleaned.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from theater import timing
from theater.daemon import workers
from theater.daemon.harness_runtime import wait_for_unix_endpoint
from theater.daemon.observation.live import LiveRegistration
from theater.daemon.spawning.models import (
    NativeSpawnSelection,
    ProviderLaunchOutcome,
    Reservation,
)
from theater.daemon.spawning.planning import install_runtime_mcp_plans
from theater.daemon.spawning.runtime_identity import bind_runtime_identity
from theater.harness.base import LaunchPlan
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    DeliveryResult,
    RuntimeCompatibility,
    RuntimeContext,
    RuntimeHost,
    RuntimeLifecyclePhase,
    RuntimePlan,
    RuntimePlanningContext,
    RuntimeProbeContext,
    RuntimeSessionOrder,
    RuntimeWiring,
    SessionOpenMode,
)
from theater.models import BadRequest, Participant, TheaterError, new_id, now
from theater.observability.catalog import LIFECYCLE_STAGE

logger = logging.getLogger("theater.spawner")

#: The plan's default startup deadline bounding the whole pre-dispatch
#: sequence (backend launch, handshake, UI attach, session discovery).
NATIVE_LAUNCH_DEADLINE_SECONDS = 30.0


@dataclass(slots=True)
class _LaunchAttempt:
    dispatch_started: bool = False
    terminal_create_attempted: bool = False


async def select_native_wiring(
    spawner,
    req,
    harness,
    participant: Participant,
    resume_predecessor: Participant | None,
) -> NativeSpawnSelection | None:
    """Resolve the effective wiring for one spawn; ``None`` means legacy.

    ``LEGACY`` opts out before probing; ``AUTO`` picks native only for a verified-compatible
    harness on the pinned release with rollout enabled, else legacy with the recorded reason.
    """
    from theater.daemon.runtime import wiring as wiring_mod

    if req.wiring is RuntimeWiring.LEGACY:
        return None
    manifest = wiring_mod.runtime_manifest_of(harness)
    if manifest is None:
        return None
    if manifest.host is RuntimeHost.FRONTEND and any(
        getattr(spawner, name, None) is None
        for name in ("frontend_runtime_host", "runtime_manager", "runtime_io")
    ):
        return None
    fork_parent = None
    if resume_predecessor is not None and manifest.host is RuntimeHost.DETACHED_BACKEND:
        predecessor_binding = spawner.registry.store.get_runtime_binding(resume_predecessor.id)
        if predecessor_binding is None or predecessor_binding.native_session_id is None:
            return None
        fork_parent = predecessor_binding.native_session_id
    if req.wiring is RuntimeWiring.AUTO and not wiring_mod.NATIVE_AUTO_SELECTION_ENABLED:
        return None
    try:
        compatibility = await workers.to_thread(
            manifest.probe,
            RuntimeProbeContext(
                participant_id=participant.id, binary=harness.binary, cwd=participant.cwd
            ),
            label="spawn.runtime_probe",
        )
    except Exception as exc:
        logger.warning("native probe for %s failed; using legacy launch: %s", req.harness, exc)
        return None
    if not isinstance(compatibility, RuntimeCompatibility):
        logger.warning(
            "native probe for %s returned invalid compatibility; using legacy", req.harness
        )
        return None
    if not compatibility.supported:
        logger.info(
            "native preference selects legacy for %s: %s", req.harness, compatibility.reason
        )
        return None
    if manifest.host is RuntimeHost.DETACHED_BACKEND and manifest.endpoint_discovery is not None:
        # The backend announces its loopback endpoint on stdout; there is
        # no preselected endpoint to persist at reservation time.
        endpoint: str | None = None
    elif manifest.host is RuntimeHost.DETACHED_BACKEND:
        endpoint = wiring_mod.native_endpoint(participant.id)
    else:
        endpoint = wiring_mod.frontend_endpoint(participant.id)
    return NativeSpawnSelection(
        runtime=manifest,
        endpoint=endpoint,
        backend_generation=wiring_mod.RUNTIME_BACKEND_GENERATION_INITIAL,
        compatibility=compatibility,
        fork_parent_session=fork_parent,
    )


async def launch_native(spawner, reservation: Reservation) -> Participant:
    """Run the detached sequence for one natively-wired spawn under the startup deadline.

    A timeout cleans up like any startup failure unless transmission had begun; a failed
    backend teardown keeps worktree, pane and binding and raises a diagnostic instead.
    """
    participant = reservation.participant
    native = reservation.native
    assert native is not None
    store = spawner.registry.store
    pid = participant.id
    attempt = _LaunchAttempt()
    try:
        attached = await asyncio.wait_for(
            _launch_native_sequence(spawner, reservation, native, participant, attempt),
            NATIVE_LAUNCH_DEADLINE_SECONDS,
        )
    except BaseException as exc:
        if not attempt.dispatch_started and not _dispatch_may_have_begun(store, pid):
            cleaned = await _cleanup_failed_native(spawner, participant, native)
            if cleaned:
                if (
                    not attempt.terminal_create_attempted
                    and reservation.legacy_plan is not None
                    and isinstance(exc, Exception)
                ):
                    logger.warning(
                        "native startup for %s failed before dispatch; using legacy launch: %s",
                        pid,
                        exc,
                    )
                    return await spawner._launch_legacy_fallback(reservation)
                await spawner.cleanup_reservation(participant)
            else:
                raise TheaterError(
                    f"native spawn of {pid!r} failed and its backend or credential "
                    "cleanup could not be verified; the runtime binding, pane, and "
                    "worktree are preserved for inspection — inspect generation "
                    f"{native.backend_generation} before retrying"
                ) from exc
        if isinstance(exc, TimeoutError):
            raise TheaterError(
                f"native spawn of {pid!r} did not complete within "
                f"{NATIVE_LAUNCH_DEADLINE_SECONDS:.0f}s — the backend, UI, or session "
                "discovery stalled; inspect the participant's runtime logs before retrying"
            ) from exc
        raise
    else:
        return attached


async def _launch_native_sequence(
    spawner,
    reservation: Reservation,
    native: NativeSpawnSelection,
    participant: Participant,
    attempt: _LaunchAttempt,
) -> Participant:
    store = spawner.registry.store
    req = reservation.req
    pid = participant.id
    generation = native.backend_generation
    stage_span = partial(
        timing.span,
        LIFECYCLE_STAGE,
        action="spawn",
        id=pid,
        operation_id=None if reservation.provider is None else reservation.provider.operation_id,
    )

    if spawner.runtime_manager is None or spawner.controls is None or spawner.runtime_io is None:
        raise BadRequest(
            f"native wiring for {pid!r} requires the daemon runtime manager, "
            "I/O, and control service; this spawner was composed without them"
        )

    # ---- 2. detached backend ------------------------------------------
    plan, token_file = _prepare_backend_plan(reservation, native, store)
    with stage_span(stage="native_backend"):
        backend = await spawner.runtime_manager.launch_backend(
            pid,
            backend_generation=generation,
            plan=plan,
            cwd=Path(reservation.child_cwd),
        )

    # ---- 3. persist the verified pid + strong start identity ---------
    _persist_backend_start(store, pid, generation, native, backend)

    # ---- 4. endpoint readiness before any runtime connection ----------
    # The endpoint binds after exec, so probe reachability (no protocol) within the one
    # startup deadline; a discovered http endpoint's client owns its own readiness.
    if native.endpoint is not None and native.endpoint.startswith("unix:"):
        with stage_span(stage="native_endpoint"):
            await wait_for_unix_endpoint(native.endpoint, timeout=NATIVE_LAUNCH_DEADLINE_SECONDS)

    # ---- 5. one runtime instance, observer connection, UI plan --------
    with stage_span(stage="native_runtime"):
        runtime = await spawner.runtime_manager.get_or_create(
            pid,
            backend_generation=generation,
            create=_runtime_factory(
                spawner,
                native,
                reservation,
                participant,
                endpoint=native.endpoint if native.endpoint is not None else backend.endpoint,
                token_file=token_file,
            ),
        )
    fork_parent = native.fork_parent_session
    bound_upfront = False
    if native.runtime.session_order is RuntimeSessionOrder.SESSION_FIRST:
        # The exact session is opened through the runtime before the stock
        # UI exists; the frontend then attaches to the exact returned id.
        with stage_span(stage="native_session"):
            binding = await _open_bound_session(
                spawner,
                runtime,
                native,
                store,
                participant,
                generation,
                mode=SessionOpenMode.FORK if fork_parent is not None else SessionOpenMode.NEW,
                native_session_id=fork_parent,
            )
        with stage_span(stage="native_frontend_plan"):
            pane_plan = await runtime.frontend_plan(native_session_id=binding.native_session_id)
        bound_upfront = True
    elif fork_parent is not None:
        # FORK: open the predecessor's exact session first, then attach the
        # UI to the exact returned id (the frozen fork order).
        with stage_span(stage="native_session"):
            binding = await _open_bound_session(
                spawner,
                runtime,
                native,
                store,
                participant,
                generation,
                mode=SessionOpenMode.FORK,
                native_session_id=fork_parent,
            )
        with stage_span(stage="native_frontend_plan"):
            pane_plan = await runtime.frontend_plan(native_session_id=binding.native_session_id)
        bound_upfront = True
    else:
        # NEW: the promptless fresh-native-UI plan completes the observer
        # handshake before the pane exists; the UI creates the session.
        with stage_span(stage="native_frontend_plan"):
            pane_plan = await runtime.frontend_plan(native_session_id=None)

    attached = await _launch_native_terminal_fenced(spawner, reservation, pane_plan, attempt)

    if not bound_upfront:
        # ---- 6. wait for the exact UI-created session -----------------
        with stage_span(stage="native_session"):
            binding = await _open_bound_session(
                spawner,
                runtime,
                native,
                store,
                participant,
                generation,
                mode=SessionOpenMode.NEW,
                native_session_id=None,
            )

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
        attempt.dispatch_started = True
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


def _prepare_backend_plan(
    reservation: Reservation, native: NativeSpawnSelection, store
) -> tuple[RuntimePlan, Path | None]:
    """Validate the backend plan and install participant-scoped MCP configuration."""
    participant, req = reservation.participant, reservation.req
    planner = native.runtime.plan
    if planner is None:
        raise BadRequest("detached native wiring requires a backend planner")
    token_file = _runtime_token_file(store, participant, native)
    plan = planner(
        RuntimePlanningContext(
            participant_id=participant.id,
            cwd=reservation.child_cwd,
            endpoint=native.endpoint,
            token_file=token_file,
            approval=req.approval,
            model=req.model,
            reasoning_effort=req.reasoning_effort,
        )
    )
    if not isinstance(plan, RuntimePlan):
        raise TypeError("runtime manifest planner must return a RuntimePlan")
    _require_endpoint_agreement(plan, native, participant.id)
    backend_plan, fallback_plan = install_runtime_mcp_plans(
        plan.backend, reservation.legacy_plan, participant, store=store
    )
    reservation.legacy_plan = fallback_plan
    return replace(plan, backend=backend_plan), token_file


def _persist_backend_start(
    store, pid: str, generation: int, native: NativeSpawnSelection, backend
) -> None:
    """Record the verified pid, then a discovered endpoint, or fail closed.

    Both writes are guarded by the expected generation: a delayed launch
    callback must never record facts for another generation's backend.
    """
    identity = backend.identity
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
    if native.endpoint is not None:
        return
    # The resolved loopback URL is a generation fact: it must outlive this
    # launch so restart adoption and recovery reconnect to the exact port
    # the backend printed, never a guessed one.
    if not store.record_runtime_endpoint(
        pid,
        backend_generation=generation,
        endpoint=backend.endpoint,
        updated_at=now(),
    ):
        raise TheaterError(
            f"runtime binding generation for {pid!r} changed during launch; "
            "failing closed instead of recording another generation's endpoint"
        )


async def _open_bound_session(
    spawner,
    runtime,
    native: NativeSpawnSelection,
    store,
    participant: Participant,
    generation: int,
    *,
    mode: SessionOpenMode,
    native_session_id: str | None,
):
    """Open the exact session, bind its identity, register live wiring."""
    binding = await runtime.open_session(mode=mode, native_session_id=native_session_id)
    bind_runtime_identity(store, participant.id, binding, generation)
    if not spawner.runtime_manager.mark_session_open(participant.id, runtime, binding):
        raise TheaterError(
            f"runtime for {participant.id!r} changed before its exact session was cached"
        )
    _register_live_wiring(spawner, native, participant.id, runtime, binding)
    return binding


def _require_endpoint_agreement(plan: RuntimePlan, native: NativeSpawnSelection, pid: str) -> None:
    """Fail closed when plan and manifest disagree on endpoint discovery.

    A planner silently flipping the seam would reconnect to an address nothing verified.
    """
    declared = native.runtime.endpoint_discovery is not None
    if declared and (plan.endpoint is not None or plan.endpoint_discovery is None):
        raise BadRequest(
            f"the runtime manifest for {pid!r} declares stdout endpoint discovery but "
            "its planner returned a fixed endpoint plan; refuse to connect to an "
            "address no discovery validated"
        )
    if not declared and (plan.endpoint is None or plan.endpoint_discovery is not None):
        raise BadRequest(
            f"the runtime manifest for {pid!r} declares a fixed endpoint but its "
            "planner returned an endpoint-discovery plan; refuse to accept an "
            "endpoint mode the manifest did not declare"
        )


def _runtime_token_file(
    store, participant: Participant, native: NativeSpawnSelection
) -> Path | None:
    """Locate the core-minted runtime credential file, or fail closed; never read the secret.

    ``None`` when no credential is declared; a declared need without a record is a launch-order
    violation, not a retry.
    """
    from theater.daemon.artifacts import ArtifactKind, validate_persisted_path
    from theater.harness.contracts.channels import ChannelKind

    declaration = native.runtime.runtime_credential
    if declaration is None:
        return None
    record = store.get_channel_credential(
        participant.id, ChannelKind.RUNTIME, declaration.channel_id
    )
    if record is None or not getattr(record, "token_path", None):
        raise BadRequest(
            f"the runtime manifest for {participant.id!r} declares credential "
            f"{declaration.channel_id!r} but no core-minted credential was persisted; "
            "the credential must be minted during reservation before the backend "
            "launches — inspect the daemon log for the failed reservation"
        )
    token_path = Path(record.token_path)
    validate_persisted_path(token_path, owner_id=participant.id, kind=ArtifactKind.FILE)
    return token_path


def _runtime_factory(
    spawner,
    native: NativeSpawnSelection,
    reservation: Reservation,
    participant: Participant,
    *,
    endpoint: str | None,
    token_file: Path | None,
):
    """Bind the manifest factory to one immutable context and injected I/O."""

    async def create():
        context = RuntimeContext(
            participant_id=participant.id,
            cwd=participant.cwd,
            io=spawner.runtime_io,
            backend_generation=native.backend_generation,
            endpoint=endpoint,
            token_file=token_file,
            approval=reservation.req.approval,
            model=reservation.req.model,
            reasoning_effort=reservation.req.reasoning_effort,
        )
        return native.runtime.factory(context)

    return create


def _register_live_wiring(
    spawner,
    native: NativeSpawnSelection,
    participant_id: str,
    runtime,
    binding,
) -> None:
    """Register the runtime's live channel with the observer hub.

    Right after identity binds, so evidence reported before the prompt still reconciles.
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


async def _launch_native_terminal(spawner, reservation: Reservation, pane_plan: LaunchPlan):
    """Create the stock native UI through the selected terminal provider."""
    if reservation.provider is None:
        raise BadRequest("native stock UI launch requires a selected terminal provider")
    attached = await spawner._launch_provider_terminal(reservation, pane_plan)
    return attached, None


async def _launch_native_terminal_fenced(
    spawner,
    reservation: Reservation,
    pane_plan: LaunchPlan,
    attempt: _LaunchAttempt,
) -> Participant:
    """Classify terminal creation before native-backend cleanup."""
    attempt.terminal_create_attempted = True
    attempt.dispatch_started = True
    try:
        attached, _created = await _launch_native_terminal(spawner, reservation, pane_plan)
    except ProviderLaunchOutcome as exc:
        attempt.dispatch_started = exc.outcome.state == "uncertain"
        raise
    except TheaterError as exc:
        details = getattr(exc, "details", None)
        attempt.dispatch_started = (
            isinstance(details, Mapping) and details.get("possibly_executed") is True
        )
        raise
    attempt.dispatch_started = False
    return attached


def _dispatch_may_have_begun(store, participant_id: str) -> bool:
    """Whether any control operation's transmission may have begun.

    ``DISPATCHED`` is persisted before sending and ACCEPTED/UNKNOWN means it happened: past
    this boundary nothing is resent, relaunched, or cleaned.
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
    native: NativeSpawnSelection,
) -> bool:
    """Clean only verified participant-owned resources after a pre-dispatch failure.

    A failed backend teardown returns ``False`` (not best-effort): a possibly running backend
    keeps binding and pane, and the caller raises instead of generic reservation cleanup.
    """
    pid = participant.id
    credential_path: Path | None = None
    declaration = native.runtime.runtime_credential
    if declaration is not None:
        from theater.harness.contracts.channels import ChannelKind

        credential = spawner.registry.store.get_channel_credential(
            pid,
            ChannelKind.RUNTIME,
            declaration.channel_id,
        )
        if credential is not None:
            credential_path = Path(credential.token_path)
    try:
        await spawner.runtime_manager.teardown(
            pid,
            backend_generation=native.backend_generation,
        )
    except Exception:
        logger.exception(
            "backend teardown after failed native spawn of %s failed; the "
            "binding, pane, and worktree are kept — a backend may still be "
            "running in them",
            pid,
        )
        return False
    binding = spawner.registry.store.terminal_bindings.get(pid)
    if binding is not None:
        try:
            result = await spawner.controls.terminate_provider(
                pid,
                caller_id="cli",
                callback_operation_id=f"native-cleanup-{new_id()}",
            )
        except Exception:
            return False
        if result.get("delivery") != "accepted" or result.get("exit_confirmed") is not True:
            return False
    spawner.registry.store.delete_channel_credentials(pid)
    if declaration is not None:
        from theater.harness.contracts.channels import ChannelKind

        if spawner.registry.store.get_channel_credential(
            pid,
            ChannelKind.RUNTIME,
            declaration.channel_id,
        ) is not None or (credential_path is not None and credential_path.exists()):
            logger.error("runtime credential cleanup for %s could not be verified", pid)
            return False
    spawner.registry.store.delete_runtime_binding(pid)
    if spawner.live_hub is not None:
        spawner.live_hub.unregister(pid)
    return True
