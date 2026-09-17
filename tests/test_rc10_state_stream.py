"""Focused public snapshot and orchestration-journal follow coverage."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from theater import paths, protocol
from theater.daemon.events.reader import JournalReader, StateReadError, StreamCursor
from theater.daemon.events.snapshot import SnapshotService
from theater.daemon.persistence.repositories.journal import JournalAppend
from theater.daemon.store import Store
from theater.frontend.capabilities import PUBLIC_API_MAJOR, PUBLIC_API_MINOR
from theater.models import (
    JournalEventRecord,
    ProviderRecord,
    PublicOperationRecord,
    Status,
    WorkspaceRecord,
    WorkspaceUsageRecord,
)


def _request(request_id: int, method: str, params: dict[str, object] | None = None) -> bytes:
    return protocol.encode({"id": request_id, "method": method, "params": params or {}})


def _handshake(client_id: str) -> bytes:
    return _request(
        1,
        "frontend.handshake",
        {
            "api": {"major": PUBLIC_API_MAJOR, "minor": PUBLIC_API_MINOR},
            "client_id": client_id,
            "role": "operator",
            "channel": "rpc",
            "required_capabilities": [],
        },
    )


async def _public_connection(client_id: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_unix_connection(
        str(paths.socket_path()), limit=max(protocol.MAX_MESSAGE_BYTES, 1024)
    )
    writer.write(_handshake(client_id))
    await writer.drain()
    response = json.loads(await protocol.read_message(reader))
    assert response["ok"] is True
    return reader, writer


async def _call(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, frame: bytes) -> dict:
    writer.write(frame)
    await writer.drain()
    return json.loads(await protocol.read_message(reader))


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    await writer.wait_closed()


def _event(entity_id: str, revision: int = 1) -> JournalEventRecord:
    return JournalEventRecord(
        kind="participant.updated",
        entity_id=entity_id,
        entity_revision=revision,
        payload={"participant_id": entity_id},
        recorded_at=1.0,
    )


def _append(daemon, *events: JournalEventRecord, transaction_id: str) -> JournalAppend:
    with daemon.store.write_unit() as unit:
        return daemon.store.journal.append_group(unit, list(events), transaction_id=transaction_id)


async def test_snapshot_pages_are_immutable_active_and_publicly_validated(daemon) -> None:
    first = daemon.registry.register(harness="vibe", pane="%state-1", cwd="/tmp/state-1")
    second = daemon.registry.register(harness="vibe", pane="%state-2", cwd="/tmp/state-2")
    dead = daemon.registry.register(harness="vibe", pane=None, cwd="/tmp/state-dead")
    daemon.registry.set_status(dead.id, Status.DEAD)
    daemon.jobs.create(
        handle="state-job",
        caller_id="state-caller",
        target_id=first.id,
        kind="send",
    )
    with daemon.store.write_unit() as unit:
        daemon.store.operations.create(
            PublicOperationRecord(
                operation_id="state-operation",
                kind="send",
                actor_client_id="state-client",
                actor_participant_id=None,
                target_ids=(first.id,),
                state="accepted",
                phase="accepted",
                created_at=1.0,
                updated_at=1.0,
            ),
            connection=unit.connection,
        )
        daemon.store.providers.register(
            ProviderRecord(
                provider_id="state-provider",
                selector="state-provider",
                kind="fixture",
                credential_verifier="not-a-credential",
                configuration_version=1,
                capabilities=("terminal-provider.v1",),
                limits={"terminals": 1},
                generation=2,
                last_report_revision=3,
                created_at=1.0,
                updated_at=1.0,
            ),
            connection=unit.connection,
        )
        workspace = WorkspaceRecord(
            workspace_id="state-workspace",
            ownership_kind="theater",
            owner_id="state-client",
            path="/tmp/state-workspace",
            state="active",
            created_at=1.0,
            updated_at=1.0,
        )
        daemon.store.workspaces.create(workspace, connection=unit.connection)
        assert daemon.store.workspaces.acquire_usage(
            WorkspaceUsageRecord(
                usage_id="state-usage",
                workspace_id=workspace.workspace_id,
                holder_kind="participant",
                holder_id=first.id,
                acquired_at=1.0,
            ),
            connection=unit.connection,
        )
        daemon.store.journal.append_group(
            unit, [_event(first.id)], transaction_id="state-snapshot-event"
        )

    reader, writer = await _public_connection("state-snapshot-client")
    try:
        snapshot_response = await _call(
            reader, writer, _request(2, "frontend.state.snapshot", {"page_size": 1})
        )
        snapshot = snapshot_response["result"]
        assert snapshot_response["ok"] is True
        assert snapshot["ending_cursor"]["sequence"] == 1
        assert snapshot["participants"][0]["participant_id"] == first.id
        assert snapshot["operations"][0]["operation_id"] == "state-operation"
        assert snapshot["jobs"][0]["handle"] == "state-job"
        assert snapshot["providers"][0]["health"] == "unknown"
        assert snapshot["workspaces"][0]["usages"][0]["usage_id"] == "state-usage"
        assert dead.id not in {item["participant_id"] for item in snapshot["participants"]}

        daemon.registry.set_status(second.id, Status.DEAD)
        second_page = await _call(
            reader,
            writer,
            _request(
                3,
                "frontend.state.page",
                {"snapshot_id": snapshot["snapshot_id"], "page": 1},
            ),
        )
        assert second_page["result"]["participants"][0]["participant_id"] == second.id
        assert second_page["result"]["participants"][0]["status"] != "dead"
        assert second_page["result"]["complete"] is True

        released = await _call(
            reader,
            writer,
            _request(4, "frontend.state.release", {"snapshot_id": snapshot["snapshot_id"]}),
        )
        assert released["result"] == {"released": True}
        expired = await _call(
            reader,
            writer,
            _request(
                5,
                "frontend.state.page",
                {"snapshot_id": snapshot["snapshot_id"], "page": 0},
            ),
        )
        assert expired["error"]["code"] == "snapshot_expired"
    finally:
        await _close(writer)


async def test_snapshot_cache_expires_and_refuses_overflow(daemon) -> None:
    timestamp = 100.0

    def clock() -> float:
        return timestamp

    names = iter(("snapshot-a", "snapshot-b", "snapshot-c"))
    service = SnapshotService(daemon.store, clock=clock, id_factory=lambda: next(names))
    first = service.snapshot("client-a", page_size=1)
    first_snapshot_id = first["snapshot_id"]
    assert isinstance(first_snapshot_id, str)
    service.snapshot("client-a", page_size=1)
    with pytest.raises(StateReadError, match="maximum number"):
        service.snapshot("client-a", page_size=1)
    with pytest.raises(StateReadError, match="belongs to another client"):
        service.page("client-b", first_snapshot_id, 0)

    timestamp += 61.0
    with pytest.raises(StateReadError, match="missing, expired"):
        service.page("client-a", first_snapshot_id, 0)
    assert service.cache.used_bytes == 0

    too_small = SnapshotService(
        daemon.store,
        clock=clock,
        id_factory=lambda: "snapshot-small",
        cache_bytes=1,
    )
    with pytest.raises(StateReadError, match="cannot fit"):
        too_small.snapshot("client-b", page_size=1)


async def test_follow_keeps_groups_whole_and_closes_waiter_races(daemon) -> None:
    stream_id = daemon.store.journal.stream_id()
    cursor = {"stream_id": stream_id, "sequence": 0}
    reader, writer = await _public_connection("state-follow-client")
    try:
        empty = await _call(
            reader,
            writer,
            _request(
                2,
                "frontend.state.follow",
                {"cursor": cursor, "wait_seconds": 0, "limit": 1},
            ),
        )
        assert empty["result"] == {"transactions": [], "cursor": cursor, "timed_out": False}

        first = _append(
            daemon,
            _event("state-a"),
            _event("state-b", 2),
            transaction_id="state-group-a",
        )
        followed = await _call(
            reader,
            writer,
            _request(3, "frontend.state.follow", {"cursor": cursor, "limit": 1}),
        )
        transaction = followed["result"]["transactions"]
        assert len(transaction) == 1
        assert [event["entity_id"] for event in transaction[0]["events"]] == ["state-a", "state-b"]
        assert followed["result"]["cursor"]["sequence"] == first.ending_sequence

        replay = await _call(
            reader,
            writer,
            _request(4, "frontend.state.follow", {"cursor": cursor, "limit": 1}),
        )
        assert replay["result"] == followed["result"]

        boundary_error = await _call(
            reader,
            writer,
            _request(
                5,
                "frontend.state.follow",
                {"cursor": {"stream_id": stream_id, "sequence": 1}, "wait_seconds": 0},
            ),
        )
        assert boundary_error["error"]["code"] == "resnapshot_required"

        timeout = await _call(
            reader,
            writer,
            _request(
                6,
                "frontend.state.follow",
                {"cursor": followed["result"]["cursor"], "wait_seconds": 0.01},
            ),
        )
        assert timeout["result"]["transactions"] == []
        assert timeout["result"]["timed_out"] is True

        waiting = asyncio.create_task(
            daemon.state_service.follow(followed["result"]["cursor"], wait_seconds=1, limit=1)
        )
        for _ in range(10):
            if daemon.state_service.follows.waiter_count:
                break
            await asyncio.sleep(0)
        assert daemon.state_service.follows.waiter_count == 1
        second = _append(daemon, _event("state-c", 3), transaction_id="state-group-b")
        woken = await waiting
        assert woken["cursor"]["sequence"] == second.ending_sequence
        assert woken["timed_out"] is False

        cancelled = asyncio.create_task(
            daemon.state_service.follow(woken["cursor"], wait_seconds=30, limit=1)
        )
        for _ in range(10):
            if daemon.state_service.follows.waiter_count:
                break
            await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert daemon.state_service.follows.waiter_count == 0

        with daemon.store.write_unit() as unit:
            daemon.store.journal.delete_through(first.ending_sequence, connection=unit.connection)
        retention = await _call(
            reader,
            writer,
            _request(7, "frontend.state.follow", {"cursor": cursor, "wait_seconds": 0}),
        )
        assert retention["error"]["code"] == "resnapshot_required"
        mismatch = await _call(
            reader,
            writer,
            _request(
                8,
                "frontend.state.follow",
                {"cursor": {"stream_id": "replacement-stream", "sequence": 0}},
            ),
        )
        assert mismatch["error"]["code"] == "resnapshot_required"
    finally:
        await _close(writer)


def test_journal_cursor_resumes_after_restart_but_not_database_replacement(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = Store(path)
    try:
        with store.write_unit() as unit:
            appended = store.journal.append_group(
                unit, [_event("restart-a")], transaction_id="restart-group"
            )
        cursor = StreamCursor(appended.stream_id, appended.ending_sequence)
    finally:
        store.close()

    reopened = Store(path)
    replacement = Store(tmp_path / "replacement.db")
    try:
        resumed = JournalReader(reopened.journal).read(cursor, limit=1)
        assert resumed.transactions == ()
        assert resumed.cursor == cursor
        with pytest.raises(StateReadError, match="different database stream"):
            JournalReader(replacement.journal).read(cursor, limit=1)
    finally:
        reopened.close()
        replacement.close()
