"""Dataclasses for the spawn lifecycle.

``SpawnRequest`` carries every parameter the RPC layer assembles.
``Reservation`` carries everything ``reserve`` produced that ``launch``
needs, so the daemon can create its spawn job between the two steps.
"""

from __future__ import annotations

from dataclasses import dataclass

from theater.harness.base import LaunchPlan
from theater.models import Participant


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
    #: True: isolated worktree; non-empty str: named shared worktree; None/False: no worktree.
    worktree: str | bool | None = False
    #: Base branch for the worktree; defaults to current HEAD.
    base_branch: str | None = None
    #: Opaque model spelling; None means the harness picks.
    model: str | None = None
    #: Opaque reasoning effort; None means the harness picks its default.
    reasoning_effort: str | None = None
    #: Opaque session id to resume; None means start cold.
    resume: str | None = None
    #: Raw serialized JSON response-format hint; only launch-time traps are enforced here.
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
    resume_predecessor: Participant | None = None
