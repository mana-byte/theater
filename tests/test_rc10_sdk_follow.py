"""Public-SDK snapshot, follow, and operation-wait synchronization coverage."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from theater.frontend import (
    FrontendClient,
    RequestUncertain,
    StateProjection,
    StateSynchronizationError,
    StateSynchronizer,
)

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


@asynccontextmanager
async def _fixture_server(handler: Handler) -> AsyncIterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="rc10-sdk-follow-", dir="/tmp"))
    socket_path = root / "frontend.sock"
    writers: set[asyncio.StreamWriter] = set()
    tasks: set[asyncio.Task[None]] = set()
    failures: list[BaseException] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        writers.add(writer)
        try:
            await handler(reader, writer)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            failures.append(exc)
        finally:
            writers.discard(writer)
            tasks.discard(task)
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_unix_server(serve, path=str(socket_path))
    try:
        yield socket_path
    finally:
        server.close()
        await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
        if tasks:
            for task in tuple(tasks):
                task.cancel()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=1)
        socket_path.unlink(missing_ok=True)
        shutil.rmtree(root)
        if failures:
            raise failures[0]


async def _read_request(reader: asyncio.StreamReader) -> dict[str, object] | None:
    frame = await reader.readline()
    if not frame:
        return None
    assert frame.endswith(b"\n")
    value = json.loads(frame)
    assert isinstance(value, dict)
    return value


async def _send(writer: asyncio.StreamWriter, value: dict[str, object]) -> None:
    writer.write(json.dumps(value, allow_nan=False, separators=(",", ":")).encode() + b"\n")
    await writer.drain()


async def _handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    request = await _read_request(reader)
    assert request is not None
    assert request["method"] == "frontend.handshake"
    request_id = request["id"]
    assert type(request_id) is int
    await _send(
        writer,
        {
            "id": request_id,
            "ok": True,
            "result": {
                "api": {"major": 1, "minor": 1},
                "daemon_instance_id": "fixture-daemon",
                "package_version": "1.0.0rc10",
                "capabilities": ["orchestration.v1", "state.follow.v1"],
                "limits": {"max_frame_bytes": 67_108_864, "max_in_flight": 1},
            },
        },
    )


def _participant(
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


def _provider(health: str) -> dict[str, object]:
    return {
        "provider_id": "provider-a",
        "selector": "provider-a",
        "kind": "fixture",
        "generation": 1,
        "health": health,
        "capabilities": [],
    }


def _operation(state: str, phase: str, *, operation_id: str = "operation-a") -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "kind": "send",
        "state": state,
        "phase": phase,
        "actor": {"client_id": "sdk-follow"},
        "target_ids": ["participant-a"],
        "job_handle": "job-a",
    }


def _job(handle: str, state: str) -> dict[str, object]:
    return {
        "handle": handle,
        "state": state,
        "kind": "send",
        "actor": {"client_id": "sdk-follow"},
        "target_id": "participant-a",
    }


def _workspace(
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


def _snapshot(
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


def _event(
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


def _transaction(
    transaction_id: str, sequence: int, *events: dict[str, object]
) -> dict[str, object]:
    return {
        "transaction_id": transaction_id,
        "events": list(events),
        "ending_cursor": {"stream_id": "stream-a", "sequence": sequence},
    }


def _follow(
    sequence: int, *transactions: dict[str, object], timed_out: bool = False
) -> dict[str, object]:
    return {
        "transactions": list(transactions),
        "cursor": {"stream_id": "stream-a", "sequence": sequence},
        "timed_out": timed_out,
    }


def _state_wire(projection: StateProjection) -> dict[str, dict[str, dict[str, object]]]:
    return {
        "participants": {
            identifier: item.to_wire() for identifier, item in projection.participants.items()
        },
        "operations": {
            identifier: item.to_wire() for identifier, item in projection.operations.items()
        },
        "jobs": {identifier: item.to_wire() for identifier, item in projection.jobs.items()},
        "providers": {
            identifier: item.to_wire() for identifier, item in projection.providers.items()
        },
        "workspaces": {
            identifier: item.to_wire() for identifier, item in projection.workspaces.items()
        },
    }


@pytest.mark.asyncio
async def test_snapshot_assembly_is_atomic_and_recovers_from_expiry() -> None:  # noqa: PLR0915
    snapshots = iter(("snapshot-old", "snapshot-broken", "snapshot-expired", "snapshot-new"))
    released: list[str] = []

    async def handler(  # noqa: PLR0912
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await _handshake(reader, writer)
        while request := await _read_request(reader):
            method = request["method"]
            request_id = request["id"]
            assert type(request_id) is int
            params = request["params"]
            assert isinstance(params, dict)
            if method == "frontend.state.snapshot":
                snapshot_id = next(snapshots)
                if snapshot_id == "snapshot-old":
                    result = _snapshot(
                        snapshot_id,
                        0,
                        True,
                        1,
                        participants=[_participant("participant-a", "idle", 1)],
                        usage={"input_tokens": 3},
                    )
                elif snapshot_id == "snapshot-new":
                    result = _snapshot(
                        snapshot_id,
                        0,
                        True,
                        4,
                        participants=[_participant("participant-a", "ready", 4)],
                    )
                else:
                    result = _snapshot(
                        snapshot_id,
                        0,
                        False,
                        2,
                        participants=[_participant("participant-a", "partial", 2)],
                    )
                await _send(writer, {"id": request_id, "ok": True, "result": result})
            elif method == "frontend.state.page":
                snapshot_id = params["snapshot_id"]
                assert isinstance(snapshot_id, str)
                if snapshot_id == "snapshot-broken":
                    result = _snapshot(snapshot_id, 1, True, 3)
                    await _send(writer, {"id": request_id, "ok": True, "result": result})
                elif snapshot_id == "snapshot-expired":
                    await _send(
                        writer,
                        {
                            "id": request_id,
                            "ok": False,
                            "error": {"code": "snapshot_expired", "message": "expired"},
                        },
                    )
                else:
                    raise AssertionError(f"unexpected snapshot page {snapshot_id!r}")
            elif method == "frontend.state.release":
                snapshot_id = params["snapshot_id"]
                assert isinstance(snapshot_id, str)
                released.append(snapshot_id)
                if snapshot_id == "snapshot-expired":
                    await _send(
                        writer,
                        {
                            "id": request_id,
                            "ok": False,
                            "error": {"code": "snapshot_expired", "message": "already expired"},
                        },
                    )
                else:
                    await _send(
                        writer,
                        {"id": request_id, "ok": True, "result": {"released": True}},
                    )
            else:
                raise AssertionError(f"unexpected method {method!r}")

    async with _fixture_server(handler) as socket_path:
        client = FrontendClient(socket_path, client_id="sdk-follow")
        synchronizer = StateSynchronizer(client)
        initial = await synchronizer.refresh(page_size=1)
        assert initial.usage == {"input_tokens": 3}
        assert initial.participants["participant-a"].status == "idle"

        with pytest.raises(StateSynchronizationError):
            await synchronizer.refresh(page_size=1)
        assert synchronizer.projection is initial

        recovered = await synchronizer.refresh(page_size=1)
        assert recovered.cursor.sequence == 4
        assert recovered.participants["participant-a"].status == "ready"
        await client.close()

    assert released == ["snapshot-old", "snapshot-broken", "snapshot-expired", "snapshot-new"]


@pytest.mark.asyncio
async def test_follow_applies_complete_groups_and_preserves_future_events() -> None:  # noqa: PLR0915
    follow_calls = 0
    snapshots = 0

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal follow_calls, snapshots
        await _handshake(reader, writer)
        while request := await _read_request(reader):
            method = request["method"]
            request_id = request["id"]
            assert type(request_id) is int
            if method == "frontend.state.snapshot":
                snapshots += 1
                participant = _participant(
                    "participant-a", "ready" if snapshots > 1 else "idle", 2 if snapshots > 1 else 1
                )
                if snapshots > 1:
                    participant["future_projection_field"] = {"kept": True}
                sequence = 4 if snapshots > 1 else 1
                await _send(
                    writer,
                    {
                        "id": request_id,
                        "ok": True,
                        "result": _snapshot(
                            f"snapshot-{snapshots}",
                            0,
                            True,
                            sequence,
                            participants=[participant],
                            usage={"output_tokens": 5},
                        ),
                    },
                )
            elif method == "frontend.state.release":
                await _send(writer, {"id": request_id, "ok": True, "result": {"released": True}})
            elif method == "frontend.state.follow":
                follow_calls += 1
                if follow_calls == 1:
                    participant = _participant("participant-a", "ready", 2)
                    participant["future_projection_field"] = {"kept": True}
                    transaction = _transaction(
                        "transaction-a",
                        3,
                        _event("participant.updated", "participant-a", 2, participant),
                        _event(
                            "future.entity_changed",
                            "future-a",
                            1,
                            {"opaque": "kept"},
                            future_event_field="kept",
                        ),
                    )
                    result = _follow(3, transaction)
                elif follow_calls == 2:
                    result = _follow(
                        3,
                        _transaction(
                            "transaction-a",
                            3,
                            _event(
                                "participant.updated",
                                "participant-a",
                                2,
                                _participant("participant-a", "ready", 2),
                            ),
                            _event("future.entity_changed", "future-a", 1, {"opaque": "kept"}),
                        ),
                    )
                elif follow_calls == 3:
                    result = _follow(
                        4,
                        _transaction(
                            "transaction-stale-revision",
                            4,
                            _event(
                                "participant.updated",
                                "participant-a",
                                2,
                                _participant("participant-a", "idle", 2),
                            ),
                        ),
                    )
                else:
                    raise AssertionError("unexpected extra follow")
                await _send(writer, {"id": request_id, "ok": True, "result": result})
            else:
                raise AssertionError(f"unexpected method {method!r}")

    async with _fixture_server(handler) as socket_path:
        client = FrontendClient(socket_path, client_id="sdk-follow")
        synchronizer = StateSynchronizer(client)
        await synchronizer.refresh()

        followed = await synchronizer.follow_once(wait_seconds=0)
        assert followed.cursor.sequence == 3
        assert followed.participants["participant-a"].status == "ready"
        assert followed.participants["participant-a"].extra["future_projection_field"] == {
            "kept": True
        }
        assert followed.unapplied_events[0].kind == "future.entity_changed"
        assert followed.unapplied_events[0].payload == {"opaque": "kept"}
        assert followed.unapplied_events[0].extra["future_event_field"] == "kept"

        replayed = await synchronizer.follow_once(wait_seconds=0)
        assert replayed.cursor.sequence == 3
        assert len(replayed.unapplied_events) == 1

        deduplicated = await synchronizer.follow_once(wait_seconds=0)
        assert deduplicated.cursor.sequence == 4
        assert deduplicated.participants["participant-a"].status == "ready"

        fresh = await synchronizer.refresh()
        assert fresh.cursor == deduplicated.cursor
        assert {
            identifier: value.to_wire() for identifier, value in fresh.participants.items()
        } == {
            identifier: value.to_wire() for identifier, value in deduplicated.participants.items()
        }
        assert fresh.usage == deduplicated.usage
        await client.close()


@pytest.mark.asyncio
async def test_catalog_invalidations_are_coalesced_and_acknowledged() -> None:
    follows = 0

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal follows
        await _handshake(reader, writer)
        while request := await _read_request(reader):
            method = request["method"]
            request_id = request["id"]
            assert type(request_id) is int
            if method == "frontend.state.snapshot":
                await _send(
                    writer,
                    {
                        "id": request_id,
                        "ok": True,
                        "result": _snapshot("catalog-snapshot", 0, True, 0),
                    },
                )
            elif method == "frontend.state.release":
                await _send(writer, {"id": request_id, "ok": True, "result": {"released": True}})
            elif method == "frontend.state.follow":
                follows += 1
                if follows == 1:
                    result = _follow(
                        2,
                        _transaction(
                            "catalog-burst",
                            2,
                            _event(
                                "catalog.invalidated",
                                "provider-a",
                                1,
                                {"reason": "provider_online"},
                            ),
                            _event(
                                "catalog.invalidated",
                                "provider-b",
                                1,
                                {"reason": "provider_offline"},
                            ),
                        ),
                    )
                elif follows == 2:
                    result = _follow(
                        3,
                        _transaction(
                            "catalog-replay",
                            3,
                            _event(
                                "catalog.invalidated",
                                "provider-a",
                                1,
                                {"reason": "duplicate"},
                            ),
                        ),
                    )
                elif follows == 3:
                    result = _follow(
                        4,
                        _transaction(
                            "catalog-next",
                            4,
                            _event(
                                "catalog.invalidated",
                                "provider-a",
                                2,
                                {"reason": "provider_reconfigured"},
                            ),
                        ),
                    )
                else:
                    raise AssertionError("unexpected extra follow")
                await _send(writer, {"id": request_id, "ok": True, "result": result})
            else:
                raise AssertionError(f"unexpected method {method!r}")

    async with _fixture_server(handler) as socket_path:
        client = FrontendClient(socket_path, client_id="sdk-catalogs")
        synchronizer = StateSynchronizer(client)
        await synchronizer.refresh()

        burst = await synchronizer.follow_once(wait_seconds=0)
        assert burst.catalog_dirty is True
        assert burst.catalog_generation == 2
        assert burst.unapplied_events == ()
        assert synchronizer.acknowledge_catalogs(1) is False
        assert synchronizer.acknowledge_catalogs(2) is True
        assert synchronizer.projection is not None
        assert synchronizer.projection.catalog_dirty is False

        duplicate = await synchronizer.follow_once(wait_seconds=0)
        assert duplicate.catalog_generation == 2
        assert duplicate.catalog_dirty is False

        changed = await synchronizer.follow_once(wait_seconds=0)
        assert changed.catalog_generation == 3
        assert changed.catalog_dirty is True
        assert changed.unapplied_events == ()
        await client.close()


@pytest.mark.asyncio
async def test_follow_filters_terminal_entities_and_applies_complete_state_payloads() -> None:  # noqa: PLR0915
    snapshots = 0
    send_action = {
        "supported": True,
        "route_available": True,
        "admissible": True,
        "reason": None,
        "detail": None,
    }
    owner = {"kind": "participant", "participant_id": "owner-a", "revision": 2}
    terminal_route = {
        "identity": {
            "provider_id": "provider-a",
            "provider_generation": 4,
            "terminal_id": "terminal-a",
            "terminal_incarnation": "incarnation-a",
            "occupant": {"harness": "codex"},
            "process": None,
        },
        "health": "healthy",
    }
    active_usage = {
        "workspace_id": "workspace-live",
        "holder_kind": "participant",
        "holder_id": "participant-live",
        "acquired_at": 1.0,
    }

    def live_participant() -> dict[str, object]:
        return _participant(
            "participant-live",
            "idle",
            3,
            owner=owner,
            actions={"send": send_action},
            terminal_route=terminal_route,
            addressable=True,
        )

    def live_workspace() -> dict[str, object]:
        return _workspace("workspace-live", "active", usages=[active_usage])

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal snapshots
        await _handshake(reader, writer)
        while request := await _read_request(reader):
            method = request["method"]
            request_id = request["id"]
            assert type(request_id) is int
            if method == "frontend.state.snapshot":
                snapshots += 1
                if snapshots == 1:
                    result = _snapshot(
                        "snapshot-initial",
                        0,
                        True,
                        0,
                        participants=[
                            _participant("participant-live", "idle", 0),
                            _participant("participant-terminal", "idle", 0),
                        ],
                        operations=[
                            _operation(
                                "running",
                                "delivering",
                                operation_id="operation-terminal",
                            )
                        ],
                        jobs=[_job("job-terminal", "running")],
                        workspaces=[
                            _workspace("workspace-live", "active"),
                            _workspace("workspace-terminal", "active"),
                        ],
                    )
                elif snapshots == 2:
                    result = _snapshot(
                        "snapshot-fresh",
                        0,
                        True,
                        8,
                        participants=[live_participant()],
                        workspaces=[live_workspace()],
                    )
                else:
                    raise AssertionError("unexpected extra snapshot")
                await _send(writer, {"id": request_id, "ok": True, "result": result})
            elif method == "frontend.state.release":
                await _send(writer, {"id": request_id, "ok": True, "result": {"released": True}})
            elif method == "frontend.state.follow":
                transaction = _transaction(
                    "active-state-filtering",
                    8,
                    _event(
                        "participant.controls_changed",
                        "participant-live",
                        1,
                        _participant(
                            "participant-live",
                            "idle",
                            1,
                            actions={"send": send_action},
                        ),
                    ),
                    _event(
                        "participant.owner_changed",
                        "participant-live",
                        2,
                        _participant(
                            "participant-live",
                            "idle",
                            2,
                            owner=owner,
                            actions={"send": send_action},
                        ),
                    ),
                    _event(
                        "terminal.binding_changed",
                        "participant-live",
                        3,
                        live_participant(),
                    ),
                    _event(
                        "participant.updated",
                        "participant-terminal",
                        1,
                        _participant("participant-terminal", "dead", 1),
                    ),
                    _event(
                        "operation.updated",
                        "operation-terminal",
                        1,
                        _operation("succeeded", "settled", operation_id="operation-terminal"),
                    ),
                    _event("job.updated", "job-terminal", 1, _job("job-terminal", "done")),
                    _event(
                        "workspace.updated",
                        "workspace-terminal",
                        1,
                        _workspace("workspace-terminal", "removed"),
                    ),
                    _event(
                        "workspace.usage_changed",
                        "workspace-live",
                        1,
                        live_workspace(),
                    ),
                )
                await _send(
                    writer,
                    {"id": request_id, "ok": True, "result": _follow(8, transaction)},
                )
            else:
                raise AssertionError(f"unexpected method {method!r}")

    async with _fixture_server(handler) as socket_path:
        client = FrontendClient(socket_path, client_id="sdk-follow")
        synchronizer = StateSynchronizer(client)
        initial = await synchronizer.refresh()
        assert set(initial.participants) == {"participant-live", "participant-terminal"}
        assert set(initial.operations) == {"operation-terminal"}
        assert set(initial.jobs) == {"job-terminal"}
        assert set(initial.workspaces) == {"workspace-live", "workspace-terminal"}

        followed = await synchronizer.follow_once(wait_seconds=0)
        participant = followed.participants["participant-live"]
        assert set(followed.participants) == {"participant-live"}
        assert participant.actions["send"].supported is True
        assert participant.owner.participant_id == "owner-a"
        assert participant.terminal_route is not None
        assert participant.terminal_route.identity.terminal_id == "terminal-a"
        assert followed.operations == {}
        assert followed.jobs == {}
        assert set(followed.workspaces) == {"workspace-live"}
        assert followed.workspaces["workspace-live"].usages[0].holder_id == "participant-live"

        fresh = await synchronizer.refresh()
        assert fresh.cursor == followed.cursor
        assert _state_wire(fresh) == _state_wire(followed)
        await client.close()


@pytest.mark.asyncio
async def test_follow_resnapshots_for_cursor_gaps_and_public_resnapshot_signals() -> None:
    snapshots = 0
    follows = 0

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal snapshots, follows
        await _handshake(reader, writer)
        while request := await _read_request(reader):
            method = request["method"]
            request_id = request["id"]
            assert type(request_id) is int
            if method == "frontend.state.snapshot":
                snapshots += 1
                sequence = (1, 3, 4)[snapshots - 1]
                await _send(
                    writer,
                    {
                        "id": request_id,
                        "ok": True,
                        "result": _snapshot(
                            f"snapshot-{sequence}",
                            0,
                            True,
                            sequence,
                            participants=[
                                _participant("participant-a", f"state-{sequence}", sequence)
                            ],
                        ),
                    },
                )
            elif method == "frontend.state.release":
                await _send(writer, {"id": request_id, "ok": True, "result": {"released": True}})
            elif method == "frontend.state.follow":
                follows += 1
                if follows == 1:
                    gap = _transaction(
                        "gap",
                        3,
                        _event(
                            "participant.updated",
                            "participant-a",
                            2,
                            _participant("participant-a", "would-be-partial", 2),
                        ),
                    )
                    await _send(writer, {"id": request_id, "ok": True, "result": _follow(3, gap)})
                elif follows == 2:
                    await _send(
                        writer,
                        {
                            "id": request_id,
                            "ok": False,
                            "error": {
                                "code": "resnapshot_required",
                                "message": "retention or stream reset",
                                "details": {"reason": "stream_mismatch"},
                            },
                        },
                    )
                else:
                    raise AssertionError("unexpected extra follow")
            else:
                raise AssertionError(f"unexpected method {method!r}")

    async with _fixture_server(handler) as socket_path:
        client = FrontendClient(socket_path, client_id="sdk-follow")
        synchronizer = StateSynchronizer(client)
        await synchronizer.refresh()

        after_gap = await synchronizer.follow_once()
        assert after_gap.cursor.sequence == 3
        assert after_gap.participants["participant-a"].status == "state-3"

        after_signal = await synchronizer.follow_once()
        assert after_signal.cursor.sequence == 4
        assert after_signal.participants["participant-a"].status == "state-4"
        await client.close()


@pytest.mark.asyncio
async def test_disconnect_keeps_the_projection_stale_then_resnapshots_on_reconnect(  # noqa: PLR0915
) -> None:
    snapshots = 0
    follow_calls = 0
    observed_cursors: list[dict[str, object]] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal follow_calls, snapshots
        await _handshake(reader, writer)
        while request := await _read_request(reader):
            method = request["method"]
            request_id = request["id"]
            assert type(request_id) is int
            if method == "frontend.state.snapshot":
                snapshots += 1
                if snapshots == 1:
                    result = _snapshot(
                        "snapshot-before-restart",
                        0,
                        True,
                        1,
                        participants=[
                            _participant(
                                "participant-a",
                                "idle",
                                1,
                                name="before-restart",
                            )
                        ],
                        providers=[_provider("healthy")],
                    )
                elif snapshots == 2:
                    result = _snapshot(
                        "snapshot-after-restart",
                        0,
                        True,
                        1,
                        participants=[
                            _participant(
                                "participant-a",
                                "idle",
                                1,
                                name="after-restart",
                            )
                        ],
                        providers=[_provider("offline")],
                    )
                else:
                    raise AssertionError("unexpected extra snapshot")
                await _send(
                    writer,
                    {"id": request_id, "ok": True, "result": result},
                )
            elif method == "frontend.state.release":
                await _send(writer, {"id": request_id, "ok": True, "result": {"released": True}})
            elif method == "frontend.state.follow":
                params = request["params"]
                assert isinstance(params, dict)
                cursor = params["cursor"]
                assert isinstance(cursor, dict)
                observed_cursors.append(cursor)
                follow_calls += 1
                if follow_calls == 1:
                    return
                raise AssertionError("reconnect must refresh before following")
            else:
                raise AssertionError(f"unexpected method {method!r}")

    async with _fixture_server(handler) as socket_path:
        client = FrontendClient(socket_path, client_id="sdk-follow")
        synchronizer = StateSynchronizer(client)
        initial = await synchronizer.refresh()
        assert initial.participants["participant-a"].name == "before-restart"
        assert initial.providers["provider-a"].health == "healthy"

        with pytest.raises(RequestUncertain):
            await synchronizer.follow_once(wait_seconds=0)
        assert synchronizer.projection is not None
        assert synchronizer.projection.stale is True
        assert synchronizer.projection.cursor.sequence == 1
        assert synchronizer.projection.participants["participant-a"].name == "before-restart"
        assert synchronizer.projection.providers["provider-a"].health == "healthy"

        recovered = await synchronizer.follow_once(wait_seconds=0)
        assert recovered.stale is False
        assert recovered.cursor.sequence == 1
        assert recovered.participants["participant-a"].name == "after-restart"
        assert recovered.providers["provider-a"].health == "offline"
        await client.close()

    assert snapshots == 2
    assert observed_cursors == [{"stream_id": "stream-a", "sequence": 1}]


@pytest.mark.asyncio
async def test_operation_wait_cancellation_detaches_and_handle_survives_reconnect() -> None:
    first_wait_started = asyncio.Event()
    first_wait_closed = asyncio.Event()
    operation_waits = 0
    methods: list[str] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal operation_waits
        await _handshake(reader, writer)
        while request := await _read_request(reader):
            method = request["method"]
            methods.append(str(method))
            request_id = request["id"]
            assert type(request_id) is int
            if method == "frontend.operations.await":
                operation_waits += 1
                if operation_waits == 1:
                    first_wait_started.set()
                    assert await _read_request(reader) is None
                    first_wait_closed.set()
                    return
                if operation_waits == 2:
                    result = {"operation": _operation("running", "delivering"), "timed_out": True}
                else:
                    result = {"operation": _operation("succeeded", "settled"), "timed_out": False}
                await _send(writer, {"id": request_id, "ok": True, "result": result})
            elif method == "frontend.operations.get":
                await _send(
                    writer,
                    {"id": request_id, "ok": True, "result": _operation("running", "delivering")},
                )
            else:
                raise AssertionError(f"unexpected method {method!r}")

    async with _fixture_server(handler) as socket_path:
        client = FrontendClient(socket_path, client_id="sdk-follow")
        waiting = asyncio.create_task(client.operations.wait("operation-a", wait_seconds=30))
        await asyncio.wait_for(first_wait_started.wait(), timeout=1)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await asyncio.wait_for(first_wait_closed.wait(), timeout=1)

        observed = await client.operations.get("operation-a")
        assert observed.value.state == "running"
        assert observed.value.job_handle == "job-a"

        timed_out = await client.operations.wait("operation-a", wait_seconds=0)
        assert timed_out.value.timed_out is True
        assert timed_out.value.operation.state == "running"

        settled = await client.operations.wait("operation-a", wait_seconds=0)
        assert settled.value.timed_out is False
        assert settled.value.operation.state == "succeeded"
        assert settled.value.operation.job_handle == "job-a"
        await client.close()

    assert methods.count("frontend.operations.await") == 3
    assert "frontend.jobs.await" not in methods
