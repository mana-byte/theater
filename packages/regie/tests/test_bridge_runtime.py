from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from regie.bridge.runtime import TmuxBridge
from regie.contracts import BridgeConfig

from theater.frontend import CallbackRequest


@pytest.fixture(autouse=True)
def focus_lifecycle(monkeypatch):
    class Focus:
        def __init__(self):
            self.changed = asyncio.Event()
            self.starts = []
            self.closed = False

        async def start(self, identity):
            self.starts.append(identity)

        async def aclose(self):
            self.closed = True

        async def observe(self, expected):
            raise AssertionError("this lifecycle test does not inspect terminals")

        def validate(self, evidence):
            raise AssertionError("this lifecycle test does not mutate terminals")

    monkeypatch.setattr("regie.bridge.runtime.FocusMonitor", Focus)


async def test_bridge_registers_reconnects_and_stops_without_terminal_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    generation = 0
    reports: list[tuple[int, int, object]] = []
    providers = []
    keep_invalidating = False
    heartbeat_seen = asyncio.Event()

    class ProviderFacade:
        async def register(self, *_args, **_kwargs):
            return SimpleNamespace(value=SimpleNamespace(provider_id="provider-a"))

        async def report(self, current: int, revision: int, *, facts: object):
            reports.append((current, revision, facts))
            if keep_invalidating:
                bridge._presence.changed.set()
            receipt_ids = [receipt["operation_id"] for receipt in facts.get("receipts", [])]
            return SimpleNamespace(
                value={
                    "acknowledged_operation_ids": receipt_ids,
                    "ignored_operation_ids": [
                        operation_id
                        for operation_id in receipt_ids
                        if operation_id == "private-terminate-orphan"
                    ],
                }
            )

        async def heartbeat(self, current: int, revision: int):
            heartbeat_seen.set()
            return SimpleNamespace(value={"generation": current, "revision": revision})

    class FakeFrontendClient:
        def __init__(self, *_args, **kwargs) -> None:
            self.providers = ProviderFacade()
            self.provider_connection = kwargs.get("role") is not None

        async def connect(self):
            return SimpleNamespace(
                provider_generation=generation, limits={"provider_heartbeat_seconds": 0.01}
            )

        async def close(self) -> None:
            return None

    class FakeProviderClient:
        def __init__(self, *_args, **_kwargs) -> None:
            self.closed = asyncio.Event()
            self.connected = False
            self.last_error = RuntimeError("scripted disconnect")
            self.provider_generation = None
            providers.append(self)

        async def connect(self):
            nonlocal generation
            generation += 1
            self.provider_generation = generation
            self.connected = True
            return SimpleNamespace(
                provider_generation=generation, limits={"provider_heartbeat_seconds": 0.01}
            )

        def renew_lease(self, *, generation: int) -> None:
            if generation == 1:
                self.connected = False
                self.closed.set()

        async def wait_closed(self) -> None:
            await self.closed.wait()

        async def close(self) -> None:
            self.connected = False
            self.closed.set()

    async def pin(*, cwd: str) -> str:
        assert cwd == str(tmp_path / "state")
        return "server-a"

    async def inventory(**_kwargs):
        return ()

    async def current_server():
        return "server-a"

    monkeypatch.setattr("regie.bridge.runtime.FrontendClient", FakeFrontendClient)
    monkeypatch.setattr("regie.bridge.runtime.ProviderClient", FakeProviderClient)
    monkeypatch.setattr("regie.bridge.runtime.ensure_server", pin)
    monkeypatch.setattr("regie.bridge.runtime.managed_inventory", inventory)
    monkeypatch.setattr("regie.bridge.runtime.current_server_identity", current_server)

    bridge = TmuxBridge(
        BridgeConfig(
            theater_socket=tmp_path / "frontend.sock",
            state_dir=tmp_path / "state",
            reconnect_initial_seconds=0.001,
            reconnect_max_seconds=0.002,
        )
    )
    bridge._state.acquire()
    bridge._state.write_receipt(
        "terminal.terminate",
        "private-terminate-orphan",
        {"operation_id": "private-terminate-orphan", "delivery": "accepted"},
    )
    bridge._state.release()
    task = asyncio.create_task(bridge.run())
    async with asyncio.timeout(1):
        while (  # noqa: ASYNC110
            bridge.status.provider_generation != 2 or bridge.status.connection_state != "online"
        ):
            await asyncio.sleep(0)
    assert bridge.status.connection_state == "online"
    assert reports[0][0] == 1 and reports[1][0] == 2
    assert len(providers) == 2
    assert bridge._state.receipts() == ()
    archived = next((bridge._state.state_dir / "unmatched-receipts").glob("*.json"))
    assert json.loads(archived.read_text())["operation_id"] == "private-terminate-orphan"

    bridge._state.write_receipt(
        "terminal.deliver",
        "operation-a",
        {"operation_id": "operation-a", "provider_generation": 2, "delivery": "accepted"},
    )
    assert bridge._report_client is not None
    await bridge._heartbeat(bridge._report_client, providers[-1], 2)
    facts = reports[-1][2]
    assert isinstance(facts, dict) and len(facts["receipts"]) == 1
    assert bridge._state.receipts() == ()

    heartbeat_seen.clear()
    keep_invalidating = True
    bridge._presence.changed.set()
    await asyncio.wait_for(heartbeat_seen.wait(), 1)
    keep_invalidating = False
    assert any(facts == {"presence_invalidated": True} for _, _, facts in reports)
    assert [revision for _, revision, _ in reports] == sorted(
        {revision for _, revision, _ in reports}
    )

    await bridge.close()
    await task
    assert bridge.status.connection_state == "stopped"
    assert bridge.status.running is False
    assert bridge._presence.starts == ["server-a", "server-a"]
    assert bridge._presence.closed


