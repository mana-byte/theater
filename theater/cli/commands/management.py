"""Small operator-only management commands backed by private daemon RPCs."""

from __future__ import annotations

import json
import secrets

from theater.cli.errors import BadUsage
from theater.client import call_sync


def _emit(value: object, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
        return
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        rows = value["items"]
        if not rows:
            print("none")
            return
        for row in rows:
            if isinstance(row, dict):
                identity = row.get("provider_id", row.get("workspace_id", "-"))
                label = row.get("selector", row.get("path", "-"))
                state = row.get("health", row.get("state", "-"))
                print(f"{identity}  {label}  {state}")
        return
    print(json.dumps(value, indent=2, sort_keys=True))


def cmd_providers(args) -> int:
    if args.providers_command == "list":
        result = call_sync("providers.list", cursor=args.cursor, limit=args.limit)
    else:
        result = call_sync("providers.get", provider_id=args.provider_id)
    _emit(result, as_json=args.json)
    return 0


def cmd_workspaces(args) -> int:
    if args.workspaces_command == "list":
        result = call_sync(
            "workspaces.list", cursor=args.cursor, limit=args.limit, state=args.state
        )
    elif args.workspaces_command == "get":
        result = call_sync("workspaces.get", workspace_id=args.workspace_id)
    else:
        result = call_sync(
            "workspaces.cleanup",
            workspace_id=args.workspace_id,
            force=args.force,
            delete_branch=args.delete_branch,
            force_branch=args.force_branch,
            idempotency_key=args.idempotency_key or _key("workspace-cleanup"),
        )
    _emit(result, as_json=args.json)
    return 0


def cmd_control_transfer(args) -> int:
    revisions = args.expected_revision
    if len(revisions) != len(args.participant_ids):
        raise BadUsage("pass one --expected-revision for each participant id")
    owner = (
        {"kind": "local_operator", "participant_id": None}
        if args.new_owner in {"local", "local_operator"}
        else {"kind": "participant", "participant_id": args.new_owner}
    )
    result = call_sync(
        "controls.transfer",
        participants=[
            {"participant_id": participant_id, "expected_revision": revision}
            for participant_id, revision in zip(args.participant_ids, revisions, strict=True)
        ],
        new_owner=owner,
        idempotency_key=args.idempotency_key or _key("control-transfer"),
    )
    _emit(result, as_json=args.json)
    return 0


def _key(prefix: str) -> str:
    return f"{prefix}-{secrets.token_urlsafe(18)}"


__all__ = ["cmd_control_transfer", "cmd_providers", "cmd_workspaces"]
