"""Bring a new participant into existence.

Sequence, and why it is this order:

  1. mint the participant id      the MCP argv needs it, so it must exist first
  2. create the worktree (if requested)  the child runs in the worktree, not the parent's repo
  3. write the harness config     the pane will read it at startup
  4. tmux new-window -d           returns the pane id; identity is now settled
  5. record the pane              nothing was inferred at any point

If step 4 fails the participant is marked dead immediately rather than left as a
ghost that the régie would draw forever.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from theater import paths
from theater.daemon import worktree as worktree_mod
from theater.daemon.registry import Registry
from theater.harness import check_model, check_resume, plan_launch
from theater.harness import get as get_harness
from theater.models import BadRequest, Participant, Status, TheaterError
from theater.provenance import TranscriptProvenance, is_trusted_provenance
from theater.tmux import client as tmux

logger = logging.getLogger("theater.spawner")

#: Where windows go when the caller is not itself inside tmux.
FALLBACK_SESSION = "theater"


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
    #: Session id to resume, when the harness supports it. Opaque here and
    #: validated only by `harness.check_resume`, which refuses a harness
    #: whose `plan_launch` has no `resume` parameter. None means start cold.
    resume: str | None = None
    #: Raw serialized JSON response-format hint. Prompt construction lives in
    #: daemon methods; the spawner only enforces launch-time capability traps.
    response_format: str | None = None


class Spawner:
    def __init__(self, registry: Registry):
        self.registry = registry

    async def spawn(self, req: SpawnRequest) -> Participant:
        harness = get_harness(req.harness)
        if shutil.which(harness.binary) is None:
            raise BadRequest(f"{harness.binary!r} is not on PATH")
        resume_domain = self._validate_before_create(req, harness)
        participant = self.registry.create_spawned(
            harness=req.harness,
            cwd=req.cwd,
            parent_id=req.parent_id,
            has_prompt=bool(req.prompt),
        )

        # The child runs in the worktree, not the parent's repo — isolated
        # index and HEAD (for a True worktree) or a shared named worktree
        # (for a string worktree).
        child_cwd = req.cwd
        if req.worktree:
            root = worktree_mod.repo_root(req.cwd)
            if root is None:
                self.registry.mark_dead(participant.id)
                raise BadRequest(f"cannot create worktree: {req.cwd!r} is not in a git repo")
            try:
                if isinstance(req.worktree, str):
                    child_cwd, participant.branch = self._spawn_named_worktree(
                        root=root,
                        name=req.worktree,
                        base_branch=req.base_branch,
                    )
                else:
                    child_cwd = worktree_mod.create_worktree(
                        repo_root=root,
                        child_id=participant.id,
                        base_branch=req.base_branch,
                    )
                    participant.branch = worktree_mod.branch_name(participant.id)
            except Exception:
                self.registry.mark_dead(participant.id)
                raise
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

        config_path = paths.mcp_config_path(participant.id)
        plan = plan_launch(
            req.harness,
            participant_id=participant.id,
            prompt=req.prompt,
            config_path=config_path,
            approval=req.approval,
            model=req.model,
            resume=req.resume,
            isolate_transcript=self._has_live_cwd_sibling(participant),
        )
        if resume_domain is not None:
            plan.env["VIBE_SESSION_LOGGING__SAVE_DIR"] = str(resume_domain)
            plan = replace(plan, transcript_domain=str(resume_domain))

        self._record_launch_identity(participant, plan)

        paths.ensure_home()
        self._write_plan_files(plan)

        session = await self._resolve_session(req.tmux_session, child_cwd)
        name = req.window_name or f"{req.harness}-{participant.id[:6]}"

        try:
            pane = await tmux.new_window(
                session=session,
                name=name,
                cwd=child_cwd,
                command=plan.argv,
                env={**plan.env, "THEATER_ID": participant.id},
                background=req.background,
            )
        except Exception:
            self.registry.mark_dead(participant.id)
            raise

        # Best-effort: the window exists and the harness is starting, so
        # failing the spawn over a bookkeeping lookup would throw away a
        # working agent. A missing epoch costs one delivery check; a None
        # pane_info means the pane died in these milliseconds — caught by
        # the pane-alive check anyway.
        try:
            info = await tmux.pane_info(pane)
        except Exception:
            info = None
        return self.registry.attach_pane(
            participant.id, pane, pane_pid=info.pane_pid if info else None
        )

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
            token_path = next(iter(plan.private_files), None)
            self.registry.store.set_receipt_token(
                participant.id,
                plan.receipt_token,
                token_path=str(token_path) if token_path is not None else None,
            )

    @staticmethod
    def _write_plan_files(plan) -> None:
        """Write public config files and private launch secrets."""
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

    def _validate_before_create(self, req: SpawnRequest, harness) -> Path | None:
        """Refuse unsafe launches before a participant or worktree exists."""
        check_model(req.harness, req.model)
        check_resume(req.harness, req.resume)
        self._reject_unsafe_resume_shape(req, harness)
        resume_predecessor = self._validate_resume_identity(req)
        return self._validate_vibe_resume_domain(req, resume_predecessor)

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

    def _validate_resume_identity(self, req: SpawnRequest) -> Participant | None:
        """Resume only daemon-validated trusted session ids.

        A raw session id is just a string. Without a participant row whose
        transcript identity reached operator/proven/exact provenance, the
        daemon has no principled way to know whether resuming it would attach
        to another person's transcript. That intentionally drops compatibility
        for arbitrary externally supplied ids; recall remains the safe source
        of resume ids because it reports only recorded rows and marks
        heuristic ids as non-resumable.
        """
        if req.resume is None:
            return None
        live_matches = []
        dead_matches = []
        for participant in self.registry.list(include_dead=True):
            if (
                participant.harness == req.harness
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
            return max(dead_matches, key=lambda p: (p.last_activity, p.created_at))
        raise BadRequest(
            f"cannot resume session {req.resume!r}: Theater has no trusted "
            "dead operator/proven/exact binding for that session id"
        )

    def _validate_vibe_resume_domain(
        self, req: SpawnRequest, predecessor: Participant | None
    ) -> Path | None:
        """Return the isolated root a Vibe resume may reuse, or refuse.

        This deliberately sits behind ``_validate_resume_identity``. The Vibe
        check is about whether a trusted predecessor's transcript namespace is
        safe to reuse; it is not a second session-id validator and does not
        require exact-only provenance.
        """
        if req.resume is None or req.harness != "vibe":
            return None
        if predecessor is None or predecessor.transcript_domain is None:
            raise BadRequest(
                "cannot resume Vibe session safely: predecessor has no isolated "
                "transcript domain. Rebind or migrate the session into a Theater "
                "isolated Vibe domain, then retry."
            )
        from theater.harness.builtin.plugins.vibe import validate_isolated_domain

        domain = Path(predecessor.transcript_domain).expanduser().resolve(strict=False)
        marker = validate_isolated_domain(domain)
        if marker is None:
            raise BadRequest(
                "cannot resume Vibe session safely: predecessor uses a legacy or "
                "untrusted transcript root. Rebind or migrate it into a Theater "
                "isolated Vibe domain, then retry."
            )
        marker_owner = marker.get("participant_id")
        if not isinstance(marker_owner, str) or not self._vibe_domain_owner_matches_session(
            owner_id=marker_owner,
            session_id=req.resume,
            domain=domain,
        ):
            raise BadRequest(
                "cannot resume Vibe session safely: isolated transcript domain "
                "belongs to a different Theater session lineage. Rebind or "
                "migrate the session into its own isolated Vibe domain, then retry."
            )
        if predecessor.transcript_location is not None:
            location = Path(predecessor.transcript_location)
            try:
                location.resolve().relative_to(domain)
            except (OSError, ValueError) as exc:
                raise BadRequest(
                    "cannot resume Vibe session safely: predecessor transcript "
                    "location is outside its isolated transcript domain"
                ) from exc
        return domain

    def _vibe_domain_owner_matches_session(
        self, *, owner_id: str, session_id: str, domain: Path
    ) -> bool:
        """Whether the signed domain owner anchors this trusted resume chain."""
        for participant in self.registry.list(include_dead=True):
            if (
                participant.id == owner_id
                and participant.harness == "vibe"
                and participant.session_id == session_id
                and is_trusted_provenance(participant.session_correlation)
                and participant.transcript_domain is not None
                and Path(participant.transcript_domain).expanduser().resolve(strict=False) == domain
            ):
                return True
        return False

    def _has_live_cwd_sibling(self, participant: Participant) -> bool:
        """Whether heuristic transcript discovery would share a collision key.

        Called after worktree selection, because the child runs and writes its
        transcript from that final cwd. Spawn setup is synchronous until the
        first tmux await, so concurrent daemon requests cannot both observe
        themselves as the first same-cwd participant.
        """
        if participant.cwd is None:
            return False
        cwd = Path(participant.cwd).resolve()
        for other in self.registry.list():
            if (
                other.id != participant.id
                and other.harness == participant.harness
                and other.cwd is not None
                and Path(other.cwd).resolve() == cwd
            ):
                return True
        return False

    async def _resolve_session(self, requested: str | None, cwd: str) -> str:
        """Adopt the caller's session when there is one; never nest a server."""
        if requested:
            existing = await tmux.sessions()
            if requested in existing:
                return requested
        return await tmux.ensure_session(FALLBACK_SESSION, cwd=cwd)

    def _spawn_named_worktree(
        self, *, root: str, name: str, base_branch: str | None
    ) -> tuple[str, str]:
        """Create or join a named shared worktree.

        On first spawn for a name, creates the worktree and persists its
        identity in the ``named_worktrees`` table. On a later spawn with
        the same name and canonical main repo, joins the existing
        directory and branch — no new worktree is created.

        ``base_branch`` applies only when the named worktree is first
        created. On a join, an explicit ``base_branch`` is allowed only
        when it exactly equals the persisted ``base_branch``. If the
        persisted value is ``None`` and the join supplies any explicit
        branch, it is rejected. Omitting ``base_branch`` on a join is
        always allowed.

        Before joining, the persisted path is verified to exist as a
        linked worktree of the same canonical repository with the
        persisted branch checked out. If any fact is stale or mismatched,
        the join is refused with an actionable ``BadRequest`` — a child
        is never launched into a missing or hijacked directory.

        Returns ``(path, branch)``.
        """
        # Always key and locate under the canonical main repository,
        # not the caller's linked-worktree root. This lets a child spawned
        # from inside another linked worktree find or create the shared
        # named worktree in the right place.
        canonical_root = worktree_mod.main_repo_root(root) or root

        store = self.registry.store
        existing = store.get_named_worktree(repo_root=canonical_root, name=name)

        if existing is not None:
            # base_branch on join: allowed only when it exactly equals the
            # persisted value. If persisted is None, any explicit branch
            # is rejected.
            if base_branch is not None:
                persisted_base = existing["base_branch"]
                if persisted_base is None or base_branch != persisted_base:
                    raise BadRequest(
                        f"named worktree {name!r} was created with "
                        f"base_branch={persisted_base!r}; cannot join with "
                        f"base_branch={base_branch!r}"
                    )

            # Verify the persisted row is still intact before joining.
            worktree_mod.verify_named_worktree(
                repo_root=canonical_root,
                name=name,
                expected_path=existing["path"],
                expected_branch=existing["branch"],
            )
            return existing["path"], existing["branch"]

        path, branch = worktree_mod.create_named_worktree(
            repo_root=canonical_root, name=name, base_branch=base_branch
        )
        store.upsert_named_worktree(
            repo_root=canonical_root,
            name=name,
            branch=branch,
            path=path,
            base_branch=base_branch,
        )
        return path, branch

    #: How many times `kill` polls `pane_info` to confirm the pane is gone.
    #: tmux reaps asynchronously after `kill-pane` returns, so one immediate
    #: check races the pane's death. If the pane survives all attempts, the
    #: record is left alive and the call fails loudly: marking a live pane
    #: dead produces a ghost row the `participants.unmanaged` sweep
    #: rediscovers.
    KILL_POLL_ATTEMPTS = 5
    KILL_POLL_INTERVAL = 0.25

    async def kill_pane(self, participant_id: str) -> Participant:
        """Kill the tmux pane and confirm it is gone, nothing more.

        Job completion has to observe the worktree while it still exists —
        :meth:`teardown` removes the directory — so the caller finishes the
        participant's jobs between this method and ``teardown``. Raising on a
        surviving pane here keeps the record alive, because its jobs are still
        genuinely running and must not be finished.
        """
        p = self.registry.get(participant_id)
        if p.tmux_pane:
            await tmux.kill_pane(p.tmux_pane)
            # Confirm the pane is gone before marking the record dead.
            # `kill_pane` is fire-and-forget (check=False); a pane that
            # survives and is marked dead becomes a ghost the unmanaged
            # sweep draws back as a row the UI cannot kill.
            for _ in range(self.KILL_POLL_ATTEMPTS):
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

    def teardown(self, p: Participant) -> None:
        """Terminal teardown after the pane is confirmed gone.

        Reclaims the worktree directory and marks the record dead. The caller
        finishes the participant's jobs before this runs, because job
        completion hashes files in the worktree and ``retire`` deletes the
        directory.
        """
        # An explicit kill discards the child's work, branch included.
        self.retire(p, delete_branch=True)
        self.registry.mark_dead(p.id)

    def retire(self, p: Participant, *, delete_branch: bool) -> None:
        """Reclaim the git worktree of a participant that is going away.

        Every path that marks a participant dead should come through
        here, because a worktree outlives the pane that used it: the
        directory and the branch are repo state, not tmux state, and
        nothing else ever collects them. Before this existed only the
        explicit kill path tried, so a child that exited on its own left
        its worktree behind forever — the reason a repo with two dozen
        spawns accumulates two dozen directories under
        ``.theater/worktrees``.

        *delete_branch* encodes the difference between the two ways a
        child goes away; see :func:`worktree.remove_worktree`. A kill is
        destructive by intent, so the branch goes. A self-exit is a
        child finishing, so the branch stays and only the directory is
        pruned.

        For a **named** worktree, the directory and branch are shared
        across all children spawned with the same name in the same repo.
        Teardown must never remove a shared directory or branch while
        another live participant is using it. When other participants are
        still live in the same cwd, this method does nothing — the
        shared worktree outlives any one child, and only the last child
        out reclaims the directory. When the last child is retired, the
        directory is removed but the **branch is always retained** —
        other participants may already have completed work on it, and
        a named shared branch must never be auto-deleted merely because
        the final live participant was explicitly killed. The
        ``named_worktrees`` row is deleted only when the directory is
        actually removed. After the last teardown the branch remains, and
        the name cannot be recreated until the retained branch is merged
        or deleted by the user, or explicit future lifecycle support is
        added.

        Failure is logged, never raised. The caller is in the middle of
        retiring a participant, and refusing to mark it dead because git
        could not delete a directory would trade a leaked worktree for a
        ghost row — strictly the worse of the two.
        """
        if not (p.branch and p.branch.startswith(worktree_mod.BRANCH_PREFIX)):
            return

        # Named worktrees are shared — check whether other live participants
        # are still using the same cwd before touching the directory.
        named = None
        if self.registry is not None and self.registry.store is not None:
            named = self.registry.store.named_worktree_by_path(p.cwd or "")

        # A vanished named-worktree directory cannot answer git rev-parse,
        # but its daemon-owned row still carries the canonical repository.
        # Unique worktrees retain the child-id suffix fallback.
        root = (
            named["repo_root"]
            if named is not None
            else worktree_mod.main_repo_root(p.cwd or "", child_id=p.id)
        )
        if root is None:
            logger.warning(
                "cannot retire worktree for %s: no repo root from cwd %r",
                p.id,
                p.cwd,
            )
            return

        if named is not None:
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
            result = worktree_mod.remove_named_worktree(
                repo_root=root,
                name=named["name"],
                # Named branches are always retained: other participants may
                # have completed work on the shared branch, and auto-deleting
                # it merely because the last live participant was killed
                # would destroy completed work.
                delete_branch=False,
            )
            if result.ok:
                self.registry.store.delete_named_worktree(
                    repo_root=named["repo_root"], name=named["name"]
                )
        else:
            result = worktree_mod.remove_worktree(
                repo_root=root, child_id=p.id, delete_branch=delete_branch
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