async def test_accepted_create_receipt_heartbeat_includes_complete_inventory(
    tmp_path: Path, monkeypatch
) -> None:
    bridge = TmuxBridge(
        BridgeConfig(theater_socket=tmp_path / "frontend.sock", state_dir=tmp_path / "state")
    )
    bridge._state.acquire()
    bridge._state.update(provider_id="provider-a", tmux_server_identity="server-a")
    bridge._state.write_receipt(
        "terminal.create",
        "operation-create",
        {"operation_id": "operation-create", "outcome": "accepted"},
    )
    terminal = {
        "provider_id": "provider-a",
        "provider_generation": 2,
        "terminal_id": "%1",
        "terminal_incarnation": "incarnation-a",
        "occupant": {"occupant_id": "participant-a"},
    }

    async def inventory(**_kwargs):
        return (terminal,)

    reports = []

    class Providers:
        async def report(self, generation, revision, *, facts):
            reports.append((generation, revision, facts))
            return SimpleNamespace(value={"acknowledged_operation_ids": ["operation-create"]})

    monkeypatch.setattr("regie.bridge.runtime.managed_inventory", inventory)
    try:
        await bridge._heartbeat(
            SimpleNamespace(providers=Providers()),
            SimpleNamespace(renew_lease=lambda **_kwargs: None),
            2,
        )
        facts = reports[0][2]
        assert facts["complete"] is True
        assert facts["terminals"] == [terminal]
        assert facts["tmux_server_identity"] == "server-a"
        assert bridge._state.receipts() == ()
    finally:
        bridge._state.release()


async def test_bridge_replaces_server_without_replaying_old_pending_effects(
    tmp_path: Path, monkeypatch
) -> None:
    bridge = TmuxBridge(
        BridgeConfig(theater_socket=tmp_path / "frontend.sock", state_dir=tmp_path / "state")
    )
    bridge._state.acquire()
    bridge._state.update(provider_id="provider-a", tmux_server_identity="server-original")
    old_intent = bridge._state.prepare_launch(
        "launch-old",
        "digest-old",
        operation_id="operation-old",
        provider_id="provider-a",
        provider_generation=3,
        participant_id="participant-old",
        executable="/bin/old-agent",
        tmux_server_identity="server-original",
    )
    old_intent = bridge._state.mark_launch_dispatched(old_intent)
    reports: list[object] = []
    renewals: list[int] = []
    creates: list[object] = []

    async def replacement(*, cwd: str) -> str:
        assert cwd == str(tmp_path / "state")
        return "server-replacement"

    async def inventory(**kwargs):
        assert kwargs["expected_server_identity"] == "server-replacement"
        return ()

    async def recover(**_kwargs):
        raise AssertionError("an old-server launch intent must not inspect replacement panes")

    async def create(**kwargs):
        creates.append(kwargs)
        await kwargs["before_create"]()
        kwargs["ensure_usable"]()
        return {
            "provider_id": "provider-a",
            "provider_generation": 4,
            "terminal_id": "%9",
            "terminal_incarnation": kwargs["terminal_incarnation"],
            "occupant": {
                "occupant_id": "participant-new",
                "provider_kind": "tmux",
                "tmux_server_identity": "server-replacement",
                "terminal_incarnation": kwargs["terminal_incarnation"],
                "pane_pid": 99,
            },
            "process": {"pid": 99},
            "launch_id": "launch-new",
            "presentation": {"kind": "tmux", "pane_id": "%9"},
        }

    class Reports:
        async def report(self, generation: int, revision: int, *, facts: object):
            reports.append((generation, revision, facts))
            return SimpleNamespace(value={})

    class ReportClient:
        providers = Reports()

    class Provider:
        generation_active = True

        def renew_lease(self, *, generation: int) -> None:
            renewals.append(generation)

    monkeypatch.setattr("regie.bridge.runtime.ensure_server", replacement)
    monkeypatch.setattr("regie.bridge.runtime.managed_inventory", inventory)
    monkeypatch.setattr("regie.bridge.runtime.recover_terminal_launch", recover)
    monkeypatch.setattr("regie.bridge.callbacks.create_terminal", create)
    provider = Provider()
    bridge._provider = provider
    bridge._generation = 4
    try:
        await bridge._pin_server()
        assert bridge._state.state.tmux_server_identity == "server-replacement"
        await bridge._report_inventory(ReportClient(), provider, 4)
        assert reports == [
            (
                4,
                1,
                {
                    "terminals": [],
                    "complete": True,
                    "receipts": [],
                    "tmux_server_identity": "server-replacement",
                },
            )
        ]
        assert renewals == [4]
        assert bridge._state.launch_intents() == (old_intent,)

        request = CallbackRequest(
            callback_id="callback-new",
            method="terminal.create",
            provider_generation=4,
            params={
                "operation_id": "operation-new",
                "provider_generation": 4,
                "participant_id": "participant-new",
                "launch_id": "launch-new",
                "launch": {
                    "executable": "/bin/new-agent",
                    "argv": ["/bin/new-agent"],
                    "cwd": "/tmp",
                    "environment": {},
                },
            },
        )
        result = await bridge._callbacks.create(request)
        assert result["outcome"] == "accepted"
        assert len(creates) == 1
        assert creates[0]["expected_server_identity"] == "server-replacement"
        assert bridge._state.launch_intents() == (old_intent,)
    finally:
        bridge._state.release()
