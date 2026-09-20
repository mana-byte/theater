from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from regie.bridge.callbacks import TmuxProviderCallbacks
from regie.bridge.runtime import TmuxBridge
from regie.bridge.state import BridgeStateStore
from regie.contracts import BridgeConfig
from regie.tmux.identity import PaneSnapshot
from regie.tmux.presence import PresenceEvidence
from regie.tmux.terminals import managed_inventory

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


def _pane() -> PaneSnapshot:
    return PaneSnapshot(
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


async def test_create_receipt_is_durable_and_same_generation_is_not_replayed(
    tmp_path: Path, monkeypatch
) -> None:
    state = BridgeStateStore(tmp_path / "bridge")
    state.acquire()
    state.update(provider_id="provider-a", tmux_server_identity="server-a")
    generation = 3
    callbacks = TmuxProviderCallbacks(
        state, generation_usable=lambda requested: requested == generation
    )
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
    usable = True
    callbacks = TmuxProviderCallbacks(
        state,
        generation_usable=lambda requested: usable and requested == generation,
    )
    snapshot = _pane()
    deliveries = 0

    async def snapshot_for(_pane: str):
        return snapshot

    async def inspect(**_kwargs):
        return _identity(), PresenceEvidence("present", "focused_viewer", None), None, True

    async def deliver(_pane: str, _action, *, before_effect) -> None:
        nonlocal deliveries
        before_effect()
        deliveries += 1

    monkeypatch.setattr("regie.bridge.callbacks.pane_snapshot", snapshot_for)
    monkeypatch.setattr("regie.bridge.callbacks.inspect_terminal", inspect)
    monkeypatch.setattr("regie.bridge.callbacks.deliver_action", deliver)
    try:
        mismatched = _request("terminal.deliver")
        mismatched = CallbackRequest(
            callback_id=mismatched.callback_id,
            method=mismatched.method,
            params={**mismatched.params, "participant_id": "participant-b"},
            provider_generation=mismatched.provider_generation,
        )
        refused = await callbacks.deliver(mismatched)
        assert isinstance(refused, CallbackResponse)
        assert refused.error is not None and refused.error["code"] == "stale_terminal"
        assert deliveries == 0

        blocked = await callbacks.deliver(_request("terminal.deliver"))
        assert isinstance(blocked, CallbackResponse)
        assert blocked.error is not None and blocked.error["code"] == "human_present"
        assert deliveries == 0

        async def expire_during_inspection(**_kwargs):
            nonlocal usable
            usable = False
            return _identity(), PresenceEvidence("absent", "no_viewer", None), None, True

        monkeypatch.setattr("regie.bridge.callbacks.inspect_terminal", expire_during_inspection)
        expired = await callbacks.deliver(_request("terminal.deliver"))
        assert isinstance(expired, CallbackResponse)
        assert expired.error is not None and expired.error["code"] == "stale_generation"
        assert deliveries == 0

        usable = True
        monkeypatch.setattr("regie.bridge.callbacks.inspect_terminal", inspect)
        generation = 4
        stale = await callbacks.deliver(_request("terminal.deliver", generation=3))
        assert isinstance(stale, CallbackResponse)
        assert stale.error is not None and stale.error["code"] == "stale_generation"
        assert deliveries == 0
    finally:
        state.release()


async def test_post_effect_disconnect_keeps_receipt_without_replay(
    tmp_path: Path, monkeypatch
) -> None:
    state = BridgeStateStore(tmp_path / "bridge")
    state.acquire()
    state.update(provider_id="provider-a", tmux_server_identity="server-a")
    generation = 3
    usable = True
    callbacks = TmuxProviderCallbacks(
        state,
        generation_usable=lambda requested: usable and requested == generation,
    )

    async def snapshot_for(_pane_id: str):
        return _pane()

    async def absent(**_kwargs):
        return _identity(), PresenceEvidence("absent", "no_viewer", None), None, True

    deliveries = 0

    async def deliver_then_disconnect(_pane: str, _action, *, before_effect) -> None:
        nonlocal deliveries, usable
        before_effect()
        deliveries += 1
        usable = False

    monkeypatch.setattr("regie.bridge.callbacks.pane_snapshot", snapshot_for)
    monkeypatch.setattr("regie.bridge.callbacks.inspect_terminal", absent)
    monkeypatch.setattr("regie.bridge.callbacks.deliver_action", deliver_then_disconnect)
    try:
        delivered = await callbacks.deliver(_request("terminal.deliver"))
        assert not isinstance(delivered, CallbackResponse)
        assert delivered["delivery"] == "accepted"
        assert state.receipt("terminal.deliver", "operation-a") == delivered
        assert deliveries == 1

        usable = True
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
        generation = 4
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
    callbacks = TmuxProviderCallbacks(state, generation_usable=lambda requested: requested == 3)
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


async def test_create_recovers_the_unmarked_launch_without_executing_twice(  # noqa: PLR0915
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "bridge"
    pane: PaneSnapshot | None = None
    workload_starts = 0
    marker_attempts = 0

    async def inventory():
        return () if pane is None else (pane,)

    async def snapshot(_pane_id: str):
        return pane

    async def run(*args: str, **_kwargs):
        nonlocal pane, workload_starts
        if args[0] == "list-panes":
            return "" if pane is None else pane.pane_id
        if args[0] == "show-options":
            return ""
        if args[0] == "list-sessions":
            return "theater"
        if args[0] == "new-window":
            workload_starts += 1
            pane = PaneSnapshot(
                server_identity="server-a",
                pane_id="%7",
                pane_pid=42,
                dead=False,
                executable="agent",
                window_id="@1",
                provider_id=None,
                terminal_incarnation=None,
                occupant_id=None,
                occupant_digest=None,
                occupant_pane_pid=None,
                launch_id=None,
                launch_executable=None,
            )
            return "%7"
        assert args[0] == "rename-window"
        return ""

    async def mark(_pane_id: str, **facts) -> None:
        nonlocal pane, marker_attempts
        marker_attempts += 1
        if marker_attempts == 1:
            raise RuntimeError("simulated bridge loss before marker commit")
        assert pane is not None
        pane = replace(
            pane,
            provider_id=str(facts["provider_id"]),
            terminal_incarnation=str(facts["terminal_incarnation"]),
            occupant_id=str(facts["occupant_id"]),
            occupant_digest=hashlib.sha256(str(facts["occupant_id"]).encode()).hexdigest(),
            occupant_pane_pid=int(facts["pane_pid"]),
            launch_id=str(facts["launch_id"]),
            launch_executable=str(facts["executable"]),
        )

    monkeypatch.setattr("regie.tmux.terminals.pane_inventory", inventory)
    monkeypatch.setattr("regie.tmux.terminals.pane_snapshot", snapshot)
    monkeypatch.setattr("regie.tmux.terminals.run", run)
    monkeypatch.setattr("regie.tmux.terminals.mark_pane", mark)

    first = BridgeStateStore(root)
    first.acquire()
    first.update(provider_id="provider-a", tmux_server_identity="server-a")
    callbacks = TmuxProviderCallbacks(first, generation_usable=lambda requested: requested == 3)
    with pytest.raises(RuntimeError, match="simulated bridge loss"):
        await callbacks.create(_request("terminal.create"))
    first.release()

    bridge = TmuxBridge(BridgeConfig(theater_socket=tmp_path / "unused.sock", state_dir=root))
    bridge._state.acquire()
    provider = SimpleNamespace(generation_active=True)
    bridge._provider = provider
    bridge._generation = 4
    try:
        intent = bridge._state.launch_intents()[0]
        assert intent.dispatched is True
        await bridge._recover_launches(provider, 4)
        terminals = await managed_inventory(
            provider_id="provider-a", generation=4, expected_server_identity="server-a"
        )
        assert (workload_starts, marker_attempts) == (1, 2)
        assert (terminals[0]["terminal_id"], terminals[0]["provider_generation"]) == ("%7", 4)
        receipt = bridge._state.receipt("terminal.create", "operation-a")
        assert receipt is not None and receipt["provider_generation"] == 3
        assert tuple(bridge._state.launch_dir.iterdir()) == ()
    finally:
        bridge._state.release()
