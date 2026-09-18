"""Private operator management adapters over the shared RC10 services."""

from __future__ import annotations

from collections.abc import Mapping

from jsonschema.exceptions import ValidationError

from theater.daemon.control_ownership import ControlTransferService
from theater.daemon.rpc.router import method
from theater.frontend.capabilities import METHOD_CATALOG
from theater.frontend.schemas import validator_for
from theater.models import BadRequest


def _page_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 500:
        raise BadRequest("page limit must be an integer from 1 through 500")
    return value


def _idempotency_key(params: Mapping[str, object]) -> str:
    value = params.get("idempotency_key")
    if not isinstance(value, str) or not value:
        raise BadRequest("idempotency_key must be a non-empty string")
    return value


def _validate_public_params(method_name: str, value: Mapping[str, object]) -> None:
    """Keep private operator adapters aligned with the frozen shared request shape."""
    try:
        validator_for(METHOD_CATALOG[method_name].params_schema_id).validate(dict(value))
    except ValidationError as exc:
        raise BadRequest(f"invalid management request: {exc.message}") from exc


@method("providers.list")
async def providers_list(daemon, params: dict) -> dict[str, object]:
    limit = _page_limit(params.get("limit", 200))
    cursor = params.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise BadRequest("provider cursor must be a string")
    try:
        records, next_cursor = daemon.terminal_service.registry.list(cursor=cursor, limit=limit)
    except Exception as exc:
        if getattr(exc, "code", None) == "bad_request":
            raise BadRequest(str(exc)) from exc
        raise
    return {
        "items": [daemon.terminal_service.registry.project(record) for record in records],
        "next_cursor": next_cursor,
    }


@method("providers.get")
async def providers_get(daemon, params: dict) -> dict[str, object]:
    provider_id = params.get("provider_id")
    if not isinstance(provider_id, str) or not provider_id:
        raise BadRequest("provider_id must be a non-empty string")
    record = daemon.terminal_service.registry.get(provider_id)
    return daemon.terminal_service.registry.project(record)


@method("workspaces.list")
async def workspaces_list(daemon, params: dict) -> dict[str, object]:
    limit = _page_limit(params.get("limit", 200))
    cursor = params.get("cursor")
    state = params.get("state")
    if cursor is not None and not isinstance(cursor, str):
        raise BadRequest("workspace cursor must be a string")
    if state is not None and not isinstance(state, str):
        raise BadRequest("workspace state must be a string")
    try:
        records, next_cursor = daemon.workspace_service.list(
            cursor=cursor, limit=limit, state=state
        )
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    return {
        "items": [daemon.workspace_service.project(record) for record in records],
        "next_cursor": next_cursor,
    }


@method("workspaces.get")
async def workspaces_get(daemon, params: dict) -> dict[str, object]:
    workspace_id = params.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        raise BadRequest("workspace_id must be a non-empty string")
    return daemon.workspace_service.project(daemon.workspace_service.get(workspace_id))


@method("workspaces.cleanup")
async def workspaces_cleanup(daemon, params: dict) -> object:
    workspace_id = params.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        raise BadRequest("workspace_id must be a non-empty string")
    request = {
        "workspace_id": workspace_id,
        "force": params.get("force", False),
        "delete_branch": params.get("delete_branch", False),
        "force_branch": params.get("force_branch", False),
    }
    _validate_public_params("frontend.workspaces.cleanup", request)
    return daemon.workspace_service.cleanup(
        client_id="theater-cli",
        actor_participant_id=None,
        idempotency_key=_idempotency_key(params),
        params=request,
    )


@method("controls.transfer")
async def controls_transfer(daemon, params: dict) -> object:
    requested = params.get("participants")
    owner = params.get("new_owner")
    if not isinstance(requested, list) or not all(isinstance(item, Mapping) for item in requested):
        raise BadRequest("participants must be an array of participant revision objects")
    if not isinstance(owner, Mapping):
        raise BadRequest("new_owner must be an object")
    request = {"participants": requested, "new_owner": owner}
    _validate_public_params("frontend.participants.transfer_control", request)
    participant_ids = [str(item.get("participant_id", "")) for item in requested]
    service = ControlTransferService(daemon)
    async with daemon.controls.hold_participant_locks(participant_ids):
        return daemon.operation_service.execute_idempotent(
            client_id="theater-cli",
            idempotency_key=_idempotency_key(params),
            # The private transport deliberately shares the durable public operation
            # contract rather than inventing a second idempotency namespace.
            method="frontend.participants.transfer_control",
            params=request,
            action=lambda unit: service.transfer(requested, owner, unit=unit),
        ).value


__all__ = [
    "controls_transfer",
    "providers_get",
    "providers_list",
    "workspaces_cleanup",
    "workspaces_get",
    "workspaces_list",
]
