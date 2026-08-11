"""Bring a new participant into existence.

Sequence, and why it is this order:

  1. mint the participant id      the MCP argv needs it, so it must exist first
  2. create the worktree (if requested)  the child runs in the worktree, not the parent's repo
  3. write the harness config     the pane will read it at startup
  4. tmux new-window -d           returns the pane id; identity is now settled
  5. record the pane              nothing was inferred at any point

If step 4 fails the participant is marked dead immediately rather than left as a
STARTING ghost that the régie would draw forever.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from theater import paths
from theater.daemon import worktree as worktree_mod
from theater.daemon.registry import Registry
from theater.harness import get as get_harness
from theater.harness import plan_launch
from theater.models import BadRequest, Participant
from theater.tmux import client as tmux

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


class Spawner:
    def __init__(self, registry: Registry):
        self.registry = registry

    async def spawn(self, req: SpawnRequest) -> Participant:
        harness = get_harness(req.harness)
        if shutil.which(harness.binary) is None:
            raise BadRequest(f"{harness.binary!r} is not on PATH")

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

        return self.registry.attach_pane(participant.id, pane)

    async def _resolve_session(self, requested: str | None, cwd: str) -> str:
        """Adopt the caller's session when there is one; never nest a server."""
        if requested:
            existing = await tmux.sessions()
            if requested in existing:
                return requested
        return await tmux.ensure_session(FALLBACK_SESSION, cwd=cwd)

    async def kill(self, participant_id: str) -> None:
        p = self.registry.get(participant_id)
        if p.tmux_pane:
            await tmux.kill_pane(p.tmux_pane)
        # Clean up the worktree if the participant had one.
        if p.branch and p.branch.startswith(worktree_mod.BRANCH_PREFIX):
            root = worktree_mod.repo_root(p.cwd or "")
            if root:
                worktree_mod.remove_worktree(
                    repo_root=root, child_id=participant_id
                )
        self.registry.mark_dead(participant_id)
