"""Provider-backed participant launch composed from focused concern mixins."""

from __future__ import annotations

import shutil
from collections.abc import Mapping

from theater.daemon.operations import OperationService
from theater.daemon.spawning.models import SpawnRequest
from theater.daemon.spawning.provider_launch_parts.admission import SpawnAdmission
from theater.daemon.spawning.provider_launch_parts.adoption import ParticipantAdoption
from theater.daemon.spawning.provider_launch_parts.execution import SpawnExecution
from theater.daemon.spawning.provider_launch_parts.rollback import SpawnRollback
from theater.daemon.spawning.provider_launch_parts.validation import LaunchValidation
from theater.harness.contracts.runtime import RuntimeWiring
from theater.models import BadRequest


class ParticipantLaunchService(
    SpawnAdmission,
    SpawnExecution,
    SpawnRollback,
    ParticipantAdoption,
    LaunchValidation,
):
    """RC10 public spawn/adoption orchestration over existing daemon services."""

    DEFAULT_PROVIDER_SELECTOR = "tmux"

    def __init__(self, daemon) -> None:
        self.daemon = daemon
        self.store = daemon.store
        self.registry = daemon.registry
        self.spawner = daemon.spawner
        self.operations: OperationService = daemon.operation_service
        self.terminals = daemon.terminal_service
        self.workspaces = daemon.workspace_service
        terminal_config = getattr(getattr(daemon, "config", None), "terminals", None)
        configured_default = getattr(terminal_config, "default_provider", None)
        self.default_provider_selector = (
            configured_default
            if isinstance(configured_default, str) and configured_default
            else self.DEFAULT_PROVIDER_SELECTOR
        )

    @staticmethod
    def _spawn_request(
        params: Mapping[str, object],
        *,
        parent_id: str | None,
        cwd: str,
        worktree: bool | str,
        base_ref: str | None,
    ) -> SpawnRequest:
        raw_wiring = params.get("wiring", RuntimeWiring.AUTO.value)
        try:
            wiring = RuntimeWiring(str(raw_wiring))
        except ValueError:
            raise BadRequest("wiring must be auto, native, or legacy") from None
        return SpawnRequest(
            harness=str(params["harness"]),
            prompt=str(params.get("prompt") or ""),
            cwd=cwd,
            approval=str(params["approval"]),
            parent_id=parent_id,
            worktree=worktree,
            base_branch=base_ref,
            model=ParticipantLaunchService._optional_text(params.get("model")),
            reasoning_effort=ParticipantLaunchService._optional_text(
                params.get("reasoning_effort")
            ),
            resume=ParticipantLaunchService._optional_text(params.get("resume")),
            name=ParticipantLaunchService._optional_text(params.get("name")),
            description=ParticipantLaunchService._optional_text(params.get("description")),
            wiring=wiring,
            response_format=ParticipantLaunchService._optional_text(params.get("response_format")),
        )

    @staticmethod
    def _optional_text(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _resolve_binary(name: str) -> str | None:
        return shutil.which(name)


__all__ = ["ParticipantLaunchService"]
