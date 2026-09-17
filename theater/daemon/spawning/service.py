"""Spawner orchestration: reserve, launch, rollback, kill, teardown.

The spawn is split into ``reserve`` and ``launch`` so the daemon can
create the spawn **job** between them — before the pane exists.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from theater import paths, timing
from theater.constants.daemon import (
    BUS_KIND_PARTICIPANT_SESSION_BOUNDARY,
    TMUX_RESTART_TERMINATION_REASON,
)
from theater.constants.harness import (
    SPAWN_KILL_POLL_ATTEMPTS,
    SPAWN_KILL_POLL_INTERVAL_SECONDS,
)
from theater.constants.tmux import TMUX_DEFAULT_SESSION
from theater.daemon import workers
from theater.daemon import worktrees as worktree_mod
from theater.daemon.operations import (
    DispatchIntent,
    OperationAcceptance,
    OperationOutcome,
    OperationService,
    PreparedOperation,
)
from theater.daemon.operations.projection import operation_event_payload
from theater.daemon.persistence.repositories._json import decode_json, encode_json
from theater.daemon.persistence.transactions import WriteUnit
from theater.daemon.registry import Registry
from theater.daemon.schema import jobs as jobs_table
from theater.daemon.schema import launch_reservations, participants
from theater.daemon.spawning.frontend import start_frontend_listener
from theater.daemon.spawning.hook_compatibility import probe_hook_channels
from theater.daemon.spawning.models import (
    NativeSpawnSelection,
    ProviderLaunchOutcome,
    ProviderLaunchSelection,
    Reservation,
    SpawnRequest,
)
from theater.daemon.spawning.native import (
    launch_native,
    select_native_wiring,
)
from theater.daemon.spawning.planning import (
    build_plan,
    install_frontend_plan,
    install_hook_plan,
    install_otel_plan,
    record_launch_identity,
    record_plan_artifacts,
    resolve_pane_command,
    validate_receipt_plan,
    write_plan_files,
)
from theater.daemon.spawning.resume import (
    capture_resume_floor,
    reject_unsafe_resume_shape,
    resolve_resume_reference,
    validate_before_create,
)
from theater.daemon.terminals import ProviderUnavailable, TerminalIdentityMismatch
from theater.daemon.worktrees.service import WorkspaceRequest, WorkspaceReservation
from theater.frontend.capabilities import TERMINAL_PROVIDER_CAPABILITY
from theater.harness import get as get_harness
from theater.harness.base import LaunchPlan, ResumeLaunchOverlay
from theater.harness.contracts.channels import ChannelKind
from theater.harness.contracts.runtime import RuntimeHost, RuntimeLifecyclePhase, RuntimeWiring
from theater.models import (
    BadRequest,
    Job,
    JobState,
    JournalEventRecord,
    LaunchReservationRecord,
    Participant,
    ParticipantOrigin,
    PublicOperationRecord,
    Status,
    TerminalBindingRecord,
    TheaterError,
    Tier,
    WorkspaceState,
    WorkspaceUsageHolderKind,
    WorkspaceUsageRecord,
    new_id,
    now,
)
from theater.observability.catalog import KILL_PANE, KILL_TEARDOWN, SPAWN_LAUNCH, SPAWN_WORKTREE
from theater.provenance import is_trusted_provenance
from theater.tmux import client as tmux

if TYPE_CHECKING:
    from theater.daemon.runtime.tmux_reconcile import TmuxReconciliation

logger = logging.getLogger("theater.spawner")


@dataclass(frozen=True, slots=True)
class _SpawnAdmission:
    provider_id: str
    provider_generation: int
    provider_selector: str
    parent_id: str | None
    workspace_request: WorkspaceRequest
    request: SpawnRequest
    resume_predecessor: Participant | None
    resume_overlay: ResumeLaunchOverlay | None
    cwd: str


async def _uncancellable(fn, /, *args, reconcile=None, **kwargs):
    """Await ``fn`` so cancellation does not release the caller's lock until
    the worker finishes and state is reconciled."""
    task = asyncio.create_task(fn(*args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        result = await task
        if reconcile is not None:
            reconcile(result)
        raise


class Spawner:
    #: Poll attempts when confirming a pane is gone after kill-pane.
    KILL_POLL_ATTEMPTS = SPAWN_KILL_POLL_ATTEMPTS

    #: Interval between kill-pane confirmation polls, in seconds.
    KILL_POLL_INTERVAL = SPAWN_KILL_POLL_INTERVAL_SECONDS

    def __init__(
        self,
        registry: Registry,
        *,
        otel_runtime=None,
        reconcile_tmux: Callable[[], Awaitable[TmuxReconciliation]] | None = None,
        tmux_reconcile_lock: asyncio.Lock | None = None,
        runtime_manager=None,
        runtime_io=None,
        frontend_runtime_host=None,
        controls=None,
        live_hub=None,
        workspace_service=None,
    ):
        self.registry = registry
        self.otel_runtime = otel_runtime
        self._reconcile_tmux = reconcile_tmux
        self._tmux_reconcile_lock = tmux_reconcile_lock
        # Native runtime wiring collaborators, injected by the daemon
        # composition root. ``None`` keeps every spawn legacy — the exact
        # behaviour of a spawner built before runtime wiring existed.
        self.runtime_manager = runtime_manager
        self.runtime_io = runtime_io
        self.frontend_runtime_host = frontend_runtime_host
        self.controls = controls
        # The observer's live-channel hub: the native sequence registers a
        # participant's runtime live source here once its exact identity is
        # bound. ``None`` keeps a spawner composed without observation live.
        self.live_hub = live_hub
        self.workspace_service = workspace_service
        self._named_locks: dict[str, asyncio.Lock] = {}
        self._provisional_named_worktrees: set[str] = set()
        self._joined_named_worktrees: set[str] = set()

    def _named_lock(self, repo_root: str) -> asyncio.Lock:
        return self._named_locks.setdefault(repo_root, asyncio.Lock())

    async def reserve(self, req: SpawnRequest) -> Reservation:
        """Create the participant, worktree, plan, and config files.

        On failure the participant is marked DEAD and any worktree retired.
        """
        harness = get_harness(req.harness)
        if shutil.which(harness.binary) is None:
            raise BadRequest(f"{harness.binary!r} is not on PATH")
        req = self._resolve_resume_reference(req)
        resume_predecessor, resume_overlay = self._validate_before_create(req, harness)
        if resume_overlay is not None and resume_overlay.cwd is not None:
            req = replace(req, cwd=resume_overlay.cwd)
        description = req.description
        if description is None and resume_predecessor is not None:
            description = resume_predecessor.description
        try:
            participant = self.registry.create_spawned(
                harness=req.harness,
                cwd=req.cwd,
                parent_id=req.parent_id,
                has_prompt=bool(req.prompt),
                resumed_from_id=resume_predecessor.id if resume_predecessor is not None else None,
                name=req.name,
                description=description,
            )
        except IntegrityError:
            if resume_predecessor is None:
                raise
            raise BadRequest(
                f"cannot resume participant {resume_predecessor.id!r}: a live successor already "
                "claims this recovery"
            ) from None

        native: NativeSpawnSelection | None = None
        legacy_plan: LaunchPlan | None = None
        try:
            with timing.span(SPAWN_WORKTREE, id=participant.id, kind=req.worktree or None):
                child_cwd = await self._prepare_worktree(req, participant)
            native = await self._select_native_wiring(req, harness, participant, resume_predecessor)
            plan, native, legacy_plan = await self._plan_for_wiring(
                req,
                participant,
                resume_overlay,
                harness,
                native,
            )
            paths.ensure_home()
            if native is None or native.runtime.host is RuntimeHost.FRONTEND:
                self._record_plan_artifacts(participant, plan)
                self._record_launch_identity(participant, plan, harness.observer)
                self._write_plan_files(plan)
            if resume_predecessor is not None:
                # The resume floor is durable-observation policy, shared by
                # both wirings: a fork's observer attaches at the predecessor's
                # stream position, whatever transport delivered the prompt.
                participant.resume_floor = self._capture_resume_floor(harness, resume_predecessor)
                self.registry.store.upsert_participant(participant)

            session = await self._resolve_session(req.tmux_session, child_cwd)
            name = req.window_name or f"{req.harness}-{participant.id[:6]}"
        except BaseException:
            if native is not None:
                # A reservation that never reached launch transmitted nothing;
                # the intent row is verified participant-owned state.
                self.registry.store.delete_runtime_binding(participant.id)
            await self.cleanup_reservation(participant)
            raise

        self._provisional_named_worktrees.discard(participant.id)
        self._joined_named_worktrees.discard(participant.id)
        return Reservation(
            participant=participant,
            plan=plan,
            child_cwd=child_cwd,
            session=session,
            name=name,
            req=req,
            resume_predecessor=resume_predecessor,
            native=native,
            legacy_plan=legacy_plan,
        )

    async def prepare_provider_launch(
        self,
        req: SpawnRequest,
        participant: Participant,
        *,
        child_cwd: str,
        provider: ProviderLaunchSelection,
        workspace_usage_id: str | None,
        resume_predecessor: Participant | None = None,
        resume_overlay: ResumeLaunchOverlay | None = None,
        prevalidated: bool = False,
    ) -> Reservation:
        """Build durable harness wiring for an already-reserved public participant."""
        harness = get_harness(req.harness)
        if shutil.which(harness.binary) is None:
            raise BadRequest(f"{harness.binary!r} is not on PATH")
        req = replace(req, cwd=child_cwd, worktree=False)
        if prevalidated:
            predecessor, overlay = resume_predecessor, resume_overlay
        else:
            req = self._resolve_resume_reference(req)
            predecessor, overlay = self._validate_before_create(req, harness)
        if predecessor is not None and participant.resumed_from_id != predecessor.id:
            raise BadRequest("reserved participant resume identity changed during preparation")
        native = await self._select_native_wiring(req, harness, participant, predecessor)
        plan, native, legacy_plan = await self._plan_for_wiring(
            req, participant, overlay, harness, native
        )
        paths.ensure_home()
        if native is None or native.runtime.host is RuntimeHost.FRONTEND:
            self._record_plan_artifacts(participant, plan)
            self._record_launch_identity(participant, plan, harness.observer)
            self._write_plan_files(plan)
        if predecessor is not None:
            participant.resume_floor = self._capture_resume_floor(harness, predecessor)
            self.registry.store.upsert_participant(participant)
        return Reservation(
            participant=participant,
            plan=plan,
            child_cwd=child_cwd,
            session="",
            name=req.window_name or f"{req.harness}-{participant.id[:6]}",
            req=req,
            resume_predecessor=predecessor,
            native=native,
            legacy_plan=legacy_plan,
            provider=provider,
            workspace_usage_id=workspace_usage_id,
        )

    async def _plan_for_wiring(
        self,
        req: SpawnRequest,
        participant: Participant,
        resume_overlay: ResumeLaunchOverlay | None,
        harness,
        native: NativeSpawnSelection | None,
    ) -> tuple[LaunchPlan, NativeSpawnSelection | None, LaunchPlan | None]:
        detached = native is not None and native.runtime.host is RuntimeHost.DETACHED_BACKEND
        fallback_plan: LaunchPlan | None = None
        try:
            plan = self._build_plan(
                req,
                participant,
                resume_overlay,
                include_sidecars=not detached,
            )
            minted_token = self._validate_receipt_plan(plan, participant)
            if minted_token is not None:
                plan = replace(plan, receipt_token=minted_token)
            plan = self._install_hook_plan(
                plan,
                participant,
                harness.observer,
                enabled_channels=await probe_hook_channels(
                    participant,
                    harness,
                    native_enabled=req.wiring != RuntimeWiring.LEGACY,
                ),
            )
            plan = self._install_otel_plan(plan, participant, harness.observer)
            fallback_plan = plan
        except Exception as exc:
            if not detached:
                raise
            logger.warning(
                "ordinary fallback planning for %s failed; continuing native-only: %s",
                participant.id,
                exc,
            )
            plan = LaunchPlan(argv=[])
        if detached:
            assert native is not None
            self._mint_runtime_credential(participant, req, native.runtime)
            self._persist_launch_intent(participant, req, native)
            return LaunchPlan(argv=[]), native, fallback_plan
        if native is None:
            return plan, None, None
        legacy_plan = plan
        try:
            plan = install_frontend_plan(
                plan,
                participant,
                native.runtime,
                native.endpoint,
            )
        except Exception as exc:
            logger.warning(
                "passive frontend setup for %s failed; using legacy launch: %s",
                participant.id,
                exc,
            )
            return legacy_plan, None, None
        self._persist_launch_intent(participant, req, native)
        return plan, native, legacy_plan

    async def launch(self, reservation: Reservation) -> Participant:
        """Create the tmux window and attach the pane.

        On failure the participant is marked DEAD. Worktrees survive once launch begins.
        """
        participant = reservation.participant
        try:
            if (
                reservation.native is not None
                and reservation.native.runtime.host is RuntimeHost.DETACHED_BACKEND
            ):
                # The detached native sequence owns the pane, the initial
                # prompt, and its own complete failure ordering: backend
                # teardown, then the pane, then the binding, and only then —
                # and only if the teardown verified — the generic reservation
                # cleanup below. Once the initial prompt's transmission may
                # have begun, the native sequence cleans nothing.
                attached = await launch_native(self, reservation)
            elif reservation.native is not None:
                attached = await self._launch_frontend(reservation)
            else:
                attached = await self._launch_ordinary_pane(reservation)
        except BaseException:
            # Native failures are fully handled inside ``launch_native``;
            # this generic reservation cleanup is the legacy path only.
            if reservation.provider is not None:
                pass
            elif reservation.native is None:
                self._preserve_failed_launch(participant)
            elif reservation.native.runtime.host is RuntimeHost.FRONTEND:
                await self._close_frontend_launch(participant.id)
                self.registry.store.delete_runtime_binding(participant.id)
                self._preserve_failed_launch(participant)
            raise
        if attached.status is Status.DEAD:
            if reservation.native is not None:
                await self._close_frontend_launch(participant.id)
                self.registry.store.delete_runtime_binding(participant.id)
            self._preserve_failed_launch(participant)
            raise TheaterError("tmux server restarted or the new pane exited during spawn")
        predecessor = reservation.resume_predecessor
        if predecessor is not None:
            try:
                self.registry.store.bus_append(
                    BUS_KIND_PARTICIPANT_SESSION_BOUNDARY,
                    from_id=predecessor.id,
                    to_id=participant.id,
                    payload={"reason": "resume", "predecessor_id": predecessor.id},
                )
            except Exception:
                logger.exception("could not record resume boundary for %s", participant.id)
        return attached

    async def _launch_frontend(self, reservation: Reservation) -> Participant:
        native = reservation.native
        assert native is not None
        if (
            self.frontend_runtime_host is None
            or self.runtime_manager is None
            or self.runtime_io is None
        ):
            raise BadRequest("frontend wiring requires the daemon frontend runtime host")
        credential = self.registry.store.get_channel_credential(
            reservation.participant.id,
            ChannelKind.LIVE,
            native.runtime.channel.channel.id,
        )
        if credential is None:
            raise BadRequest("frontend wiring requires its launch channel credential")
        try:
            await start_frontend_listener(
                host=self.frontend_runtime_host,
                runtime_manager=self.runtime_manager,
                runtime_io=self.runtime_io,
                live_hub=self.live_hub,
                store=self.registry.store,
                participant=reservation.participant,
                runtime=native.runtime,
                generation=native.backend_generation,
                endpoint=native.endpoint,
                approval=reservation.req.approval,
                model=reservation.req.model,
                reasoning_effort=reservation.req.reasoning_effort,
                token=credential.token,
            )
        except Exception:
            await self._close_frontend_launch(reservation.participant.id)
            self.registry.store.delete_runtime_binding(reservation.participant.id)
            if reservation.legacy_plan is None:
                raise
            fallback = replace(
                reservation,
                plan=reservation.legacy_plan,
                native=None,
                legacy_plan=None,
            )
            return await self._launch_ordinary_pane(fallback)
        try:
            attached = await self._launch_ordinary_pane(reservation)
        except ProviderLaunchOutcome as exc:
            if exc.outcome.state != "uncertain":
                await self._close_frontend_launch(reservation.participant.id)
            raise
        except BaseException:
            await self._close_frontend_launch(reservation.participant.id)
            raise
        phase = (
            RuntimeLifecyclePhase.ACTIVE
            if reservation.req.prompt
            else RuntimeLifecyclePhase.ATTACHED
        )
        if not self.registry.store.set_runtime_lifecycle(
            reservation.participant.id,
            phase,
            backend_generation=native.backend_generation,
            updated_at=now(),
        ):
            await self._close_frontend_launch(reservation.participant.id)
            raise TheaterError("frontend runtime binding changed during pane launch")
        return attached

    async def _launch_ordinary_pane(self, reservation: Reservation) -> Participant:
        if reservation.provider is not None:
            return await self._launch_pane(reservation)
        if self._tmux_reconcile_lock is None:
            attached = await self._launch_pane(reservation)
        else:
            async with self._tmux_reconcile_lock:
                attached = await self._launch_pane(reservation)
        if self._reconcile_tmux is not None:
            await self._reconcile_tmux()
            attached = self.registry.get(reservation.participant.id)
        return attached

    async def _launch_legacy_fallback(self, reservation: Reservation) -> Participant:
        plan = reservation.legacy_plan
        if plan is None:
            raise TheaterError("native startup has no validated legacy fallback plan")
        harness = get_harness(reservation.req.harness)
        participant = self.registry.store.get_participant(reservation.participant.id)
        if participant is None:
            raise TheaterError("native startup participant vanished before legacy fallback")
        participant.session_id = None
        participant.session_correlation = None
        try:
            self.registry.store.upsert_participant(participant)
            self._record_plan_artifacts(participant, plan)
            self._record_launch_identity(participant, plan, harness.observer)
            self._write_plan_files(plan)
            fallback = replace(
                reservation,
                participant=participant,
                plan=plan,
                native=None,
                legacy_plan=None,
            )
            attached = await self._launch_ordinary_pane(fallback)
        except ProviderLaunchOutcome:
            raise
        except BaseException:
            self._preserve_failed_launch(participant)
            raise
        if attached.status is Status.DEAD:
            self._preserve_failed_launch(participant)
            raise TheaterError("tmux server restarted or the fallback pane exited during spawn")
        return attached

    async def _close_frontend_launch(self, participant_id: str) -> None:
        if self.frontend_runtime_host is not None:
            await self.frontend_runtime_host.close(participant_id)
        if self.runtime_manager is not None:
            await self.runtime_manager.close(participant_id)
        if self.live_hub is not None:
            self.live_hub.unregister(participant_id)

    async def _launch_pane(self, reservation: Reservation) -> Participant:
        participant = reservation.participant
        if reservation.provider is not None:
            return await self._launch_provider_pane(reservation, reservation.plan)
        with timing.span(SPAWN_LAUNCH, id=participant.id, harness=participant.harness):
            created = await tmux.new_window_with_identity(
                session=reservation.session,
                name=reservation.name,
                cwd=reservation.child_cwd,
                command=reservation.plan.argv,
                env={**reservation.plan.env, "THEATER_ID": participant.id},
                background=reservation.req.background,
            )
        return self.registry.attach_pane(
            participant.id,
            created.pane_id,
            pane_pid=created.pane_pid,
            tmux_server_identity=created.server_identity,
        )

    async def _launch_provider_pane(
        self, reservation: Reservation, plan: LaunchPlan
    ) -> Participant:
        """Create one terminal through the selected immutable provider generation."""
        provider = reservation.provider
        if provider is None:
            raise RuntimeError("provider launch selection is missing")
        command = resolve_pane_command(plan)
        if not command:
            raise BadRequest("terminal launch plan has no executable")
        params = {
            "operation_id": provider.operation_id,
            "provider_generation": provider.provider_generation,
            "participant_id": reservation.participant.id,
            "launch_id": provider.launch_id,
            "launch": {
                "executable": command[0],
                "argv": command,
                "cwd": reservation.child_cwd,
                "environment": {**plan.env, "THEATER_ID": reservation.participant.id},
                "presentation": {
                    "name": reservation.name,
                    "background": reservation.req.background,
                },
            },
        }
        provider.mark_dispatched()
        with timing.span(
            SPAWN_LAUNCH,
            id=reservation.participant.id,
            harness=reservation.participant.harness,
        ):
            outcome = await provider.terminal_service.dispatch_operation(
                provider.provider_id,
                provider.provider_generation,
                "terminal.create",
                params,
            )
        if outcome.state != "succeeded" or not isinstance(outcome.result, Mapping):
            raise ProviderLaunchOutcome(outcome)
        terminal = outcome.result.get("terminal")
        if not isinstance(terminal, Mapping):
            raise ProviderLaunchOutcome(
                OperationOutcome.uncertain(
                    phase="provider_identity_missing",
                    error={
                        "code": "terminal_identity_mismatch",
                        "message": "provider accepted terminal creation without terminal identity",
                    },
                )
            )
        if (
            terminal.get("provider_id") != provider.provider_id
            or terminal.get("provider_generation") != provider.provider_generation
        ):
            raise ProviderLaunchOutcome(
                OperationOutcome.uncertain(
                    phase="provider_identity_mismatch",
                    error={
                        "code": "terminal_identity_mismatch",
                        "message": "provider returned a terminal owned by another generation",
                    },
                )
            )
        return provider.bind_terminal(terminal)

    async def spawn(self, req: SpawnRequest) -> Participant:
        """Reserve then launch in one call."""
        reservation = await self.reserve(req)
        return await self.launch(reservation)

    async def _prepare_worktree(self, req: SpawnRequest, participant: Participant) -> str:
        """Create the worktree (if requested) and return the child cwd."""
        child_cwd = req.cwd
        if not req.worktree:
            return child_cwd

        root = await workers.to_thread(worktree_mod.repo_root, req.cwd, label="spawn.repo_root")
        if root is None:
            raise BadRequest(f"cannot create worktree: {req.cwd!r} is not in a git repo")
        if isinstance(req.worktree, str):
            child_cwd, participant.branch = await self._spawn_named_worktree(
                root=root,
                name=req.worktree,
                base_branch=req.base_branch,
                reservation_id=participant.id,
            )
        else:
            child_cwd = await workers.to_thread(
                worktree_mod.create_worktree,
                repo_root=root,
                child_id=participant.id,
                base_branch=req.base_branch,
                label="spawn.create_worktree",
            )
            participant.branch = worktree_mod.branch_name(participant.id)
        participant.cwd = child_cwd
        self.registry.store.upsert_participant(participant)
        self.registry.store.bus_append(
            "participant.worktree",
            to_id=participant.id,
            payload={
                "path": child_cwd,
                "branch": participant.branch,
                "root": root,
                "named": isinstance(req.worktree, str),
                "name": req.worktree if isinstance(req.worktree, str) else None,
            },
        )
        return child_cwd

    def _validate_receipt_plan(self, plan: LaunchPlan, participant: Participant) -> str | None:
        """Pre-flight receipt plan validation via the planning module."""
        return validate_receipt_plan(plan, participant)

    def _build_plan(
        self,
        req: SpawnRequest,
        participant: Participant,
        overlay: ResumeLaunchOverlay | None,
        *,
        include_sidecars: bool = True,
    ) -> LaunchPlan:
        """Launch plan construction via the planning module."""
        return build_plan(
            req,
            participant,
            overlay,
            registry=self.registry if include_sidecars else None,
        )

    @staticmethod
    def _install_hook_plan(
        plan: LaunchPlan,
        participant: Participant,
        observer,
        *,
        enabled_channels: frozenset[str] | None = None,
    ) -> LaunchPlan:
        """Apply generic launch-local hook installation."""
        return install_hook_plan(plan, participant, observer, enabled_channels=enabled_channels)

    def _install_otel_plan(
        self,
        plan: LaunchPlan,
        participant: Participant,
        observer,
    ) -> LaunchPlan:
        """Apply generic launch-local native OTel installation."""
        return install_otel_plan(plan, participant, observer, self.otel_runtime)

    @staticmethod
    def _write_plan_files(plan: LaunchPlan) -> None:
        """Plan file writing via the planning module."""
        write_plan_files(plan)

    def _record_launch_identity(self, participant: Participant, plan: LaunchPlan, observer) -> None:
        """Identity recording via the planning module."""
        record_launch_identity(
            participant,
            plan,
            self.registry,
            runtime=self.otel_runtime,
            observer=observer,
        )

    def _record_plan_artifacts(self, participant: Participant, plan: LaunchPlan) -> None:
        """Persist launch artifact ownership before file writes."""
        record_plan_artifacts(participant, plan, self.registry)

    def _resolve_resume_reference(self, req: SpawnRequest) -> SpawnRequest:
        """Resume reference resolution via the resume module."""
        return resolve_resume_reference(req, self.registry)

    def _validate_before_create(
        self, req: SpawnRequest, harness
    ) -> tuple[Participant | None, ResumeLaunchOverlay | None]:
        """Refuse unsafe launches before a participant or worktree exists."""
        return validate_before_create(req, harness, self.registry)

    @staticmethod
    def _reject_unsafe_resume_shape(req: SpawnRequest, harness) -> None:
        """Refuse resume combinations that are unsafe or silently dropped."""
        reject_unsafe_resume_shape(req, harness)

    @staticmethod
    def _capture_resume_floor(harness, predecessor: Participant) -> str:
        """Resume floor capture via the resume module."""
        return capture_resume_floor(harness, predecessor)

    async def _select_native_wiring(
        self,
        req: SpawnRequest,
        harness,
        participant: Participant,
        resume_predecessor: Participant | None,
    ):
        """Native wiring selection via the native module."""
        return await select_native_wiring(self, req, harness, participant, resume_predecessor)

    def _persist_launch_intent(self, participant: Participant, req: SpawnRequest, native) -> None:
        """Persist the launch intent (INTENDED) before the backend can start.

        The durable transaction boundary of the accepted UI-first order: the
        binding row with its wiring, generation, private endpoint, and
        launch-policy facts exists before any process is spawned, so a crash
        between reservation and backend start still leaves recoverable
        intent. Launch policy carries approval/model selection facts only —
        never secrets.
        """
        from theater.daemon.controls.routing import manifest_control_routes
        from theater.daemon.persistence.repositories.runtime_bindings import (
            ParticipantRuntimeBinding,
            encode_launch_policy,
        )

        launch_policy = encode_launch_policy(
            {
                "runtime_host": native.runtime.host.value,
                "control_routes": manifest_control_routes(native.runtime),
                **{
                    key: value
                    for key, value in (
                        ("approval", req.approval),
                        ("model", req.model),
                        ("reasoning_effort", req.reasoning_effort),
                    )
                    if value is not None
                },
            }
        )
        self.registry.store.upsert_runtime_binding(
            ParticipantRuntimeBinding(
                participant_id=participant.id,
                harness=req.harness,
                wiring=RuntimeWiring.NATIVE,
                backend_generation=native.backend_generation,
                lifecycle=RuntimeLifecyclePhase.INTENDED,
                endpoint=native.endpoint,
                native_version=native.compatibility.native_version,
                compatibility_policy=native.compatibility.policy,
                launch_policy=launch_policy,
                created_at=now(),
                updated_at=now(),
            )
        )

    def _mint_runtime_credential(
        self, participant: Participant, req: SpawnRequest, manifest
    ) -> None:
        """Core-mint one participant runtime secret and persist its record.

        The token is written 0600 into the participant's runtime artifacts
        """
        import os
        import secrets

        from theater.daemon.harness_runtime.backend import backend_artifacts_dir

        declaration = manifest.runtime_credential
        if declaration is None:
            return
        token = secrets.token_urlsafe(32)
        token_path = backend_artifacts_dir(participant.id) / "runtime.token"
        paths.ensure_private_file(token_path)
        fd = os.open(token_path, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.write(fd, token.encode("utf-8"))
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self.registry.store.set_channel_credential(
            participant.id,
            harness=req.harness,
            kind=ChannelKind.RUNTIME,
            channel_id=declaration.channel_id,
            token=token,
            token_path=str(token_path),
        )

    async def cleanup_reservation(self, participant: Participant) -> None:
        """Rollback a proven never-dispatched reservation and its worktree."""
        current = self.registry.store.get_participant(participant.id)
        if current is not None and current.termination_reason == TMUX_RESTART_TERMINATION_REASON:
            self._provisional_named_worktrees.discard(participant.id)
            self._joined_named_worktrees.discard(participant.id)
            return
        participant = current or participant
        discard_named_branch = participant.id in self._provisional_named_worktrees
        preserve_joined_worktree = participant.id in self._joined_named_worktrees
        try:
            if not preserve_joined_worktree:
                if discard_named_branch:
                    await self._retire(participant, delete_branch=True, delete_named_branch=True)
                else:
                    await self._retire(
                        participant,
                        delete_branch=True,
                        delete_named_branch=False,
                    )
        except BaseException:
            logger.warning(
                "retire raised for %s; proceeding to mark_dead",
                participant.id,
                exc_info=True,
            )
        finally:
            self._provisional_named_worktrees.discard(participant.id)
            self._joined_named_worktrees.discard(participant.id)
        self.registry.mark_dead(participant.id)

    def _preserve_failed_launch(self, participant: Participant) -> None:
        """Retain resources once terminal dispatch may have begun."""
        self._provisional_named_worktrees.discard(participant.id)
        self._joined_named_worktrees.discard(participant.id)
        self.registry.mark_dead(participant.id)

    async def _resolve_session(self, requested: str | None, cwd: str) -> str:
        """Adopt the caller's session when there is one; never nest a server."""
        if requested:
            existing = await tmux.sessions()
            if requested in existing:
                return requested
        return await tmux.ensure_session(TMUX_DEFAULT_SESSION, cwd=cwd)

    async def _spawn_named_worktree(
        self,
        *,
        root: str,
        name: str,
        base_branch: str | None,
        reservation_id: str | None = None,
    ) -> tuple[str, str]:
        """Create or join a named shared worktree, serialized per repo."""
        canonical_root = (
            await workers.to_thread(
                worktree_mod.main_repo_root, root, label="spawn.named.main_repo_root"
            )
            or root
        )

        async with self._named_lock(canonical_root):
            store = self.registry.store
            existing = store.get_named_worktree(repo_root=canonical_root, name=name)

            if existing is not None:
                if base_branch is not None:
                    persisted_base = existing["base_branch"]
                    if persisted_base is None or base_branch != persisted_base:
                        raise BadRequest(
                            f"named worktree {name!r} was created with "
                            f"base_branch={persisted_base!r}; cannot join with "
                            f"base_branch={base_branch!r}"
                        )

                await _uncancellable(
                    workers.to_thread,
                    worktree_mod.verify_named_worktree,
                    label="spawn.named.verify",
                    repo_root=canonical_root,
                    name=name,
                    expected_path=existing["path"],
                    expected_branch=existing["branch"],
                )
                if reservation_id is not None:
                    self._joined_named_worktrees.add(reservation_id)
                return existing["path"], existing["branch"]

            def record_created(result: tuple[str, str]) -> None:
                store.upsert_named_worktree(
                    repo_root=canonical_root,
                    name=name,
                    branch=result[1],
                    path=result[0],
                    base_branch=base_branch,
                )
                if reservation_id is not None:
                    self._provisional_named_worktrees.add(reservation_id)

            path, branch = await _uncancellable(
                workers.to_thread,
                worktree_mod.create_named_worktree,
                label="spawn.named.create",
                repo_root=canonical_root,
                name=name,
                base_branch=base_branch,
                reconcile=record_created,
            )
            record_created((path, branch))
            return path, branch

    async def kill_pane(
        self,
        participant_id: str,
        *,
        expected_server_identity: str | None,
        expected_pane_pid: int | None,
    ) -> Participant:
        """Kill the tmux pane and confirm it is gone."""
        p = self.registry.get(participant_id)
        if p.tmux_pane:
            if (
                expected_server_identity is None
                or p.tmux_server_identity != expected_server_identity
                or expected_pane_pid is None
                or p.pid != expected_pane_pid
            ):
                raise BadRequest(
                    f"cannot kill {participant_id!r}: tmux pane ownership is not verified"
                )
            with timing.span(KILL_PANE, id=p.id, pane=p.tmux_pane, harness=p.harness) as sp:
                if not await tmux.kill_pane_if_identity(
                    p.tmux_pane,
                    expected_server_identity,
                    expected_pane_pid,
                ):
                    raise TheaterError(
                        f"cannot kill {participant_id!r}: tmux pane ownership changed before kill"
                    )
                for attempt in range(self.KILL_POLL_ATTEMPTS):
                    sp["attempts"] = attempt + 1
                    info = await tmux.pane_info(p.tmux_pane)
                    if info is None:
                        break
                    await asyncio.sleep(self.KILL_POLL_INTERVAL)
                else:
                    raise TheaterError(
                        f"pane {p.tmux_pane} of {participant_id!r} survived "
                        f"kill-pane; record left alive to avoid a ghost"
                    )
        return p

    async def teardown(self, p: Participant) -> None:
        """Terminal teardown after the pane is confirmed gone."""
        with timing.span(KILL_TEARDOWN, id=p.id, harness=p.harness):
            self.release_workspace_usage(p, reason="participant_exit")
            self.registry.mark_dead(p.id)

    def release_workspace_usage(self, p: Participant, *, reason: str) -> None:
        """Release durable usage after the participant's execution is proven over."""
        if self.workspace_service is None or p.workspace_id is None:
            return
        try:
            self.workspace_service.release_participant_usage(
                workspace_id=p.workspace_id,
                participant_id=p.id,
                reason=reason,
            )
        except Exception:
            logger.exception(
                "workspace usage release failed for %s; retaining it for reconciliation",
                p.id,
            )

    async def retire(self, p: Participant, *, delete_branch: bool) -> None:
        """Rollback a worktree before terminal dispatch.

        Runtime exit and kill paths must not call this method. Named worktrees
        retain their branch unless the caller proves it created the reservation.
        """
        await self._retire(p, delete_branch=delete_branch, delete_named_branch=False)

    async def _retire(
        self,
        p: Participant,
        *,
        delete_branch: bool,
        delete_named_branch: bool,
    ) -> None:
        if not (p.branch and p.branch.startswith(worktree_mod.BRANCH_PREFIX)):
            return

        named = None
        if self.registry is not None and self.registry.store is not None:
            named = self.registry.store.named_worktree_by_path(p.cwd or "")

        if named is not None:
            root = named["repo_root"]
        else:
            root = await workers.to_thread(
                worktree_mod.main_repo_root,
                p.cwd or "",
                child_id=p.id,
                label="retire.main_repo_root",
            )

        if root is None:
            logger.warning(
                "cannot retire worktree for %s: no repo root from cwd %r",
                p.id,
                p.cwd,
            )
            return

        if named is not None:
            async with self._named_lock(root):
                live = self.registry.store.live_participants_in_cwd(p.cwd or "")
                others = [x for x in live if x.id != p.id]
                if others:
                    logger.info(
                        "not removing named worktree %r for %s: %d other live "
                        "participant(s) still share cwd %s",
                        named["name"],
                        p.id,
                        len(others),
                        p.cwd,
                    )
                    return
                result = await _uncancellable(
                    workers.to_thread,
                    worktree_mod.remove_named_worktree,
                    label="retire.remove_named",
                    repo_root=root,
                    name=named["name"],
                    delete_branch=delete_named_branch,
                    reconcile=lambda r: (
                        self.registry.store.delete_named_worktree(
                            repo_root=named["repo_root"], name=named["name"]
                        )
                        if r.ok
                        else None
                    ),
                )
                if result.ok:
                    self.registry.store.delete_named_worktree(
                        repo_root=named["repo_root"], name=named["name"]
                    )
        else:
            result = await workers.to_thread(
                worktree_mod.remove_worktree,
                repo_root=root,
                child_id=p.id,
                delete_branch=delete_branch,
                label="retire.remove",
            )

        if not result.ok:
            logger.warning(
                "worktree cleanup incomplete for %s "
                "(directory removed: %s, branch removed: %s): %s",
                p.id,
                result.worktree_removed,
                result.branch_removed,
                "; ".join(result.errors) or "no git error reported",
            )


