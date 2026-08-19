"""Bring a new participant into existence.

Sequence, and why it is this order:

  1. mint the participant id      the MCP argv needs it, so it must exist first
  2. create the worktree (if requested)  the child runs in the worktree, not the parent's repo
  3. write the harness config     the pane will read it at startup
  4. tmux new-window -d           returns the pane id; identity is now settled
  5. record the pane              nothing was inferred at any point

The spawn is split into ``reserve`` (steps 1-3) and ``launch`` (steps 4-5)
so the daemon can create the spawn **job** between them — before the pane
exists. This closes the ordering race where a child that finishes in the
gap between pane creation and job creation had its turn end observed with
no RUNNING job to receive the result.

If ``launch`` fails the participant is marked dead immediately rather than
left as a ghost that the régie would draw forever.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, replace

from theater import paths, timing
from theater.daemon import workers
from theater.daemon import worktree as worktree_mod
from theater.daemon.registry import Registry
from theater.harness import check_model, check_reasoning, check_resume, plan_launch
from theater.harness import get as get_harness
from theater.harness import normalize as normalize_harness
from theater.harness.base import LaunchPlan, ResumeLaunchOverlay
from theater.harness.observation import HarnessObserver
from theater.models import BadRequest, Participant, Status, TheaterError
from theater.provenance import TranscriptProvenance, is_trusted_provenance
from theater.resume_floor import UNKNOWN_FLOOR, encode_floor
from theater.tmux import client as tmux

logger = logging.getLogger("theater.spawner")

FALLBACK_SESSION = "theater"


async def _uncancellable(fn, /, *args, reconcile=None, **kwargs):
    """Await ``fn(*args, **kwargs)`` so that cancellation does not release
    the caller's lock until the worker finishes and state is reconciled.

    Uses ``asyncio.shield`` to protect the inner task. On
    ``CancelledError``, awaits the task to completion (lock stays held).
    If *reconcile* is provided and the task succeeded, it is called with
    the result so the caller can commit state. The original
    ``CancelledError`` is then re-raised — no re-cancellation needed."""
    task = asyncio.create_task(fn(*args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        result = await task
        if reconcile is not None:
            reconcile(result)
        raise


@dataclass(frozen=True, slots=True)
class SpawnRequest:
    harness: str
    prompt: str
    cwd: str
    approval: str
    parent_id: str | None = None
    tmux_session: str | None = None
    window_name: str | None = None
    background: bool = True
    #: If True, create a git worktree for the child and run it there.
    #: If a non-empty string, create or join a named shared linked worktree.
    #: None or False means run in the requested cwd without a worktree.
    worktree: str | bool | None = False
    #: Base branch for the worktree. Defaults to current HEAD.
    base_branch: str | None = None
    #: Model for the child, in whatever spelling its harness expects. Opaque
    #: here and never validated: see `harness.plan_launch`. None means the
    #: harness picks, which is what every spawn did before this existed.
    model: str | None = None
    #: Reasoning effort for the child (e.g. "low", "medium", "high"). Opaque
    #: here and validated only by `harness.check_reasoning`, which refuses a
    #: harness whose `plan_launch` has no `reasoning_effort` parameter. None
    #: means the harness picks its default.
    reasoning_effort: str | None = None
    #: Session id to resume, when the harness supports it. Opaque here and
    #: validated only by `harness.check_resume`, which refuses a harness
    #: whose `plan_launch` has no `resume` parameter. None means start cold.
    resume: str | None = None
    #: Raw serialized JSON response-format hint. Prompt construction lives in
    #: daemon methods; the spawner only enforces launch-time capability traps.
    response_format: str | None = None


@dataclass(slots=True)
class Reservation:
    """Everything ``reserve`` produced that ``launch`` needs.

    Carries the participant row, the launch plan, the resolved child cwd,
    the resolved tmux session name, the window name, and the original
    request — enough to create the tmux window without re-deriving anything.
    The daemon creates its spawn job between ``reserve`` and ``launch`` so
    the job is RUNNING before the pane can produce output.
    """

    participant: Participant
    plan: LaunchPlan
    child_cwd: str
    session: str
    name: str
    req: SpawnRequest


class Spawner:
    def __init__(self, registry: Registry):
        self.registry = registry
        self._named_locks: dict[str, asyncio.Lock] = {}

    def _named_lock(self, repo_root: str) -> asyncio.Lock:
        return self._named_locks.setdefault(repo_root, asyncio.Lock())

    async def reserve(self, req: SpawnRequest) -> Reservation:
        """Create the participant, worktree, plan, and config files. On
        failure the participant is marked DEAD and any worktree retired."""
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
            with timing.span("spawn.worktree", id=participant.id, kind=req.worktree or None):
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
        )

    async def launch(self, reservation: Reservation) -> Participant:
        """Create the tmux window and attach the pane. On failure the
        participant is marked DEAD and the worktree retired."""
        participant = reservation.participant
        try:
            with timing.span("spawn.launch", id=participant.id, harness=participant.harness):
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
            return self.registry.attach_pane(
                participant.id, pane, pane_pid=info.pane_pid if info else None
            )
        except BaseException:
            await self.cleanup_reservation(participant)
            raise

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

    def _build_plan(
        self,
        req: SpawnRequest,
        participant: Participant,
        overlay: ResumeLaunchOverlay | None,
    ) -> LaunchPlan:
        """Construct the launch plan and merge the resume overlay, if any."""
        config_path = paths.mcp_config_path(participant.id)
        plan = plan_launch(
            req.harness,
            participant_id=participant.id,
            prompt=req.prompt,
            config_path=config_path,
            approval=req.approval,
            model=req.model,
            reasoning_effort=req.reasoning_effort,
            resume=req.resume,
        )
        if overlay is None:
            return plan
        env = {**plan.env, **overlay.env}
        transcript_domain = plan.transcript_domain
        if overlay.transcript_domain is not None:
            transcript_domain = overlay.transcript_domain
        return replace(plan, env=env, transcript_domain=transcript_domain)

    def _validate_receipt_plan(self, plan: LaunchPlan, participant: Participant) -> str | None:
        """Pre-flight: validate a receipt plan and mint the token.

        Returns the minted token string, or ``None`` when the plan does not
        use receipts (``receipt_token_path is None``). Core owns the secret:
        the plugin sets only ``receipt_token_path``, and core mints the token
        here. A plugin that sets ``receipt_token`` itself is refused, because
        a third-party plugin could ship a constant or weak token and core
        would faithfully write and trust it.

        Runs after ``_build_plan`` returns and before ``_write_plan_files``.
        The participant row has been created but no worktree or launch-plan
        file exists yet — plan construction and receipt pre-flight run before
        ``_prepare_worktree``, so a rejected plan leaves nothing behind.
        """
        if plan.receipt_token_path is None:
            return None
        if plan.receipt_token is not None:
            raise BadRequest(
                "launch plan sets receipt_token; core owns the receipt secret "
                "and mints it from receipt_token_path. The plugin should set "
                "only receipt_token_path, not receipt_token."
            )
        # The observer must actually implement the hook. A plan declaring
        # receipt_token_path against an inheriting default would write a token
        # file that can never be used — core would refuse the receipt at
        # runtime because the base raises ValueError.
        # Resolve through the global registry, not daemon.observer.harnesses:
        # _build_plan calls the global plan_launch, so the plan was produced
        # by this same registry's harness — checking a different one would be
        # incoherent. The receipt RPC later resolves through the daemon's
        # harnesses because it handles a live participant the daemon owns; a
        # daemon constructed with injected harnesses could not have spawned
        # through the global registry anyway.
        harness = get_harness(participant.harness)
        observer = getattr(harness, "observer", None)
        if observer is None:
            raise BadRequest(
                f"harness {participant.harness!r} has no observer; cannot use transcript receipts"
            )
        if (
            type(observer).validate_transcript_receipt
            is HarnessObserver.validate_transcript_receipt
        ):
            raise BadRequest(
                f"harness {participant.harness!r} observer does not implement "
                "validate_transcript_receipt; a plugin must implement this hook "
                "to use transcript receipts. See docs/harness-plugins.md"
            )
        # Refuse an existing symlink first: the writer uses O_TRUNC which
        # follows symlinks, so a symlink at the token path could write the
        # token to an attacker-chosen location. Check before resolve() so a
        # symlink pointing outside the observation dir is caught as a
        # symlink, not as an out-of-dir path.
        if plan.receipt_token_path.is_symlink():
            raise BadRequest(
                f"receipt_token_path {plan.receipt_token_path!r} is a symlink; "
                "refusing to write a token through a symlink"
            )
        # The receipt token path must live under the harness's observation dir.
        obs_dir = paths.observation_dir(participant.harness, participant.id)
        try:
            resolved_token = plan.receipt_token_path.resolve(strict=False)
            resolved_obs = obs_dir.resolve(strict=False)
            resolved_token.relative_to(resolved_obs)
        except ValueError:
            raise BadRequest(
                f"receipt_token_path {plan.receipt_token_path!r} must resolve "
                f"under the harness observation directory {obs_dir!r}"
            ) from None
        # No collision with public or private plan files: _write_plan_files
        # writes public then private, so an identical private path silently
        # overwrites the public one.
        all_plan_paths = set(plan.files) | set(plan.private_files)
        for existing in all_plan_paths:
            if existing.resolve(strict=False) == resolved_token:
                raise BadRequest(
                    f"receipt_token_path {plan.receipt_token_path!r} collides "
                    f"with a launch-plan file {existing!r}"
                )
        return secrets.token_urlsafe(32)

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

    def _record_launch_identity(self, participant: Participant, plan) -> None:
        """Persist exact launch facts before the process can write output."""
        if (
            plan.session_id is None
            and plan.transcript_domain is None
            and plan.receipt_token is None
        ):
            return
        if plan.session_id is not None:
            participant.session_id = plan.session_id
            participant.session_correlation = str(TranscriptProvenance.EXACT)
        participant.transcript_domain = plan.transcript_domain
        self.registry.store.upsert_participant(participant)
        if plan.receipt_token is not None:
            token_path = plan.receipt_token_path
            self.registry.store.set_receipt_token(
                participant.id,
                plan.receipt_token,
                token_path=str(token_path) if token_path is not None else None,
            )

    @staticmethod
    def _write_plan_files(plan) -> None:
        """Write public config files, private launch secrets, and the receipt token."""
        for path, contents in plan.files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
        for path, contents in plan.private_files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.parent.chmod(0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(contents)
        # Core owns the receipt token file: it generates the token, writes it
        # mode 0600, and deletes it on death. The plugin must NOT also put it
        # in private_files or the two writers collide.
        if plan.receipt_token_path is not None and plan.receipt_token is not None:
            token_path = plan.receipt_token_path
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.parent.chmod(0o700)
            fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(plan.receipt_token + "\n")

    def _resolve_resume_reference(self, req: SpawnRequest) -> SpawnRequest:
        """If ``resume`` is a Theater participant id, resolve it to the harness
        session id the daemon already holds.

        Participant primary-key matches take precedence: if the value is an
        exact row id, it is resolved here. Otherwise the value is treated as a
        native harness session id and the existing path handles it. This means
        an unknown participant id is indistinguishable from a native session id
        and fails the existing trusted-binding check — which is the right
        failure for a value that is neither.
        """
        if req.resume is None:
            return req
        participant = self.registry.store.get_participant(req.resume)
        if participant is None:
            return req
        if normalize_harness(participant.harness) != normalize_harness(req.harness):
            raise BadRequest(
                f"participant {participant.id!r} belongs to harness "
                f"{participant.harness!r}, not {req.harness!r}"
            )
        if participant.status is not Status.DEAD:
            raise BadRequest(f"cannot resume participant {participant.id!r}: it is still live")
        if not participant.session_id:
            raise BadRequest(
                f"cannot resume participant {participant.id!r}: "
                "Theater has not recorded its harness session id"
            )
        return replace(req, resume=participant.session_id)

    def _validate_before_create(
        self, req: SpawnRequest, harness
    ) -> tuple[Participant | None, ResumeLaunchOverlay | None]:
        """Refuse unsafe launches before a participant or worktree exists."""
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
        # A harness that accepts resume but silently drops the prompt
        # (opencode: `-s` routes to session view, `--prompt` is only read
        # on the home screen) must not be handed both. The spawner has no
        # readiness detection, so injecting the prompt after startup would
        # race the TUI and sometimes drop it. A loud refusal is better than
        # a racy injection that fails silently.
        if req.resume and req.prompt and not harness.resume_takes_prompt:
            raise BadRequest(
                f"harness {req.harness!r} cannot resume a session with a prompt; "
                f"resume it without one and use send to deliver the task"
            )
        if req.resume and req.response_format and not harness.resume_takes_prompt:
            raise BadRequest(
                f"harness {req.harness!r} cannot resume a session with response_format; "
                f"resume it without one and use send to deliver the task"
            )
        # A resumed session's transcript describes files at its original cwd;
        # a fresh worktree points it at different files, so the transcript's
        # path references would resolve to the wrong content.
        if req.resume and req.worktree:
            raise BadRequest(
                "cannot resume into a worktree: the session's transcript "
                "describes files that are not the worktree's files"
            )

    def _validate_resume_identity(
        self, req: SpawnRequest
    ) -> tuple[Participant | None, Sequence[Participant]]:
        """Resume only daemon-validated trusted session ids.

        A raw session id is just a string. Without a participant row whose
        transcript identity reached operator/proven/exact provenance, the
        daemon has no principled way to know whether resuming it would attach
        to another person's transcript. That intentionally drops compatibility
        for arbitrary externally supplied ids; recall remains the safe source
        of resume ids because it reports only recorded rows and marks
        heuristic ids as non-resumable.

        Returns ``(predecessor, trusted_owners)``: the selected newest dead
        predecessor, and the complete trusted matching set (same canonical
        harness, same session id, trusted provenance) including the
        predecessor itself. The hook needs the whole set because the Vibe
        marker commonly names the selected predecessor row.
        """
        if req.resume is None:
            return None, ()
        canonical = normalize_harness(req.harness)
        live_matches = []
        dead_matches = []
        for participant in self.registry.list(include_dead=True):
            if (
                normalize_harness(participant.harness) == canonical
                and participant.session_id == req.resume
                and is_trusted_provenance(participant.session_correlation)
            ):
                if participant.status is Status.DEAD:
                    dead_matches.append(participant)
                else:
                    live_matches.append(participant)
        if live_matches:
            live = max(live_matches, key=lambda p: (p.last_activity, p.created_at))
            raise BadRequest(
                f"cannot resume session {req.resume!r}: trusted owner {live.id} is still "
                "live. Use send to deliver work to the live participant, or wait for it "
                "to die before resuming the session."
            )
        if dead_matches:
            predecessor = max(dead_matches, key=lambda p: (p.last_activity, p.created_at))
            return predecessor, dead_matches
        raise BadRequest(
            f"cannot resume session {req.resume!r}: Theater has no trusted "
            "dead operator/proven/exact binding for that session id. If its owner row "
            "was garbage-collected, resume the session outside Theater, then adopt and "
            "bind that pane, or wait for tombstone support."
        )

    @staticmethod
    def _capture_resume_floor(harness, predecessor: Participant) -> str:
        """Capture the predecessor's transcript stream position before launch.

        Called at the last safe pre-launch moment — after identity is
        persisted, before tmux creates the pane. Returns the encoded floor
        string (structured JSON or ``"unknown"``). An unreadable or non-file
        transcript produces ``"unknown"``: present-but-unknown is still a
        floor, and the reducer suppresses completion rather than treating
        the successor as a cold spawn.
        """
        location = predecessor.transcript_location
        if location is None:
            return UNKNOWN_FLOOR
        point = harness.observer.stream_floor(location)
        return encode_floor(point)

    async def _resolve_session(self, requested: str | None, cwd: str) -> str:
        """Adopt the caller's session when there is one; never nest a server."""
        if requested:
            existing = await tmux.sessions()
            if requested in existing:
                return requested
        return await tmux.ensure_session(FALLBACK_SESSION, cwd=cwd)

    async def _spawn_named_worktree(
        self, *, root: str, name: str, base_branch: str | None
    ) -> tuple[str, str]:
        """Create or join a named shared worktree. Serialized per repo via
        ``_named_lock`` to prevent create/create and join/retire races.

        ``base_branch`` applies only on first creation; on join it must
        match the persisted value or be omitted. Returns ``(path, branch)``.
        """
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

    KILL_POLL_ATTEMPTS = 5
    KILL_POLL_INTERVAL = 0.25

    async def kill_pane(self, participant_id: str) -> Participant:
        """Kill the tmux pane and confirm it is gone."""
        p = self.registry.get(participant_id)
        if p.tmux_pane:
            with timing.span("kill.pane", id=p.id, pane=p.tmux_pane) as sp:
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
        """Terminal teardown after the pane is confirmed gone. Reclaims the
        worktree and marks the record dead."""
        with timing.span("kill.teardown", id=p.id):
            await self.retire(p, delete_branch=True)
            self.registry.mark_dead(p.id)

    async def retire(self, p: Participant, *, delete_branch: bool) -> None:
        """Reclaim the git worktree of a participant that is going away.

        ``delete_branch``: True for kills (branch discarded), False for
        self-exits (branch preserved — it holds the commits). Named worktrees
        always retain the branch; only the directory is removed when the last
        live participant leaves. Failure is logged, never raised.
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
