"""Durable provider-backed participant launch and adoption lifecycle."""

from __future__ import annotations

from dataclasses import dataclass

from theater.daemon.spawning.models import (
    SpawnRequest,
)
from theater.daemon.worktrees.service import (
    WorkspaceRequest,
)
from theater.harness.base import ResumeLaunchOverlay
from theater.models import (
    Participant,
)


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