class ParticipantLaunchService:
    """RC10 public spawn/adoption orchestration over existing daemon services."""

    DEFAULT_PROVIDER_SELECTOR = "tmux"

    def __init__(self, daemon) -> None:
        self.daemon = daemon
        self.store = daemon.store
        self.registry = daemon.registry
        self.spawner = daemon.spawner
        self.operations: OperationService = daemon.operation_service
        self.terminals = daemon.terminal_service
        self.workspaces = daemon.workspace_service
        terminal_config = getattr(getattr(daemon, "config", None), "terminals", None)
        configured_default = getattr(terminal_config, "default_provider", None)
        self.default_provider_selector = (
            configured_default
            if isinstance(configured_default, str) and configured_default
            else self.DEFAULT_PROVIDER_SELECTOR
        )

    def spawn(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        params: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Accept a spawn and detach all filesystem/provider work from the request."""
        captured: dict[str, object] = {}

        def prepare(operation_id: str, unit: WriteUnit) -> PreparedOperation:
            return self._prepare_spawn_operation(
                operation_id,
                unit,
                client_id=client_id,
                params=params,
                captured=captured,
            )

        acceptance = self.operations.accept_operation(
            client_id=client_id,
            idempotency_key=idempotency_key,
            method="frontend.participants.spawn",
            params=params,
            prepare=prepare,
        )
        if acceptance.replayed:
            return acceptance.response
        participant = captured["participant"]
        assert isinstance(participant, Participant)
        self._start_spawn(acceptance, captured, params, participant)
        return acceptance.response

    def _prepare_spawn_operation(
        self,
        operation_id: str,
        unit: WriteUnit,
        *,
        client_id: str,
        params: Mapping[str, object],
        captured: dict[str, object],
    ) -> PreparedOperation:
        admission = self._validate_spawn_admission(params, unit)
        workspace = self._reserve_existing_workspace(
            admission.workspace_request,
            reservation_id=operation_id,
            connection=unit.connection,
        )
        cwd = workspace.workspace.path if workspace is not None else admission.cwd
        request = (
            replace(admission.request, cwd=cwd, worktree=False)
            if workspace is not None
            else admission.request
        )
        description = self._optional_text(params.get("description"))
        if description is None and admission.resume_predecessor is not None:
            description = admission.resume_predecessor.description
        participant_id = new_id()
        participant = self.registry.create_spawned(
            pid=participant_id,
            harness=request.harness,
            cwd=cwd,
            parent_id=admission.parent_id,
            has_prompt=True,
            resumed_from_id=(
                admission.resume_predecessor.id
                if admission.resume_predecessor is not None
                else None
            ),
            name=self._optional_text(params.get("name")),
            description=description,
            workspace_id=workspace.workspace.workspace_id if workspace is not None else None,
            connection=unit.connection,
        )
        timestamp = now()
        job = self._reserve_spawn_job(
            unit,
            participant_id=participant_id,
            parent_id=admission.parent_id,
            client_id=client_id,
            prompt=request.prompt,
            timestamp=timestamp,
        )
        launch = LaunchReservationRecord(
            operation_id=operation_id,
            participant_id=participant_id,
            provider_id=admission.provider_id,
            workspace_usage_id=workspace.usage.usage_id if workspace is not None else None,
            adapter=request.harness,
            phase="reserved",
            launch_facts={
                "provider_generation": admission.provider_generation,
                "provider_selector": admission.provider_selector,
                "workspace_request": self._workspace_request_facts(admission.workspace_request),
                "cwd": cwd,
                "approval": request.approval,
                "resume": request.resume,
            },
            artifact_refs=(),
            created_at=timestamp,
            updated_at=timestamp,
        )
        self.store.operations.reserve_launch(launch, connection=unit.connection)
        captured.update(
            participant=participant,
            provider_id=admission.provider_id,
            provider_generation=admission.provider_generation,
            workspace_request=admission.workspace_request,
            workspace_reservation=workspace,
            request=request,
            resume_predecessor=admission.resume_predecessor,
            resume_overlay=admission.resume_overlay,
        )
        self._stage_spawn_caches(unit, participant, cwd)
        events = self._spawn_acceptance_events(unit, participant, job, workspace, timestamp)
        return PreparedOperation(
            record=PublicOperationRecord(
                operation_id=operation_id,
                kind="spawn",
                actor_client_id=client_id,
                actor_participant_id=admission.parent_id,
                target_ids=(participant_id,),
                state="accepted",
                phase="launch_reserved",
                job_handle=participant_id,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            response={
                "operation_id": operation_id,
                "state": "accepted",
                "participant_id": participant_id,
                "job_handle": participant_id,
            },
            events=events,
        )

    def _validate_spawn_admission(
        self, params: Mapping[str, object], unit: WriteUnit
    ) -> _SpawnAdmission:
        provider_id, generation = self._select_provider(params.get("provider"), unit)
        provider = self.store.providers.get(provider_id, connection=unit.connection)
        assert provider is not None
        parent_id = self._optional_text(params.get("initiating_participant_id"))
        if (
            parent_id is not None
            and self._participant_in_connection(parent_id, unit.connection) is None
        ):
            raise BadRequest(f"no initiating participant {parent_id!r} exists")
        workspace_request = self._workspace_request(params)
        self._validate_workspace_request(workspace_request)
        cwd = self._workspace_cwd(workspace_request, unit)
        request = self._spawn_request(
            params,
            parent_id=parent_id,
            cwd=cwd,
            worktree=workspace_request.worktree,
            base_ref=workspace_request.base_ref,
        )
        harness = get_harness(request.harness)
        if shutil.which(harness.binary) is None:
            raise BadRequest(f"{harness.binary!r} is not on PATH")
        request = self.spawner._resolve_resume_reference(request)
        predecessor, overlay = self.spawner._validate_before_create(request, harness)
        if overlay is not None and overlay.cwd is not None:
            if workspace_request.workspace_id is not None and overlay.cwd != cwd:
                raise BadRequest("resume workspace does not match the predecessor's trusted cwd")
            cwd = overlay.cwd
            request = replace(request, cwd=cwd)
            if workspace_request.workspace_id is None:
                workspace_request = replace(workspace_request, cwd=cwd)
        return _SpawnAdmission(
            provider_id=provider_id,
            provider_generation=generation,
            provider_selector=provider.selector,
            parent_id=parent_id,
            workspace_request=workspace_request,
            request=request,
            resume_predecessor=predecessor,
            resume_overlay=overlay,
            cwd=cwd,
        )

    def _workspace_cwd(self, request: WorkspaceRequest, unit: WriteUnit) -> str:
        if request.workspace_id is None:
            assert request.cwd is not None
            return request.cwd
        workspace = self.store.workspaces.get(request.workspace_id, connection=unit.connection)
        if workspace is None:
            raise BadRequest(f"no workspace {request.workspace_id!r} exists")
        if workspace.state != WorkspaceState.ACTIVE.value:
            raise BadRequest(f"workspace {workspace.workspace_id!r} is not active")
        return workspace.path

    def _reserve_spawn_job(
        self,
        unit: WriteUnit,
        *,
        participant_id: str,
        parent_id: str | None,
        client_id: str,
        prompt: str,
        timestamp: float,
    ) -> Job:
        job = Job(
            handle=participant_id,
            caller_id=parent_id,
            target_id=participant_id,
            kind="spawn",
            prompt=prompt,
            state=JobState.RUNNING.value,
            result=None,
            error_code=None,
            created_at=timestamp,
            finished_at=None,
            actor_client_id=client_id,
            actor_participant_id=parent_id,
        )
        unit.connection.execute(insert(jobs_table).values(**job.to_dict()))
        return job

    def _stage_spawn_caches(self, unit: WriteUnit, participant: Participant, cwd: str) -> None:
        unit.after_commit(
            lambda: self.daemon.jobs.attach_touch_accumulator(participant.id, cwd=cwd)
        )
        if participant.name is not None:
            unit.after_commit(
                lambda: self.registry.remember_reserved_name(participant.id, participant.name or "")
            )

    def _spawn_acceptance_events(
        self,
        unit: WriteUnit,
        participant: Participant,
        job: Job,
        workspace: WorkspaceReservation | None,
        timestamp: float,
    ) -> tuple[JournalEventRecord, ...]:
        first = self.store.journal.current_sequence(connection=unit.connection) + 1
        events = [
            self._participant_event(participant, timestamp, revision=first),
            self._job_event(job, timestamp, revision=first + 1),
        ]
        if workspace is not None:
            events.append(
                self._workspace_usage_event(
                    workspace.usage,
                    timestamp,
                    revision=first + 2,
                    action="acquired",
                )
            )
        return tuple(events)

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
                if workspace is None:
                    workspace = await self.workspaces.reserve(
                        captured["workspace_request"],
                        reservation_id=acceptance.record.operation_id,
                    )
                    captured["workspace_reservation"] = workspace
                    self._link_workspace(acceptance.record.operation_id, participant, workspace)
                assert isinstance(workspace, WorkspaceReservation)
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
                attached = await self.spawner.launch(reservation)
                return OperationOutcome.succeeded(
                    phase="terminal_bound",
                    result={"participant_id": attached.id, "job_handle": attached.id},
                )
            except ProviderLaunchOutcome as exc:
                return exc.outcome
            except (BadRequest, ProviderUnavailable, TerminalIdentityMismatch) as exc:
                return OperationOutcome.failed(
                    phase="launch_refused",
                    error={"code": exc.code, "message": str(exc)},
                )

        self.operations.start(
            acceptance.record.operation_id,
            dispatch=DispatchIntent(phase="launch_preparing"),
            side_effect=side_effect,
        )

    def adopt(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        params: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Accept explicit adoption; the detached task always performs a fresh inspect."""
        captured: dict[str, object] = {}

        def prepare(operation_id: str, unit: WriteUnit) -> PreparedOperation:
            provider_id, generation = self._select_provider(
                params["provider_id"], unit, allow_selector=False
            )
            actor_participant_id = self._optional_text(params.get("initiating_participant_id"))
            if (
                actor_participant_id is not None
                and self._participant_in_connection(actor_participant_id, unit.connection) is None
            ):
                raise BadRequest(f"no initiating participant {actor_participant_id!r} exists")
            requested = self._optional_text(params.get("participant_id"))
            if requested is None:
                participant = self.registry.create_spawned(
                    pid=new_id(),
                    harness="unknown",
                    cwd="",
                    parent_id=None,
                    has_prompt=False,
                    tier=Tier.ADOPTED,
                    origin=ParticipantOrigin.ADOPTED,
                    connection=unit.connection,
                )
            else:
                participant = self._participant_in_connection(requested, unit.connection)
                if participant is None:
                    raise BadRequest(f"no participant {requested!r} exists")
                if participant.origin not in {ParticipantOrigin.EXTERNAL, None}:
                    raise BadRequest("adoption can attach only an existing external participant")
                if (
                    self.store.terminal_bindings.get(participant.id, connection=unit.connection)
                    is not None
                ):
                    raise TerminalIdentityMismatch(
                        provider_id, str(params["terminal_id"]), "participant_already_bound"
                    )
            timestamp = now()
            first_revision = self.store.journal.current_sequence(connection=unit.connection) + 1
            captured.update(
                participant=participant,
                provider_id=provider_id,
                provider_generation=generation,
            )
            return PreparedOperation(
                record=PublicOperationRecord(
                    operation_id=operation_id,
                    kind="adopt",
                    actor_client_id=client_id,
                    actor_participant_id=actor_participant_id,
                    target_ids=(participant.id,),
                    state="accepted",
                    phase="adoption_reserved",
                    created_at=timestamp,
                    updated_at=timestamp,
                ),
                response={
                    "operation_id": operation_id,
                    "state": "accepted",
                    "participant_id": participant.id,
                    "job_handle": None,
                },
                events=(self._participant_event(participant, timestamp, revision=first_revision),),
            )

        acceptance = self.operations.accept_operation(
            client_id=client_id,
            idempotency_key=idempotency_key,
            method="frontend.participants.adopt",
            params=params,
            prepare=prepare,
        )
        if acceptance.replayed:
            return acceptance.response
        participant = captured["participant"]
        assert isinstance(participant, Participant)
        provider_generation = captured["provider_generation"]
        assert type(provider_generation) is int

        async def side_effect() -> OperationOutcome:
            try:
                inspected = await self.terminals.inspect(
                    str(captured["provider_id"]),
                    provider_generation,
                    str(params["terminal_id"]),
                    str(params["terminal_incarnation"]),
                )
                terminal = self._inspected_terminal(
                    inspected,
                    provider_id=str(captured["provider_id"]),
                    terminal_id=str(params["terminal_id"]),
                )
                self._validate_adoption(
                    participant,
                    terminal,
                    inspected,
                    provider_id=str(captured["provider_id"]),
                    provider_generation=provider_generation,
                    terminal_id=str(params["terminal_id"]),
                    terminal_incarnation=str(params["terminal_incarnation"]),
                )
                attached = self._bind_adoption(
                    acceptance.record.operation_id, participant, terminal
                )
                return OperationOutcome.succeeded(
                    phase="terminal_adopted", result={"participant_id": attached.id}
                )
            except (BadRequest, ProviderUnavailable, TerminalIdentityMismatch) as exc:
                return OperationOutcome.failed(
                    phase="adoption_refused",
                    error={"code": exc.code, "message": str(exc)},
                )

        self.operations.start(
            acceptance.record.operation_id,
            dispatch=DispatchIntent(phase="terminal_inspection_started"),
            side_effect=side_effect,
        )
        return acceptance.response

    def _select_provider(
        self, requested: object, unit: WriteUnit, *, allow_selector: bool = True
    ) -> tuple[str, int]:
        token = (
            requested
            if isinstance(requested, str) and requested
            else self.default_provider_selector
        )
        record = self.store.providers.get(token, connection=unit.connection)
        if record is None and allow_selector:
            record = self.store.providers.get_by_selector(token, connection=unit.connection)
        if record is None:
            raise ProviderUnavailable(token, "not_registered")
        if TERMINAL_PROVIDER_CAPABILITY not in record.capabilities:
            raise ProviderUnavailable(record.provider_id, "missing_terminal_create_capability")
        if self.terminals.connections.health(record.provider_id) not in {"online", "reconciling"}:
            raise ProviderUnavailable(record.provider_id, "not_launchable")
        if not self.terminals.connections.is_current(record.provider_id, record.generation):
            raise ProviderUnavailable(record.provider_id, "no_current_callback_generation")
        return record.provider_id, record.generation

    def _reserve_existing_workspace(
        self,
        request: WorkspaceRequest,
        *,
        reservation_id: str,
        connection,
    ) -> WorkspaceReservation | None:
        if request.workspace_id is None:
            return None
        workspace = self.store.workspaces.get(request.workspace_id, connection=connection)
        if workspace is None:
            raise BadRequest(f"no workspace {request.workspace_id!r} exists")
        if workspace.state != WorkspaceState.ACTIVE.value:
            raise BadRequest(f"workspace {workspace.workspace_id!r} is not active")
        existing = self.store.workspaces.get_active_usage(
            workspace.workspace_id,
            holder_kind=WorkspaceUsageHolderKind.RESERVATION.value,
            holder_id=reservation_id,
            connection=connection,
        )
        if existing is not None:
            return WorkspaceReservation(workspace, existing, False)
        usage = WorkspaceUsageRecord(
            usage_id=new_id(),
            workspace_id=workspace.workspace_id,
            holder_kind=WorkspaceUsageHolderKind.RESERVATION.value,
            holder_id=reservation_id,
            acquired_at=now(),
        )
        if not self.store.workspaces.acquire_usage(usage, connection=connection):
            raise BadRequest(f"workspace {workspace.workspace_id!r} is not active")
        return WorkspaceReservation(workspace, usage, False)

    def _link_workspace(
        self,
        operation_id: str,
        participant: Participant,
        reservation: WorkspaceReservation,
    ) -> None:
        with self.store.write_unit() as unit:
            current = self._participant_in_connection(participant.id, unit.connection)
            if current is None:
                raise RuntimeError("reserved participant disappeared")
            current.cwd = reservation.workspace.path
            current.branch = reservation.workspace.branch
            current.workspace_id = reservation.workspace.workspace_id
            self.registry.persist_in_connection(current, unit.connection)
            unit.connection.execute(
                update(launch_reservations)
                .where(launch_reservations.c.operation_id == operation_id)
                .values(
                    workspace_usage_id=reservation.usage.usage_id,
                    phase="workspace_ready",
                    updated_at=now(),
                )
            )
            revision = self.store.journal.current_sequence(connection=unit.connection) + 1
            self.store.journal.append_group(
                unit, [self._participant_event(current, now(), revision=revision)]
            )
        participant.cwd = current.cwd
        participant.branch = current.branch
        participant.workspace_id = current.workspace_id

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
                "argv": list(resolve_pane_command(plan)),
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
            operation = self._persist_dispatch_identity(operation_id, terminal, unit)
            unit.connection.execute(
                update(launch_reservations)
                .where(launch_reservations.c.operation_id == operation_id)
                .values(phase="terminal_bound", updated_at=now())
            )
            self._append_binding_events(
                unit, participant, binding, participant_usage, operation=operation
            )
        return participant

    def _bind_adoption(
        self,
        operation_id: str,
        participant: Participant,
        terminal: Mapping[str, object],
    ) -> Participant:
        with self.store.write_unit() as unit:
            current = self._participant_in_connection(participant.id, unit.connection)
            if current is None:
                raise RuntimeError("adoption participant disappeared")
            binding = self._binding(current.id, terminal)
            self._ensure_terminal_unbound(binding, unit.connection)
            self.store.terminal_bindings.bind(binding, connection=unit.connection)
            if current.origin is ParticipantOrigin.ADOPTED:
                current.tier = Tier.ADOPTED
            occupant = terminal["occupant"]
            assert isinstance(occupant, Mapping)
            harness = occupant.get("harness")
            if isinstance(harness, str) and harness:
                current.harness = harness
            cwd = occupant.get("cwd")
            if isinstance(cwd, str) and cwd:
                current.cwd = cwd
            self.registry.persist_in_connection(current, unit.connection)
            operation = self._persist_dispatch_identity(operation_id, terminal, unit)
            self._append_binding_events(unit, current, binding, None, operation=operation)
        return current

    def _persist_dispatch_identity(
        self, operation_id: str, terminal: Mapping[str, object], unit: WriteUnit
    ) -> PublicOperationRecord:
        operation = self.store.operations.get(operation_id, connection=unit.connection)
        if operation is None:
            raise RuntimeError("public operation disappeared before terminal binding")
        occupant = terminal["occupant"]
        process = terminal.get("process")
        assert isinstance(occupant, Mapping)
        assert process is None or isinstance(process, Mapping)
        generation = terminal["provider_generation"]
        if type(generation) is not int:
            raise TypeError("terminal provider generation must be an integer")
        updated = replace(
            operation,
            dispatch_provider_id=str(terminal["provider_id"]),
            dispatch_provider_generation=generation,
            dispatch_terminal_id=str(terminal["terminal_id"]),
            dispatch_terminal_incarnation=str(terminal["terminal_incarnation"]),
            dispatch_terminal_occupant_evidence=dict(occupant),
            dispatch_terminal_process_facts=None if process is None else dict(process),
            updated_at=now(),
        )
        if not self.store.operations.replace(
            updated,
            expected_state=operation.state,
            expected_updated_at=operation.updated_at,
            connection=unit.connection,
        ):
            raise RuntimeError("public operation changed during terminal binding")
        return updated

    def _ensure_terminal_unbound(self, candidate: TerminalBindingRecord, connection) -> None:
        for binding in self.store.terminal_bindings.list_for_provider(
            candidate.provider_id, connection=connection
        ):
            if binding.terminal_id != candidate.terminal_id:
                continue
            if binding.participant_id == candidate.participant_id:
                raise TerminalIdentityMismatch(
                    candidate.provider_id, candidate.terminal_id, "participant_already_bound"
                )
            reason = (
                "occupant_replaced"
                if binding.terminal_incarnation == candidate.terminal_incarnation
                and binding.occupant_evidence != candidate.occupant_evidence
                else "terminal_id_reused"
            )
            raise TerminalIdentityMismatch(candidate.provider_id, candidate.terminal_id, reason)

    def _append_binding_events(
        self,
        unit: WriteUnit,
        participant: Participant,
        binding: TerminalBindingRecord,
        usage: WorkspaceUsageRecord | None,
        *,
        operation: PublicOperationRecord,
    ) -> None:
        first = self.store.journal.current_sequence(connection=unit.connection) + 1
        timestamp = now()
        events = [
            self._participant_event(participant, timestamp, revision=first),
            JournalEventRecord(
                kind="terminal.binding_changed",
                entity_id=participant.id,
                entity_revision=first + 1,
                payload=self.terminals.bindings.project(binding),
                recorded_at=timestamp,
            ),
        ]
        if usage is not None:
            events.append(
                self._workspace_usage_event(
                    usage,
                    timestamp,
                    revision=first + 2,
                    action="handed_off",
                )
            )
        events.append(
            JournalEventRecord(
                kind="operation.updated",
                entity_id=operation.operation_id,
                entity_revision=first + len(events),
                payload=operation_event_payload(operation),
                recorded_at=timestamp,
            )
        )
        self.store.journal.append_group(unit, events)

    def _validate_adoption(
        self,
        participant: Participant,
        terminal: Mapping[str, object],
        inspected: Mapping[str, object],
        *,
        provider_id: str,
        provider_generation: int,
        terminal_id: str,
        terminal_incarnation: str,
    ) -> None:
        identity = (
            terminal.get("provider_id"),
            terminal.get("provider_generation"),
            terminal.get("terminal_id"),
            terminal.get("terminal_incarnation"),
        )
        if identity != (provider_id, provider_generation, terminal_id, terminal_incarnation):
            raise TerminalIdentityMismatch(provider_id, terminal_id, "inspection_identity")
        occupant = terminal.get("occupant")
        if not isinstance(occupant, Mapping):
            raise TerminalIdentityMismatch(
                str(terminal["provider_id"]), str(terminal["terminal_id"]), "occupant"
            )
        harness = occupant.get("harness")
        if not isinstance(harness, str) or not harness:
            raise TerminalIdentityMismatch(
                str(terminal["provider_id"]), str(terminal["terminal_id"]), "harness"
            )
        if participant.harness not in {"unknown", harness}:
            raise TerminalIdentityMismatch(
                str(terminal["provider_id"]), str(terminal["terminal_id"]), "harness"
            )
        process = terminal.get("process")
        if participant.pid is not None and (
            not isinstance(process, Mapping) or process.get("pid") != participant.pid
        ):
            raise TerminalIdentityMismatch(
                str(terminal["provider_id"]), str(terminal["terminal_id"]), "process"
            )
        if participant.session_id and is_trusted_provenance(participant.session_correlation):
            lifecycle = inspected.get("lifecycle")
            if not isinstance(lifecycle, Mapping):
                raise TerminalIdentityMismatch(
                    str(terminal["provider_id"]),
                    str(terminal["terminal_id"]),
                    "trusted_identity_missing",
                )
            reported_session = lifecycle.get("native_session_id") or lifecycle.get("session_id")
            if reported_session != participant.session_id:
                raise TerminalIdentityMismatch(
                    str(terminal["provider_id"]),
                    str(terminal["terminal_id"]),
                    "trusted_identity",
                )

    @staticmethod
    def _inspected_terminal(
        inspected: Mapping[str, object], *, provider_id: str, terminal_id: str
    ) -> Mapping[str, object]:
        terminal = inspected.get("terminal")
        if not isinstance(terminal, Mapping):
            raise TerminalIdentityMismatch(provider_id, terminal_id, "inspection_missing_identity")
        return terminal

    @staticmethod
    def _binding(participant_id: str, terminal: Mapping[str, object]) -> TerminalBindingRecord:
        occupant = terminal["occupant"]
        process = terminal.get("process")
        assert isinstance(occupant, Mapping)
        assert process is None or isinstance(process, Mapping)
        generation = terminal["provider_generation"]
        if type(generation) is not int:
            raise TypeError("terminal provider generation must be an integer")
        timestamp = now()
        return TerminalBindingRecord(
            participant_id=participant_id,
            provider_id=str(terminal["provider_id"]),
            provider_generation=generation,
            terminal_id=str(terminal["terminal_id"]),
            terminal_incarnation=str(terminal["terminal_incarnation"]),
            occupant_evidence=dict(occupant),
            process_facts=None if process is None else dict(process),
            health="healthy",
            report_revision=0,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @staticmethod
    def _participant_in_connection(participant_id: str, connection) -> Participant | None:
        row = connection.execute(
            select(participants).where(participants.c.id == participant_id)
        ).first()
        return Participant.from_row(row._mapping) if row is not None else None

    @staticmethod
    def _workspace_request(params: Mapping[str, object]) -> WorkspaceRequest:
        raw = params.get("workspace")
        values = raw if isinstance(raw, Mapping) else {}
        workspace_id = values.get("workspace_id")
        cwd = values.get("cwd", params.get("cwd"))
        worktree = values.get("worktree", False)
        base_ref = values.get("base_ref")
        if workspace_id is not None and not isinstance(workspace_id, str):
            raise BadRequest("workspace_id must be a string")
        if cwd is not None and not isinstance(cwd, str):
            raise BadRequest("workspace cwd must be a string")
        if not isinstance(worktree, (bool, str)):
            raise BadRequest("workspace worktree must be a boolean or name")
        if base_ref is not None and not isinstance(base_ref, str):
            raise BadRequest("workspace base_ref must be a string")
        return WorkspaceRequest(
            workspace_id=workspace_id,
            cwd=cwd,
            worktree=worktree,
            base_ref=base_ref,
        )

    @staticmethod
    def _workspace_request_facts(request: WorkspaceRequest) -> dict[str, object]:
        return {
            "workspace_id": request.workspace_id,
            "cwd": request.cwd,
            "worktree": request.worktree,
            "base_ref": request.base_ref,
        }

    @staticmethod
    def _spawn_request(
        params: Mapping[str, object],
        *,
        parent_id: str | None,
        cwd: str,
        worktree: bool | str,
        base_ref: str | None,
    ) -> SpawnRequest:
        return SpawnRequest(
            harness=str(params["harness"]),
            prompt=str(params["prompt"]),
            cwd=cwd,
            approval=str(params["approval"]),
            parent_id=parent_id,
            worktree=worktree,
            base_branch=base_ref,
            model=ParticipantLaunchService._optional_text(params.get("model")),
            reasoning_effort=ParticipantLaunchService._optional_text(
                params.get("reasoning_effort")
            ),
            resume=ParticipantLaunchService._optional_text(params.get("resume")),
            name=ParticipantLaunchService._optional_text(params.get("name")),
            description=ParticipantLaunchService._optional_text(params.get("description")),
        )

    @staticmethod
    def _validate_workspace_request(request: WorkspaceRequest) -> None:
        if request.workspace_id is not None:
            if (
                request.cwd is not None
                or request.worktree is not False
                or request.base_ref is not None
            ):
                raise BadRequest("workspace_id cannot be combined with cwd, worktree, or base_ref")
            return
        if request.cwd is None:
            raise BadRequest("workspace preparation requires workspace_id or cwd")
        if not isinstance(request.worktree, (bool, str)) or request.worktree == "":
            raise BadRequest("worktree must be false, true, or a non-empty name")
        if request.base_ref is not None and request.worktree is False:
            raise BadRequest("base_ref requires unique or named worktree creation")

    @staticmethod
    def _optional_text(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _participant_event(
        participant: Participant, timestamp: float, *, revision: int
    ) -> JournalEventRecord:
        return JournalEventRecord(
            kind="participant.updated",
            entity_id=participant.id,
            entity_revision=revision,
            payload={
                "participant_id": participant.id,
                "origin": str(participant.origin or ParticipantOrigin(str(participant.tier))),
                "harness": participant.harness,
                "status": str(participant.status),
                "parent_id": participant.parent_id,
                "cwd": participant.cwd,
                "workspace_id": participant.workspace_id,
            },
            recorded_at=timestamp,
        )

    @staticmethod
    def _job_event(job: Job, timestamp: float, *, revision: int) -> JournalEventRecord:
        return JournalEventRecord(
            kind="job.updated",
            entity_id=job.handle,
            entity_revision=revision,
            payload={
                "handle": job.handle,
                "state": str(job.state),
                "kind": str(job.kind),
                "target_id": job.target_id,
                "actor": {
                    "client_id": job.actor_client_id,
                    "participant_id": job.actor_participant_id,
                },
            },
            recorded_at=timestamp,
        )

    @staticmethod
    def _workspace_usage_event(
        usage: WorkspaceUsageRecord,
        timestamp: float,
        *,
        revision: int,
        action: str,
    ) -> JournalEventRecord:
        return JournalEventRecord(
            kind="workspace.usage_changed",
            entity_id=usage.workspace_id,
            entity_revision=revision,
            payload={
                "workspace_id": usage.workspace_id,
                "usage_id": usage.usage_id,
                "holder_kind": usage.holder_kind,
                "holder_id": usage.holder_id,
                "acquired_at": usage.acquired_at,
                "action": action,
            },
            recorded_at=timestamp,
        )
