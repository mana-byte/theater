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
import shutil
from dataclasses import dataclass

from theater import paths
from theater.daemon import worktree as worktree_mod
from theater.daemon.registry import Registry
from theater.harness import check_model, check_resume, plan_launch
from theater.harness import get as get_harness
from theater.models import BadRequest, Participant, TheaterError
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
    worktree: bool = False
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


class Spawner:
    def __init__(self, registry: Registry):
        self.registry = registry

    async def spawn(self, req: SpawnRequest) -> Participant:
        harness = get_harness(req.harness)
        if shutil.which(harness.binary) is None:
            raise BadRequest(f"{harness.binary!r} is not on PATH")
        # Before step 1, with the PATH check, because both are refusals and a
        # refusal after step 1 leaves a participant — and possibly a worktree —
        # behind for something we could have known up front.
        check_model(req.harness, req.model)
        check_resume(req.harness, req.resume)
        # A harness that accepts resume but silently drops the prompt (opencode:
        # `-s` routes to the session view, `--prompt` is only read on the home
        # screen) must not be handed both. Delivering the prompt by typing into
        # the pane after the session is up would be the useful behaviour, but
        # the spawner has no readiness detection — the observer's screen-tail
        # marker checks live in a different module with different timing — so an
        # injection here would race the TUI's startup and sometimes land before
        # it is ready. A racy injection that intermittently drops the task is
        # worse than a loud refusal, because it fails silently rather than
        # loudly. Refused here, before anything is created, for the same reason
        # as the checks above.
        if req.resume and req.prompt and not harness.resume_takes_prompt:
            raise BadRequest(
                f"harness {req.harness!r} cannot resume a session with a prompt; "
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

        participant = self.registry.create_spawned(
            harness=req.harness, cwd=req.cwd, parent_id=req.parent_id
        )

        # Create a worktree if requested. The child runs in the worktree,
        # not the parent's repo — isolated index and HEAD.
        child_cwd = req.cwd
        if req.worktree:
            root = worktree_mod.repo_root(req.cwd)
            if root is None:
                self.registry.mark_dead(participant.id)
                raise BadRequest(
                    f"cannot create worktree: {req.cwd!r} is not in a git repo"
                )
            try:
                child_cwd = worktree_mod.create_worktree(
                    repo_root=root,
                    child_id=participant.id,
                    base_branch=req.base_branch,
                )
            except Exception:
                self.registry.mark_dead(participant.id)
                raise
            # Store the worktree path and branch on the participant.
            participant.cwd = child_cwd
            participant.branch = worktree_mod.branch_name(participant.id)
            self.registry.store.upsert_participant(participant)
            self.registry.store.bus_append(
                "participant.worktree",
                to_id=participant.id,
                payload={
                    "path": child_cwd,
                    "branch": participant.branch,
                    "root": root,
                },
            )

        config_path = paths.mcp_config_dir() / f"{participant.id}.json"
        plan = plan_launch(
            req.harness,
            participant_id=participant.id,
            prompt=req.prompt,
            config_path=config_path,
            approval=req.approval,
            model=req.model,
            resume=req.resume,
        )

        paths.ensure_home()
        for path, contents in plan.files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)

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

        # The launch epoch, read straight back out of tmux rather than
        # inferred. Best-effort on purpose: the window exists and the harness
        # is already starting, so failing the spawn over a bookkeeping lookup
        # would throw away a working agent. A missing epoch costs one delivery
        # check, and `pane_info` returning None means the pane died inside
        # these few milliseconds — which the pane-alive check catches anyway.
        try:
            info = await tmux.pane_info(pane)
        except Exception:
            info = None
        return self.registry.attach_pane(
            participant.id, pane, pane_pid=info.pane_pid if info else None
        )

    async def _resolve_session(self, requested: str | None, cwd: str) -> str:
        """Adopt the caller's session when there is one; never nest a server."""
        if requested:
            existing = await tmux.sessions()
            if requested in existing:
                return requested
        return await tmux.ensure_session(FALLBACK_SESSION, cwd=cwd)

    #: How many times `kill` polls `pane_info` to confirm the pane is gone,
    #: and how long it waits between attempts. tmux reaps a pane asynchronously
    #: rather than before `kill-pane` returns, so a single check immediately
    #: after the call is a race that the pane is about to disappear — it has
    #: been told to die but may not have done so yet. A few short polls over
    #: roughly a second is long enough for tmux to finish reaping in every case
    #: observed, and short enough that a caller blocking on the kill is not
    #: held for a perceptible time. If the pane is still there after all
    #: attempts, the record is left alive and the call fails loudly: marking
    #: the record dead while the pane lives produces the exact ghost row the
    #: `participants.unmanaged` sweep rediscovers, and that is the failure
    #: this polling exists to prevent.
    KILL_POLL_ATTEMPTS = 5
    KILL_POLL_INTERVAL = 0.25

    async def kill(self, participant_id: str) -> None:
        p = self.registry.get(participant_id)
        if p.tmux_pane:
            await tmux.kill_pane(p.tmux_pane)
            # Confirm the pane is really gone before marking the record dead.
            # `kill_pane` is a fire-and-forget call with check=False, so it
            # cannot report whether tmux honoured it. A pane that survives
            # the kill and is marked dead anyway becomes a ghost: the
            # unmanaged sweep sees a known harness running in a pane with no
            # live record, and draws it back in the régie as a row the UI
            # cannot kill.
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
        # An explicit kill discards the child's work, branch included.
        self.retire(p, delete_branch=True)
        self.registry.mark_dead(participant_id)

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

        Failure is logged, never raised. The caller is in the middle of
        retiring a participant, and refusing to mark it dead because git
        could not delete a directory would trade a leaked worktree for a
        ghost row — strictly the worse of the two.
        """
        if not (p.branch and p.branch.startswith(worktree_mod.BRANCH_PREFIX)):
            return

        # main_repo_root, not repo_root: the child's cwd *is* its
        # worktree, and `git rev-parse --show-toplevel` from in there
        # answers with the worktree itself. Feeding that back as the
        # repo root is what produced the doubled
        # `<worktree>/.theater/worktrees/<id>` path that git rejected on
        # every removal this daemon has ever attempted. child_id lets it
        # fall back to stripping the suffix when the directory is
        # already gone and git cannot answer at all.
        root = worktree_mod.main_repo_root(p.cwd or "", child_id=p.id)
        if root is None:
            logger.warning(
                "cannot retire worktree for %s: no repo root from cwd %r",
                p.id,
                p.cwd,
            )
            return

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
