from __future__ import annotations

import stat
from pathlib import Path

import pytest
from regie.bridge.state import BridgeAlreadyRunning, BridgeStateStore


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
