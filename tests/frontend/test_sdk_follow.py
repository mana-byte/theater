"""StateSynchronizer follow: complete groups, terminal filtering, batches, catalog invalidations."""

from __future__ import annotations

import asyncio

from tests.support.frontend_wire import projection_wire, read_request
from tests.support.sdk_fixture_server import fixture_server, send_frame
from tests.support.sdk_state_frames import (
    follow_result,
    job_frame,
    journal_event,
    journal_transaction,
    operation_frame,
    participant_frame,
    provider_frame,
    snapshot_page,
    state_handshake,
    workspace_frame,
)
from theater.frontend import (
    FrontendClient,
    StateSynchronizer,
)


async def test_follow_applies_complete_groups_and_preserves_future_events() -> None:  # noqa: PLR0915
    follow_calls = 0
    snapshots = 0

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal follow_calls, snapshots
        await state_handshake(reader, writer)
        while request := await read_request(reader):
            method = request["method"]
            request_id = request["id"]
            assert type(request_id) is int
            if method == "frontend.state.snapshot":
                snapshots += 1
                participant = participant_frame(
                    "participant-a", "ready" if snapshots > 1 else "idle", 2 if snapshots > 1 else 1
                )
                if snapshots > 1:
                    participant["future_projection_field"] = {"kept": True}
                sequence = 4 if snapshots > 1 else 1
                await send_frame(
                    writer,
                    {
                        "id": request_id,
                        "ok": True,
                        "result": snapshot_page(
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
                await send_frame(
                    writer, {"id": request_id, "ok": True, "result": {"released": True}}
                )
            elif method == "frontend.state.follow":
                follow_calls += 1
                if follow_calls == 1:
                    participant = participant_frame("participant-a", "ready", 2)
                    participant["future_projection_field"] = {"kept": True}
                    transaction = journal_transaction(
                        "transaction-a",
                        3,
                        journal_event("participant.updated", "participant-a", 2, participant),
                        journal_event(
                            "future.entity_changed",
                            "future-a",
                            1,
                            {"opaque": "kept"},
                            future_event_field="kept",
                        ),
                    )
                    result = follow_result(3, transaction)
                elif follow_calls == 2:
                    result = follow_result(
                        3,
                        journal_transaction(
                            "transaction-a",
                            3,
                            journal_event(
                                "participant.updated",
                                "participant-a",
                                2,
                                participant_frame("participant-a", "ready", 2),
                            ),
                            journal_event(
                                "future.entity_changed", "future-a", 1, {"opaque": "kept"}
                            ),
                        ),
                    )
                elif follow_calls == 3:
                    result = follow_result(
                        4,
                        journal_transaction(
                            "transaction-stale-revision",
                            4,
                            journal_event(
                                "participant.updated",
                                "participant-a",
                                2,
                                participant_frame("participant-a", "idle", 2),
                            ),
                        ),
                    )
                else:
                    raise AssertionError("unexpected extra follow")
                await send_frame(writer, {"id": request_id, "ok": True, "result": result})
            else:
                raise AssertionError(f"unexpected method {method!r}")

    async with fixture_server(handler) as socket_path:
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


async def test_catalog_invalidations_are_coalesced_and_acknowledged() -> None:
    follows = 0

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal follows
        await state_handshake(reader, writer)
        while request := await read_request(reader):
            method = request["method"]
            request_id = request["id"]
            assert type(request_id) is int
            if method == "frontend.state.snapshot":
                await send_frame(
                    writer,
                    {
                        "id": request_id,
                        "ok": True,
                        "result": snapshot_page("catalog-snapshot", 0, True, 0),
                    },
                )
            elif method == "frontend.state.release":
                await send_frame(
                    writer, {"id": request_id, "ok": True, "result": {"released": True}}
                )
            elif method == "frontend.state.follow":
                follows += 1
                if follows == 1:
                    result = follow_result(
                        2,
                        journal_transaction(
                            "catalog-burst",
                            2,
                            journal_event(
                                "catalog.invalidated",
                                "provider-a",
                                1,
                                {"reason": "provider_online"},
                            ),
                            journal_event(
                                "catalog.invalidated",
                                "provider-b",
                                1,
                                {"reason": "provider_offline"},
                            ),
                        ),
                    )
                elif follows == 2:
                    result = follow_result(
                        3,
                        journal_transaction(
                            "catalog-replay",
                            3,
                            journal_event(
                                "catalog.invalidated",
                                "provider-a",
                                1,
                                {"reason": "duplicate"},
                            ),
                        ),
                    )
                elif follows == 3:
                    result = follow_result(
                        4,
                        journal_transaction(
                            "catalog-next",
                            4,
                            journal_event(
                                "catalog.invalidated",
                                "provider-a",
                                2,
                                {"reason": "provider_reconfigured"},
                            ),
                        ),
                    )
                else:
                    raise AssertionError("unexpected extra follow")
                await send_frame(writer, {"id": request_id, "ok": True, "result": result})
            else:
                raise AssertionError(f"unexpected method {method!r}")

    async with fixture_server(handler) as socket_path:
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
        return participant_frame(
            "participant-live",
            "idle",
            3,
            owner=owner,
            actions={"send": send_action},
            terminal_route=terminal_route,
            addressable=True,
        )

    def live_workspace() -> dict[str, object]:
        return workspace_frame("workspace-live", "active", usages=[active_usage])

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal snapshots
        await state_handshake(reader, writer)
        while request := await read_request(reader):
            method = request["method"]
            request_id = request["id"]
            assert type(request_id) is int
            if method == "frontend.state.snapshot":
                snapshots += 1
                if snapshots == 1:
                    result = snapshot_page(
                        "snapshot-initial",
                        0,
                        True,
                        0,
                        participants=[
                            participant_frame("participant-live", "idle", 0),
                            participant_frame("participant-terminal", "idle", 0),
                        ],
                        operations=[
                            operation_frame(
                                "running",
                                "delivering",
                                operation_id="operation-terminal",
                            )
                        ],
                        jobs=[job_frame("job-terminal", "running")],
                        workspaces=[
                            workspace_frame("workspace-live", "active"),
                            workspace_frame("workspace-terminal", "active"),
                        ],
                    )
                elif snapshots == 2:
                    result = snapshot_page(
                        "snapshot-fresh",
                        0,
                        True,
                        8,
                        participants=[live_participant()],
                        workspaces=[live_workspace()],
                    )
                else:
                    raise AssertionError("unexpected extra snapshot")
                await send_frame(writer, {"id": request_id, "ok": True, "result": result})
            elif method == "frontend.state.release":
                await send_frame(
                    writer, {"id": request_id, "ok": True, "result": {"released": True}}
                )
            elif method == "frontend.state.follow":
                transaction = journal_transaction(
                    "active-state-filtering",
                    8,
                    journal_event(
                        "participant.controls_changed",
                        "participant-live",
                        1,
                        participant_frame(
                            "participant-live",
                            "idle",
                            1,
                            actions={"send": send_action},
                        ),
                    ),
                    journal_event(
                        "participant.owner_changed",
                        "participant-live",
                        2,
                        participant_frame(
                            "participant-live",
                            "idle",
                            2,
                            owner=owner,
                            actions={"send": send_action},
                        ),
                    ),
                    journal_event(
                        "terminal.binding_changed",
                        "participant-live",
                        3,
                        live_participant(),
                    ),
                    journal_event(
                        "participant.updated",
                        "participant-terminal",
                        1,
                        participant_frame("participant-terminal", "dead", 1),
                    ),
                    journal_event(
                        "operation.updated",
                        "operation-terminal",
                        1,
                        operation_frame("succeeded", "settled", operation_id="operation-terminal"),
                    ),
                    journal_event(
                        "job.updated", "job-terminal", 1, job_frame("job-terminal", "done")
                    ),
                    journal_event(
                        "workspace.updated",
                        "workspace-terminal",
                        1,
                        workspace_frame("workspace-terminal", "removed"),
                    ),
                    journal_event(
                        "workspace.usage_changed",
                        "workspace-live",
                        1,
                        live_workspace(),
                    ),
                )
                await send_frame(
                    writer,
                    {"id": request_id, "ok": True, "result": follow_result(8, transaction)},
                )
            else:
                raise AssertionError(f"unexpected method {method!r}")

    async with fixture_server(handler) as socket_path:
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
        assert projection_wire(fresh) == projection_wire(followed)
        await client.close()


async def test_follow_batch_matches_separate_follows_and_mid_batch_gap() -> None:  # noqa: PLR0915
    snapshots = 0
    follows = 0

    def tx_a() -> dict[str, object]:
        return journal_transaction(
            "tx-a",
            2,
            journal_event(
                "participant.updated",
                "participant-a",
                2,
                participant_frame("participant-a", "ready", 2),
            ),
            journal_event("catalog.invalidated", "provider-a", 1, {"reason": "provider_online"}),
        )

    def tx_b() -> dict[str, object]:
        return journal_transaction(
            "tx-b",
            5,
            journal_event(
                "participant.updated",
                "participant-a",
                3,
                participant_frame("participant-a", "running", 3),
            ),
            journal_event(
                "participant.removed", "participant-b", 2, {"participant_id": "participant-b"}
            ),
            journal_event("job.updated", "job-a", 1, job_frame("job-a", "done")),
        )

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal snapshots, follows
        await state_handshake(reader, writer)
        while request := await read_request(reader):
            method = request["method"]
            request_id = request["id"]
            assert type(request_id) is int
            if method == "frontend.state.snapshot":
                snapshots += 1
                if snapshots <= 2:
                    result = snapshot_page(
                        "snapshot-base",
                        0,
                        True,
                        0,
                        participants=[
                            participant_frame("participant-a", "idle", 1),
                            participant_frame("participant-b", "idle", 1),
                        ],
                        jobs=[job_frame("job-a", "running")],
                        providers=[provider_frame("healthy")],
                        usage={"input_tokens": 7},
                    )
                else:
                    result = snapshot_page(
                        "snapshot-after-gap",
                        0,
                        True,
                        6,
                        participants=[participant_frame("participant-a", "fresh-after-gap", 5)],
                        usage={"input_tokens": 7},
                    )
                await send_frame(writer, {"id": request_id, "ok": True, "result": result})
            elif method == "frontend.state.release":
                await send_frame(
                    writer, {"id": request_id, "ok": True, "result": {"released": True}}
                )
            elif method == "frontend.state.follow":
                follows += 1
                if follows == 1:
                    result = follow_result(5, tx_a(), tx_b())
                elif follows == 2:
                    result = follow_result(2, tx_a())
                elif follows == 3:
                    result = follow_result(5, tx_b())
                elif follows == 4:
                    result = follow_result(
                        9,
                        journal_transaction(
                            "tx-c",
                            6,
                            journal_event(
                                "participant.updated",
                                "participant-a",
                                4,
                                participant_frame("participant-a", "would-be-partial", 4),
                            ),
                        ),
                        journal_transaction(
                            "tx-d",
                            9,
                            journal_event(
                                "participant.updated",
                                "participant-a",
                                5,
                                participant_frame("participant-a", "never-applied", 5),
                            ),
                        ),
                    )
                else:
                    raise AssertionError("unexpected extra follow")
                await send_frame(writer, {"id": request_id, "ok": True, "result": result})
            else:
                raise AssertionError(f"unexpected method {method!r}")

    async with fixture_server(handler) as socket_path:
        client = FrontendClient(socket_path, client_id="sdk-follow")
        batched_sync = StateSynchronizer(client)
        await batched_sync.refresh()
        batched = await batched_sync.follow_once(wait_seconds=0)

        individual_sync = StateSynchronizer(client)
        await individual_sync.refresh()
        await individual_sync.follow_once(wait_seconds=0)
        individual = await individual_sync.follow_once(wait_seconds=0)

        assert batched.cursor.sequence == 5
        assert set(batched.participants) == {"participant-a"}
        assert batched.participants["participant-a"].status == "running"
        assert batched.jobs == {}
        assert batched.providers["provider-a"].health == "healthy"
        assert batched.catalog_dirty is True
        assert batched.catalog_generation == 1
        assert individual.cursor == batched.cursor
        assert projection_wire(individual) == projection_wire(batched)
        assert individual.entity_revisions == batched.entity_revisions
        assert individual.unapplied_events == batched.unapplied_events
        assert individual.catalog_dirty == batched.catalog_dirty
        assert individual.catalog_generation == batched.catalog_generation
        assert individual.usage == batched.usage

        resnapshotted = await batched_sync.follow_once(wait_seconds=0)
        assert snapshots == 3
        assert resnapshotted.cursor.sequence == 6
        assert resnapshotted.participants["participant-a"].status == "fresh-after-gap"
        assert batched_sync.projection is resnapshotted
        await client.close()
