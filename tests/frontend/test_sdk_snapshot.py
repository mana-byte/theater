"""StateSynchronizer snapshot assembly, cursor resnapshots and reconnect recovery."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.frontend_wire import read_request
from tests.support.sdk_fixture_server import fixture_server, send_frame
from tests.support.sdk_state_frames import (
    follow_result,
    journal_event,
    journal_transaction,
    participant_frame,
    provider_frame,
    snapshot_page,
    state_handshake,
)
from theater.frontend import (
    FrontendClient,
    RequestUncertain,
    StateSynchronizationError,
    StateSynchronizer,
)


async def test_snapshot_assembly_is_atomic_and_recovers_from_expiry() -> None:  # noqa: PLR0915
    snapshots = iter(("snapshot-old", "snapshot-broken", "snapshot-expired", "snapshot-new"))
    released: list[str] = []

    async def handler(  # noqa: PLR0912
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await state_handshake(reader, writer)
        while request := await read_request(reader):
            method = request["method"]
            request_id = request["id"]
            assert type(request_id) is int
            params = request["params"]
            assert isinstance(params, dict)
            if method == "frontend.state.snapshot":
                snapshot_id = next(snapshots)
                if snapshot_id == "snapshot-old":
                    result = snapshot_page(
                        snapshot_id,
                        0,
                        True,
                        1,
                        participants=[participant_frame("participant-a", "idle", 1)],
                        usage={"input_tokens": 3},
                    )
                elif snapshot_id == "snapshot-new":
                    result = snapshot_page(
                        snapshot_id,
                        0,
                        True,
                        4,
                        participants=[participant_frame("participant-a", "ready", 4)],
                    )
                else:
                    result = snapshot_page(
                        snapshot_id,
                        0,
                        False,
                        2,
                        participants=[participant_frame("participant-a", "partial", 2)],
                    )
                await send_frame(writer, {"id": request_id, "ok": True, "result": result})
            elif method == "frontend.state.page":
                snapshot_id = params["snapshot_id"]
                assert isinstance(snapshot_id, str)
                if snapshot_id == "snapshot-broken":
                    result = snapshot_page(snapshot_id, 1, True, 3)
                    await send_frame(writer, {"id": request_id, "ok": True, "result": result})
                elif snapshot_id == "snapshot-expired":
                    await send_frame(
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
                    await send_frame(
                        writer,
                        {
                            "id": request_id,
                            "ok": False,
                            "error": {"code": "snapshot_expired", "message": "already expired"},
                        },
                    )
                else:
                    await send_frame(
                        writer,
                        {"id": request_id, "ok": True, "result": {"released": True}},
                    )
            else:
                raise AssertionError(f"unexpected method {method!r}")

    async with fixture_server(handler) as socket_path:
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


async def test_follow_resnapshots_for_cursor_gaps_and_public_resnapshot_signals() -> None:
    snapshots = 0
    follows = 0

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal snapshots, follows
        await state_handshake(reader, writer)
        while request := await read_request(reader):
            method = request["method"]
            request_id = request["id"]
            assert type(request_id) is int
            if method == "frontend.state.snapshot":
                snapshots += 1
                sequence = (1, 3, 4)[snapshots - 1]
                await send_frame(
                    writer,
                    {
                        "id": request_id,
                        "ok": True,
                        "result": snapshot_page(
                            f"snapshot-{sequence}",
                            0,
                            True,
                            sequence,
                            participants=[
                                participant_frame("participant-a", f"state-{sequence}", sequence)
                            ],
                        ),
                    },
                )
            elif method == "frontend.state.release":
                await send_frame(
                    writer, {"id": request_id, "ok": True, "result": {"released": True}}
                )
            elif method == "frontend.state.follow":
                follows += 1
                if follows == 1:
                    gap = journal_transaction(
                        "gap",
                        3,
                        journal_event(
                            "participant.updated",
                            "participant-a",
                            2,
                            participant_frame("participant-a", "would-be-partial", 2),
                        ),
                    )
                    await send_frame(
                        writer, {"id": request_id, "ok": True, "result": follow_result(3, gap)}
                    )
                elif follows == 2:
                    await send_frame(
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

    async with fixture_server(handler) as socket_path:
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


async def test_disconnect_keeps_the_projection_stale_then_resnapshots_on_reconnect(  # noqa: PLR0915
) -> None:
    snapshots = 0
    follow_calls = 0
    observed_cursors: list[dict[str, object]] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal follow_calls, snapshots
        await state_handshake(reader, writer)
        while request := await read_request(reader):
            method = request["method"]
            request_id = request["id"]
            assert type(request_id) is int
            if method == "frontend.state.snapshot":
                snapshots += 1
                if snapshots == 1:
                    result = snapshot_page(
                        "snapshot-before-restart",
                        0,
                        True,
                        1,
                        participants=[
                            participant_frame(
                                "participant-a",
                                "idle",
                                1,
                                name="before-restart",
                            )
                        ],
                        providers=[provider_frame("healthy")],
                    )
                elif snapshots == 2:
                    result = snapshot_page(
                        "snapshot-after-restart",
                        0,
                        True,
                        1,
                        participants=[
                            participant_frame(
                                "participant-a",
                                "idle",
                                1,
                                name="after-restart",
                            )
                        ],
                        providers=[provider_frame("offline")],
                    )
                else:
                    raise AssertionError("unexpected extra snapshot")
                await send_frame(
                    writer,
                    {"id": request_id, "ok": True, "result": result},
                )
            elif method == "frontend.state.release":
                await send_frame(
                    writer, {"id": request_id, "ok": True, "result": {"released": True}}
                )
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

    async with fixture_server(handler) as socket_path:
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
