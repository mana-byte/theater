from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from regie.state import StateController

from theater.frontend import FrontendClient, RequestUncertain


def _participant(name: str) -> dict[str, object]:
    return {
        "participant_id": "participant-a",
        "origin": "spawned",
        "harness": "codex",
        "status": "idle",
        "owner": {"kind": "local_operator", "revision": 1},
        "name": name,
        "addressable": False,
        "presence": "unknown",
        "actions": {},
        "future_display_field": {"preserved": True},
    }


def _snapshot(snapshot_id: str, name: str) -> dict[str, object]:
    return {
        "snapshot_id": snapshot_id,
        "page": 0,
        "complete": True,
        "ending_cursor": {"stream_id": "stream-a", "sequence": 1},
        "participants": [_participant(name)],
        "operations": [],
        "jobs": [],
        "providers": [],
        "workspaces": [],
    }


async def _read(reader: asyncio.StreamReader) -> dict[str, object] | None:
    frame = await reader.readline()
    if not frame:
        return None
    value = json.loads(frame)
    assert isinstance(value, dict)
    return value


async def _send(writer: asyncio.StreamWriter, value: dict[str, object]) -> None:
    writer.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
    await writer.drain()


@asynccontextmanager
async def _server(
    methods: list[str], *, follow_started: asyncio.Event | None = None
) -> AsyncIterator[Path]:
    snapshots = 0
    follows = 0

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal follows, snapshots
        try:
            while request := await _read(reader):
                method = request["method"]
                request_id = request["id"]
                assert isinstance(method, str)
                assert type(request_id) is int
                methods.append(method)
                if method == "frontend.handshake":
                    result = {
                        "api": {"major": 1, "minor": 0},
                        "daemon_instance_id": "fixture",
                        "package_version": "1.0.0rc10",
                        "capabilities": ["orchestration.v1", "state.follow.v1"],
                        "limits": {"max_frame_bytes": 67_108_864, "max_in_flight": 1},
                    }
                elif method == "frontend.state.snapshot":
                    snapshots += 1
                    result = _snapshot(
                        "before-restart" if snapshots == 1 else "after-restart",
                        "before-restart" if snapshots == 1 else "after-restart",
                    )
                elif method == "frontend.state.release":
                    result = {"released": True}
                elif method == "frontend.state.follow":
                    follows += 1
                    if follow_started is not None:
                        assert request["params"]["wait_seconds"] == 30
                        follow_started.set()
                        await reader.read()
                    else:
                        assert follows == 1
                    return
                else:
                    raise AssertionError(f"unexpected public method {method!r}")
                await _send(writer, {"id": request_id, "ok": True, "result": result})
        finally:
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()

    root = Path(tempfile.mkdtemp(prefix="regie-state-", dir="/tmp"))
    path = root / "frontend.sock"
    server = await asyncio.start_unix_server(handler, path=path)
    try:
        yield path
    finally:
        server.close()
        await server.wait_closed()
        path.unlink(missing_ok=True)
        shutil.rmtree(root)


@pytest.mark.asyncio
async def test_state_controller_keeps_stale_display_then_resnapshots_over_public_sdk() -> None:
    methods: list[str] = []
    async with _server(methods) as socket_path:
        client = FrontendClient(socket_path, client_id="regie-test")
        controller = StateController(client)
        initial = await controller.initialize()
        assert initial.participants["participant-a"].name == "before-restart"
        assert initial.participants["participant-a"].extra["future_display_field"] == {
            "preserved": True
        }

        with pytest.raises(RequestUncertain):
            await controller.synchronize()
        stale = controller.projection
        assert stale is not None and stale.stale is True
        assert stale.participants["participant-a"].name == "before-restart"

        refreshed = await controller.synchronize()
        assert refreshed.stale is False
        assert refreshed.cursor.sequence == 1
        assert refreshed.participants["participant-a"].name == "after-restart"
        await client.close()

    assert "frontend.participants.tree" not in methods
    assert methods.count("frontend.state.snapshot") == 2
    assert methods.count("frontend.state.follow") == 1


async def test_waiting_follow_yields_to_refresh_but_propagates_owner_cancellation():
    methods = []
    started = asyncio.Event()
    async with _server(methods, follow_started=started) as socket_path:
        client = FrontendClient(socket_path, client_id="regie-follow")
        controller = StateController(client)
        pending = None
        try:
            await controller.initialize()
            pending = asyncio.create_task(controller.follow())
            async with asyncio.timeout(2):
                await started.wait()
                fresh = await controller.initialize()
                assert await pending is None
            assert fresh.participants["participant-a"].name == "after-restart"
            assert controller.projection is fresh and not fresh.stale

            started.clear()
            pending = asyncio.create_task(controller.follow())
            async with asyncio.timeout(2):
                await started.wait()
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            assert controller.projection.stale
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await client.close()
