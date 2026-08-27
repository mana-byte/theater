"""Spawner orchestration: reserve, launch, cleanup, retire, kill, teardown.

The spawn is split into ``reserve`` and ``launch`` so the daemon can
create the spawn **job** between them — before the pane exists.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Sequence

from theater import paths, timing
from theater.constants.daemon import BUS_KIND_PARTICIPANT_SESSION_BOUNDARY
from theater.constants.harness import (
    SPAWN_KILL_POLL_ATTEMPTS,
    SPAWN_KILL_POLL_INTERVAL_SECONDS,
)
from theater.constants.tmux import TMUX_DEFAULT_SESSION
from theater.daemon import workers
from theater.daemon import worktrees as worktree_mod
from theater.daemon.registry import Registry
from theater.daemon.spawning.models import Reservation, SpawnRequest
from theater.daemon.spawning.planning import (
    build_plan,
    install_hook_plan,
    record_launch_identity,
    validate_receipt_plan,
    write_plan_files,
)
from theater.daemon.spawning.resume import (
    capture_resume_floor,
    reject_unsafe_resume_shape,
    resolve_resume_reference,
    validate_resume_identity,
)
from theater.harness import get as get_harness
from theater.harness.base import LaunchPlan, ResumeLaunchOverlay
from theater.models import BadRequest, Participant, TheaterError
from theater.observability.catalog import KILL_PANE, KILL_TEARDOWN, SPAWN_LAUNCH, SPAWN_WORKTREE
from theater.tmux import client as tmux

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
    #: Poll attempts when confirming a pane is gone after kill-pane.
    KILL_POLL_ATTEMPTS = SPAWN_KILL_POLL_ATTEMPTS

    #: Interval between kill-pane confirmation polls, in seconds.
    KILL_POLL_INTERVAL = SPAWN_KILL_POLL_INTERVAL_SECONDS

    def __init__(self, registry: Registry):
        self.registry = registry
        self._named_locks: dict[str, asyncio.Lock] = {}

    def _named_lock(self, repo_root: str) -> asyncio.Lock:
        return self._named_locks.setdefault(repo_root, asyncio.Lock())

    async def reserve(self, req: SpawnRequest) -> Reservation:
        """Create the participant, worktree, plan, and config files.

        On failure the participant is marked DEAD and any worktree retired.
        """
        from dataclasses import replace

        harness = get_harness(req.harness)
        if shutil.which(harness.binary) is None:
            raise BadRequest(f"{harness.binary!r} is not on PATH")
        req = self._resolve_resume_reference(req)
        resume_predecessor, resume_overlay = self._validate_before_create(req, harness)
        participant = self.registry.create_spawned(
            harness=req.harness,
            cwd=req.cwd,
            parent_id=req.parent_id,
            has_prompt=bool(req.prompt),
        )

        try:
            plan = self._build_plan(req, participant, resume_overlay)
            minted_token = self._validate_receipt_plan(plan, participant)
            if minted_token is not None:
                plan = replace(plan, receipt_token=minted_token)
            plan = self._install_hook_plan(plan, participant, harness.observer)
            with timing.span(SPAWN_WORKTREE, id=participant.id, kind=req.worktree or None):
                child_cwd = await self._prepare_worktree(req, participant)
            self._record_launch_identity(participant, plan)

            if resume_predecessor is not None:
                participant.resume_floor = self._capture_resume_floor(harness, resume_predecessor)
                self.registry.store.upsert_participant(participant)

            paths.ensure_home()
            self._write_plan_files(plan)

            session = await self._resolve_session(req.tmux_session, child_cwd)
            name = req.window_name or f"{req.harness}-{participant.id[:6]}"
        except BaseException:
            await self.cleanup_reservation(participant)
            raise

        return Reservation(
            participant=participant,
            plan=plan,
            child_cwd=child_cwd,
            session=session,
            name=name,
            req=req,
            resume_predecessor=resume_predecessor,
        )

    async def launch(self, reservation: Reservation) -> Participant:
        """Create the tmux window and attach the pane.

        On failure the participant is marked DEAD and the worktree retired.
        """
        participant = reservation.participant
        try:
            with timing.span(SPAWN_LAUNCH, id=participant.id, harness=participant.harness):
                pane = await tmux.new_window(
                    session=reservation.session,
                    name=reservation.name,
                    cwd=reservation.child_cwd,
                    command=reservation.plan.argv,
                    env={**reservation.plan.env, "THEATER_ID": participant.id},
                    background=reservation.req.background,
                )
        except BaseException:
            await self.cleanup_reservation(participant)
            raise

        try:
            info = await tmux.pane_info(pane)
        except Exception:
            info = None
        try:
            attached = self.registry.attach_pane(
                participant.id, pane, pane_pid=info.pane_pid if info else None
            )
        except BaseException:
            await self.cleanup_reservation(participant)
            raise
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
        self, req: SpawnRequest, participant: Participant, overlay: ResumeLaunchOverlay | None
    ) -> LaunchPlan:
        """Launch plan construction via the planning module."""
        return build_plan(req, participant, overlay)

    @staticmethod
    def _install_hook_plan(plan: LaunchPlan, participant: Participant, observer) -> LaunchPlan:
        """Apply generic launch-local hook installation."""
        return install_hook_plan(plan, participant, observer)

    @staticmethod
    def _write_plan_files(plan: LaunchPlan) -> None:
        """Plan file writing via the planning module."""
        write_plan_files(plan)

    def _record_launch_identity(self, participant: Participant, plan: LaunchPlan) -> None:
        """Identity recording via the planning module."""
        record_launch_identity(participant, plan, self.registry)

    def _resolve_resume_reference(self, req: SpawnRequest) -> SpawnRequest:
        """Resume reference resolution via the resume module."""
        return resolve_resume_reference(req, self.registry)

    def _validate_before_create(
        self, req: SpawnRequest, harness
    ) -> tuple[Participant | None, ResumeLaunchOverlay | None]:
        """Refuse unsafe launches before a participant or worktree exists."""
        from theater.harness import check_model, check_reasoning, check_resume

        check_model(req.harness, req.model)
        check_reasoning(req.harness, req.reasoning_effort)
        check_resume(req.harness, req.resume)
        self._reject_unsafe_resume_shape(req, harness)
        predecessor, trusted_owners = self._validate_resume_identity(req)
        overlay: ResumeLaunchOverlay | None = None
        if predecessor is not None:
            overlay = harness.resume_launch_overlay(
                predecessor=predecessor,
                trusted_session_owners=trusted_owners,
            )
        return predecessor, overlay

    @staticmethod
    def _reject_unsafe_resume_shape(req: SpawnRequest, harness) -> None:
        """Refuse resume combinations that are unsafe or silently dropped."""
        reject_unsafe_resume_shape(req, harness)

    def _validate_resume_identity(
        self, req: SpawnRequest
    ) -> tuple[Participant | None, Sequence[Participant]]:
        """Resume identity validation via the resume module."""
        return validate_resume_identity(req, self.registry)

    @staticmethod
    def _capture_resume_floor(harness, predecessor: Participant) -> str:
        """Resume floor capture via the resume module."""
        return capture_resume_floor(harness, predecessor)

    async def cleanup_reservation(self, participant: Participant) -> None:
        """Idempotent cleanup: retire the worktree and mark the participant DEAD."""
        try:
            await self.retire(participant, delete_branch=True)
        except BaseException:
            logger.warning(
                "retire raised for %s; proceeding to mark_dead",
                participant.id,
                exc_info=True,
            )
        self.registry.mark_dead(participant.id)

    async def _resolve_session(self, requested: str | None, cwd: str) -> str:
        """Adopt the caller's session when there is one; never nest a server."""
        if requested:
            existing = await tmux.sessions()
            if requested in existing:
                return requested
        return await tmux.ensure_session(TMUX_DEFAULT_SESSION, cwd=cwd)

    async def _spawn_named_worktree(
        self, *, root: str, name: str, base_branch: str | None
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
                return existing["path"], existing["branch"]

            path, branch = await _uncancellable(
                workers.to_thread,
                worktree_mod.create_named_worktree,
                label="spawn.named.create",
                repo_root=canonical_root,
                name=name,
                base_branch=base_branch,
                reconcile=lambda r: store.upsert_named_worktree(
                    repo_root=canonical_root,
                    name=name,
                    branch=r[1],
                    path=r[0],
                    base_branch=base_branch,
                ),
            )
            store.upsert_named_worktree(
                repo_root=canonical_root,
                name=name,
                branch=branch,
                path=path,
                base_branch=base_branch,
            )
            return path, branch

    async def kill_pane(self, participant_id: str) -> Participant:
        """Kill the tmux pane and confirm it is gone."""
        p = self.registry.get(participant_id)
        if p.tmux_pane:
            with timing.span(KILL_PANE, id=p.id, pane=p.tmux_pane, harness=p.harness) as sp:
                await tmux.kill_pane(p.tmux_pane)
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
            await self.retire(p, delete_branch=True)
            self.registry.mark_dead(p.id)

    async def retire(self, p: Participant, *, delete_branch: bool) -> None:
        """Reclaim the git worktree of a participant that is going away.

        ``delete_branch``: True for kills (branch discarded), False for
        self-exits (branch preserved). Named worktrees always retain the
        branch; only the directory is removed when the last live
        participant leaves. Failure is logged, never raised.
        """
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
                    delete_branch=False,
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
