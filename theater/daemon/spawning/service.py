"""Spawner orchestration: reserve, prepare, launch, rollback, and teardown."""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Mapping
from dataclasses import replace

from sqlalchemy.exc import IntegrityError

from theater import paths, timing
from theater.constants.daemon import BUS_KIND_PARTICIPANT_SESSION_BOUNDARY
from theater.daemon import workers
from theater.daemon import worktrees as worktree_mod
from theater.daemon.operations import (
    OperationOutcome,
)
from theater.daemon.registry import Registry
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
    resolve_launch_command,
    validate_receipt_plan,
    write_plan_files,
)
from theater.daemon.spawning.resume import (
    capture_resume_floor,
    resolve_resume_reference,
    validate_before_create,
)
from theater.harness import get as get_harness
from theater.harness.base import LaunchPlan, ResumeLaunchOverlay
from theater.harness.contracts.channels import ChannelKind
from theater.harness.contracts.runtime import RuntimeHost, RuntimeLifecyclePhase, RuntimeWiring
from theater.models import (
    BadRequest,
    Participant,
    Status,
    TheaterError,
    now,
)
from theater.observability.catalog import (
    KILL_TEARDOWN,
    LIFECYCLE_STAGE,
    SPAWN_LAUNCH,
    SPAWN_WORKTREE,
)

logger = logging.getLogger("theater.spawner")


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
    def __init__(
        self,
        registry: Registry,
        *,
        otel_runtime=None,
        runtime_manager=None,
        runtime_io=None,
        frontend_runtime_host=None,
        controls=None,
        live_hub=None,
        workspace_service=None,
    ):
        self.registry = registry
        self.otel_runtime = otel_runtime
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

            session = ""
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
        """Create the selected provider terminal and attach its binding.

        On failure the participant is marked DEAD. Worktrees survive once launch begins.
        """
        participant = reservation.participant
        try:
            if (
                reservation.native is not None
                and reservation.native.runtime.host is RuntimeHost.DETACHED_BACKEND
            ):
                # The native sequence owns its failure ordering (backend, terminal, binding, then
                # generic cleanup only if teardown verified); once transmission may have begun,
                # it cleans nothing.
                attached = await launch_native(self, reservation)
            elif reservation.native is not None:
                attached = await self._launch_frontend(reservation)
            else:
                attached = await self._launch_terminal(reservation)
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
            raise TheaterError("the provider terminal exited during spawn")
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
                operation_id=(
                    reservation.provider.operation_id if reservation.provider is not None else None
                ),
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
            return await self._launch_terminal(fallback)
        try:
            attached = await self._launch_terminal(reservation)
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
            raise TheaterError("frontend runtime binding changed during terminal launch")
        return attached

    async def _launch_terminal(self, reservation: Reservation) -> Participant:
        if reservation.provider is None:
            raise BadRequest("terminal launch requires a selected provider")
        return await self._launch_provider_terminal(reservation, reservation.plan)

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
            attached = await self._launch_terminal(fallback)
        except ProviderLaunchOutcome:
            raise
        except BaseException:
            self._preserve_failed_launch(participant)
            raise
        if attached.status is Status.DEAD:
            self._preserve_failed_launch(participant)
            raise TheaterError("the provider fallback terminal exited during spawn")
        return attached

    async def _close_frontend_launch(self, participant_id: str) -> None:
        if self.frontend_runtime_host is not None:
            await self.frontend_runtime_host.close(participant_id)
        if self.runtime_manager is not None:
            await self.runtime_manager.close(participant_id)
        if self.live_hub is not None:
            self.live_hub.unregister(participant_id)

    async def _launch_provider_terminal(
        self, reservation: Reservation, plan: LaunchPlan
    ) -> Participant:
        """Create one terminal through the selected immutable provider generation."""
        provider = reservation.provider
        if provider is None:
            raise RuntimeError("provider launch selection is missing")
        command = resolve_launch_command(plan)
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
        with (
            timing.span(
                SPAWN_LAUNCH,
                id=reservation.participant.id,
                harness=reservation.participant.harness,
            ),
            timing.span(
                LIFECYCLE_STAGE,
                action="spawn",
                stage="provider",
                id=reservation.participant.id,
                operation_id=provider.operation_id,
            ),
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

        The binding exists before any process spawns, so a crash in between leaves recoverable
        intent. Launch policy holds approval/model facts only — never secrets.
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
        """Retire only the participant; durable workspaces require explicit cleanup."""
        participant = self.registry.store.get_participant(participant.id) or participant
        self._provisional_named_worktrees.discard(participant.id)
        self._joined_named_worktrees.discard(participant.id)
        self.registry.mark_dead(participant.id)

    def _preserve_failed_launch(self, participant: Participant) -> None:
        """Retain resources once terminal dispatch may have begun."""
        self._provisional_named_worktrees.discard(participant.id)
        self._joined_named_worktrees.discard(participant.id)
        self.registry.mark_dead(participant.id)

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

    async def teardown(self, p: Participant) -> None:
        """Retire participant state after every physical exit is verified."""
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
