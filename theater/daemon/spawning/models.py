"""Dataclasses for the spawn lifecycle (``SpawnRequest``, ``Reservation``)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

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

    Generic spawn policy — never a harness branch.
    """

    runtime: RuntimeManifest
    #: Fixed endpoint, or ``None`` when the manifest discovers it from stdout.
    endpoint: str | None
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
    #: ``AUTO`` and ``NATIVE`` prefer a compatible native runtime, otherwise
    #: retain the ordinary launch. ``LEGACY`` is the explicit opt-out.
    wiring: RuntimeWiring = RuntimeWiring.AUTO


@dataclass(frozen=True, slots=True)
class ProviderLaunchSelection:
    """One immutable provider generation selected for terminal creation."""

    provider_id: str
    provider_generation: int
    operation_id: str
    launch_id: str
    terminal_service: Any
    mark_dispatched: Callable[[], None]
    bind_terminal: Callable[[Mapping[str, object]], Participant]


class ProviderLaunchOutcome(Exception):
    """Carry a definitive or uncertain provider result without legacy cleanup."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        super().__init__(outcome.phase)


@dataclass(slots=True)
class Reservation:
    """Everything ``reserve`` produced that ``launch`` needs, without re-deriving.

    The daemon creates its spawn job in between, so it is RUNNING before the terminal outputs.
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
    #: The ordinary plan retained until a passive frontend listener is live.
    legacy_plan: LaunchPlan | None = None
    #: Present only for an RC10 provider-backed terminal launch.
    provider: ProviderLaunchSelection | None = None
    #: Durable reservation usage retained across callback loss/restart.
    workspace_usage_id: str | None = None
