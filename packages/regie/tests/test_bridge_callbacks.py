from __future__ import annotations

import hashlib
from pathlib import Path

from regie.bridge.callbacks import TmuxProviderCallbacks
from regie.bridge.state import BridgeStateStore
from regie.tmux.identity import PaneSnapshot
from regie.tmux.presence import PresenceEvidence

from theater.frontend import CallbackRequest, CallbackResponse
from theater.frontend.schemas import validate_callback_response


def _request(method: str, generation: int = 3) -> CallbackRequest:
    params: dict[str, object] = {
        "operation_id": "operation-a",
        "provider_generation": generation,
        "participant_id": "participant-a",
        "terminal_id": "%7",
        "terminal_incarnation": "incarnation-a",
        "expected_occupant": "participant-a",
        "require_absent": True,
    }
    if method == "terminal.create":
        params = {
            "operation_id": "operation-a",
            "provider_generation": generation,
            "participant_id": "participant-a",
            "launch_id": "launch-a",
            "launch": {
                "executable": "/bin/agent",
                "argv": ["/bin/agent", "--safe"],
                "cwd": "/tmp",
                "environment": {},
            },
        }
    elif method == "terminal.deliver":
        params["action"] = {"kind": "submit_text", "text": "hello"}
    return CallbackRequest(
        callback_id="callback-a",
        method=method,
        params=params,
        provider_generation=generation,
    )


def _identity(generation: int = 3) -> dict[str, object]:
    return {
        "provider_id": "provider-a",
        "provider_generation": generation,
        "terminal_id": "%7",
        "terminal_incarnation": "incarnation-a",
        "occupant": {
            "occupant_id": "participant-a",
            "provider_kind": "tmux",
            "tmux_server_identity": "server-a",
            "terminal_incarnation": "incarnation-a",
            "pane_pid": 42,
        },
        "process": {"pid": 42, "executable": "/bin/agent"},
        "launch_id": "launch-a",
    }


async def test_create_receipt_is_durable_and_same_generation_is_not_replayed(
    tmp_path: Path, monkeypatch
) -> None:
    state = BridgeStateStore(tmp_path / "bridge")
    state.acquire()
    state.update(provider_id="provider-a", tmux_server_identity="server-a")
    generation = 3
    callbacks = TmuxProviderCallbacks(state, generation=lambda: generation)
    calls = 0

    async def create(**_kwargs):
        nonlocal calls
        calls += 1
        return _identity()

    monkeypatch.setattr("regie.bridge.callbacks.create_terminal", create)
    try:
        first = await callbacks.create(_request("terminal.create"))
        second = await callbacks.create(_request("terminal.create"))
        assert first == second
        assert calls == 1
        assert state.receipts()[0]["launch_id"] == "launch-a"
        validate_callback_response(
            "terminal.create", {"type": "response", "id": "callback-a", "result": first}
        )
    finally:
        state.release()


async def test_generation_and_presence_fences_prevent_terminal_delivery(
    tmp_path: Path, monkeypatch
) -> None:
    state = BridgeStateStore(tmp_path / "bridge")
    state.acquire()
    state.update(provider_id="provider-a", tmux_server_identity="server-a")
    generation = 3
    callbacks = TmuxProviderCallbacks(state, generation=lambda: generation)
    snapshot = PaneSnapshot(
        server_identity="server-a",
        pane_id="%7",
        pane_pid=42,
        dead=False,
        executable="agent",
        window_id="@1",
        provider_id="provider-a",
        terminal_incarnation="incarnation-a",
        occupant_id="participant-a",
        occupant_digest=hashlib.sha256(b"participant-a").hexdigest(),
        occupant_pane_pid=42,
        launch_id="launch-a",
        launch_executable="/bin/agent",
    )
    deliveries = 0

    async def snapshot_for(_pane: str):
        return snapshot

    async def inspect(**_kwargs):
        return _identity(), PresenceEvidence("present", "focused_viewer", None), None, True

    async def deliver(_pane: str, _action) -> None:
        nonlocal deliveries
        deliveries += 1

    monkeypatch.setattr("regie.bridge.callbacks.pane_snapshot", snapshot_for)
    monkeypatch.setattr("regie.bridge.callbacks.inspect_terminal", inspect)
    monkeypatch.setattr("regie.bridge.callbacks.deliver_action", deliver)
    try:
        blocked = await callbacks.deliver(_request("terminal.deliver"))
        assert isinstance(blocked, CallbackResponse)
        assert blocked.error is not None and blocked.error["code"] == "human_present"
        assert deliveries == 0

        generation = 4
        stale = await callbacks.deliver(_request("terminal.deliver", generation=3))
        assert isinstance(stale, CallbackResponse)
        assert stale.error is not None and stale.error["code"] == "stale_generation"
        assert deliveries == 0

        state.write_receipt(
            "terminal.terminate",
            "operation-a",
            {
                "operation_id": "operation-a",
                "provider_generation": 2,
                "terminal_id": "%7",
                "terminal_incarnation": "incarnation-a",
                "delivery": "accepted",
                "exit_confirmed": True,
            },
        )
        historical = await callbacks.terminate(_request("terminal.terminate", generation=4))
        assert isinstance(historical, dict)
        assert historical["delivery"] == "unknown"
        validate_callback_response(
            "terminal.terminate",
            {"type": "response", "id": "callback-a", "result": historical},
        )
    finally:
        state.release()


async def test_inventory_is_bounded_and_reports_only_a_complete_first_page(
    tmp_path: Path, monkeypatch
) -> None:
    state = BridgeStateStore(tmp_path / "bridge")
    state.acquire()
    state.update(provider_id="provider-a", tmux_server_identity="server-a")
    callbacks = TmuxProviderCallbacks(state, generation=lambda: 3)
    terminals = []
    for index in range(3):
        terminal = _identity()
        terminal["terminal_id"] = f"%{index}"
        terminals.append(terminal)

    async def inventory(**_kwargs):
        return tuple(terminals)

    monkeypatch.setattr("regie.bridge.callbacks.managed_inventory", inventory)
    try:
        first_request = _request("terminal.inventory")
        first_request = CallbackRequest(
            callback_id=first_request.callback_id,
            method=first_request.method,
            params={"provider_generation": 3, "limit": 2},
            provider_generation=3,
        )
        first = await callbacks.inventory(first_request)
        assert not isinstance(first, CallbackResponse)
        assert first["complete"] is False
        assert first["next_cursor"] == "%1"
        assert len(first["terminals"]) == 2
        validate_callback_response(
            "terminal.inventory", {"type": "response", "id": "callback-a", "result": first}
        )

        second_request = CallbackRequest(
            callback_id="callback-b",
            method="terminal.inventory",
            params={"provider_generation": 3, "limit": 2, "cursor": "%1"},
            provider_generation=3,
        )
        second = await callbacks.inventory(second_request)
        assert not isinstance(second, CallbackResponse)
        assert second["complete"] is False
        assert second["next_cursor"] is None
        assert [terminal["terminal_id"] for terminal in second["terminals"]] == ["%2"]
    finally:
        state.release()
