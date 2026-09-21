from __future__ import annotations

import asyncio
import stat
import threading
from pathlib import Path

import pytest
from regie.bridge.persistence import BridgePersistence
from regie.bridge.state import BridgeAlreadyRunning, BridgeStateStore


async def test_persistence_cancellation_drains_io_before_releasing_its_fence():
    persistence = BridgePersistence()
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    operations = []

    def write():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        operations.append("write")

    first = asyncio.create_task(persistence.run(write))
    try:
        await started.wait()
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        second = asyncio.create_task(persistence.run(operations.append, "next"))
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        assert operations == ["write", "next"]
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)


def test_bridge_state_is_private_durable_and_exclusively_locked(tmp_path: Path) -> None:
    root = tmp_path / "bridge"
    first = BridgeStateStore(root)
    state = first.acquire()
    try:
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(first.state_path.stat().st_mode) == 0o600
        assert "provider_credential" not in repr(state)

        first.update(provider_id="provider-a", tmux_server_identity="server-a")
        first.write_receipt(
            "terminal.deliver",
            "operation-a",
            {"operation_id": "operation-a", "delivery": "accepted"},
        )
        assert first.receipts()[0]["delivery"] == "accepted"

        with pytest.raises(BridgeAlreadyRunning):
            BridgeStateStore(root).acquire()
    finally:
        first.release()

    reopened = BridgeStateStore(root)
    persisted = reopened.acquire()
    try:
        assert persisted.provider_id == "provider-a"
        assert persisted.provider_credential == state.provider_credential
        assert stat.S_IMODE(next(reopened.receipt_dir.iterdir()).stat().st_mode) == 0o600
        reopened.clear_receipts()
        assert reopened.receipts() == ()
    finally:
        reopened.release()
