"""Wire-value builders for the StateSynchronizer tests: snapshot pages, events and follows."""

from __future__ import annotations

import asyncio

from tests.support.frontend_wire import read_request
from tests.support.sdk_fixture_server import send_frame


async def state_handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    request = await read_request(reader)
    assert request is not None
    assert request["method"] == "frontend.handshake"
    request_id = request["id"]
    assert type(request_id) is int
    await send_frame(
        writer,
        {
            "id": request_id,
            "ok": True,
            "result": {
                "api": {"major": 1, "minor": 1},
                "daemon_instance_id": "fixture-daemon",
                "package_version": "0.0.0-test",
                "capabilities": ["orchestration.v1", "state.follow.v1"],
                "limits": {"max_frame_bytes": 67_108_864, "max_in_flight": 1},
            },
        },
    )


def participant_frame(
    participant_id: str,
    status: str,
    revision: int,
    *,
    name: str | None = None,
    owner: dict[str, object] | None = None,
    actions: dict[str, object] | None = None,
    terminal_route: dict[str, object] | None = None,
    addressable: bool = False,
) -> dict[str, object]:
    value: dict[str, object] = {
        "participant_id": participant_id,
        "origin": "spawned",
        "harness": "codex",
        "status": status,
        "owner": owner or {"kind": "local_operator", "revision": revision},
        "addressable": addressable,
        "presence": "unknown",
        "actions": actions or {},
        "projection_revision": revision,
    }
    if name is not None:
        value["name"] = name
    if terminal_route is not None:
        value["terminal_route"] = terminal_route
    return value


def provider_frame(health: str) -> dict[str, object]:
    return {
        "provider_id": "provider-a",
        "selector": "provider-a",
        "kind": "fixture",
        "generation": 1,
        "health": health,
        "capabilities": [],
    }


def operation_frame(
    state: str, phase: str, *, operation_id: str = "operation-a"
) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "kind": "send",
        "state": state,
        "phase": phase,
        "actor": {"client_id": "sdk-follow"},
        "target_ids": ["participant-a"],
        "job_handle": "job-a",
    }


def job_frame(handle: str, state: str) -> dict[str, object]:
    return {
        "handle": handle,
        "state": state,
        "kind": "send",
        "actor": {"client_id": "sdk-follow"},
        "target_id": "participant-a",
    }


def workspace_frame(
    workspace_id: str, state: str, *, usages: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {
        "workspace_id": workspace_id,
        "ownership_kind": "theater",
        "owner_id": "sdk-follow",
        "path": f"/tmp/{workspace_id}",
        "state": state,
        "usages": usages or [],
    }


def snapshot_page(
    snapshot_id: str,
    page: int,
    complete: bool,
    sequence: int,
    *,
    participants: list[dict[str, object]] | None = None,
    operations: list[dict[str, object]] | None = None,
    jobs: list[dict[str, object]] | None = None,
    providers: list[dict[str, object]] | None = None,
    workspaces: list[dict[str, object]] | None = None,
    usage: dict[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "snapshot_id": snapshot_id,
        "page": page,
        "complete": complete,
        "ending_cursor": {"stream_id": "stream-a", "sequence": sequence},
        "participants": participants or [],
        "operations": operations or [],
        "jobs": jobs or [],
        "providers": providers or [],
        "workspaces": workspaces or [],
    }
    if usage is not None:
        value["usage"] = usage
    return value


def journal_event(
    kind: str,
    entity_id: str,
    revision: int,
    payload: dict[str, object],
    **extra: object,
) -> dict[str, object]:
    return {
        "kind": kind,
        "entity_id": entity_id,
        "entity_revision": revision,
        "payload": payload,
        **extra,
    }


def journal_transaction(
    transaction_id: str, sequence: int, *events: dict[str, object]
) -> dict[str, object]:
    return {
        "transaction_id": transaction_id,
        "events": list(events),
        "ending_cursor": {"stream_id": "stream-a", "sequence": sequence},
    }


def follow_result(
    sequence: int, *transactions: dict[str, object], timed_out: bool = False
) -> dict[str, object]:
    return {
        "transactions": list(transactions),
        "cursor": {"stream_id": "stream-a", "sequence": sequence},
        "timed_out": timed_out,
    }
