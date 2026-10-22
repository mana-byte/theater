"""Exercise real provider preparation with a participant admitted for a focused plan test."""

from __future__ import annotations

from dataclasses import replace

from theater.daemon.spawning.models import ProviderLaunchSelection, Reservation, SpawnRequest
from theater.daemon.spawning.service import Spawner
from theater.harness import get as get_harness


async def prepare_provider_reservation(spawner: Spawner, request: SpawnRequest) -> Reservation:
    """Supply production's prevalidated identity to its provider preparation seam."""
    registry = spawner.registry
    request = spawner._resolve_resume_reference(request)
    predecessor, overlay, _identity = await spawner._validate_before_create(
        request, get_harness(request.harness)
    )
    if request.worktree:
        raise AssertionError("workspace creation belongs to WorkspaceService or launch admission")
    if overlay is not None and overlay.cwd is not None:
        request = replace(request, cwd=overlay.cwd)
    participant = registry.create_spawned(
        harness=request.harness,
        cwd=request.cwd,
        parent_id=request.parent_id,
        has_prompt=bool(request.prompt),
        resumed_from_id=predecessor.id if predecessor is not None else None,
        name=request.name,
        description=request.description,
    )
    provider = ProviderLaunchSelection(
        provider_id="test-provider",
        provider_generation=1,
        operation_id="test-preparation",
        launch_id="test-preparation",
        terminal_service=None,
        mark_dispatched=lambda: None,
        bind_terminal=lambda _terminal: participant,
    )
    return await spawner.prepare_provider_launch(
        request,
        participant,
        child_cwd=request.cwd,
        provider=provider,
        workspace_usage_id=None,
        resume_predecessor=predecessor,
        resume_overlay=overlay,
        prevalidated=True,
    )
