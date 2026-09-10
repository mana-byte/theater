"""Dataclasses for the spawn lifecycle.

``SpawnRequest`` carries every parameter the RPC layer assembles.
``Reservation`` carries everything ``reserve`` produced that ``launch``
needs, so the daemon can create its spawn job between the two steps.
"""

from __future__ import annotations

from dataclasses import dataclass

from theater.harness.base import LaunchPlan
from theater.harness.contracts.runtime import (
    RuntimeCompatibility,
    RuntimeManifest,
    RuntimeWiring,
)
from theater.models import Participant


@dataclass(frozen=True, slots=True)
class NativeSpawnSelection:
    """The verified native wiring decision for one spawn.

    Generic spawn policy — never a harness branch. Carries the harness
    manifest's runtime declaration, the private endpoint the daemon owns,
    the launch generation, the Theater-verified compatibility facts, and,
    for a fork, the predecessor's exact native session id.
    """

    runtime: RuntimeManifest
    endpoint: str
    backend_generation: int
    compatibility: RuntimeCompatibility
    #: The exact parent native session id for a history fork, or ``None`` for NEW.
    fork_parent_session: str | None = None


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
    #: Live-only alias for this participant; never inherited by a resume.
    name: str | None = None
    #: Durable summary; a resumed participant inherits only when this is None.
    description: str | None = None
    #: Raw serialized JSON response-format hint; only launch-time traps are enforced here.
    response_format: str | None = None
    #: Internal wiring selection seam. The default is ``AUTO``; public spawn
    #: surfaces do not accept a wiring parameter yet (Wave 4 owns those
    #: fields), so today every RPC spawn arrives as ``AUTO`` — which selects
    #: legacy until the Wave 5 release gate enables automatic native
    #: selection. ``LEGACY`` is the explicit opt-out; ``NATIVE`` is the
    #: explicit, diagnostically-failing request.
    wiring: RuntimeWiring = RuntimeWiring.AUTO


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
    #: The verified native wiring decision, or ``None`` for the legacy path.
    native: NativeSpawnSelection | None = None
