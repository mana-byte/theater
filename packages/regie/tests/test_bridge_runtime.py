from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from regie.bridge.runtime import TmuxBridge
from regie.contracts import BridgeConfig


async def test_bridge_registers_reconnects_and_stops_without_terminal_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    generation = 0
    reports: list[tuple[int, int, object]] = []
    providers = []

    class ProviderFacade:
        async def register(self, *_args, **_kwargs):
            return SimpleNamespace(value=SimpleNamespace(provider_id="provider-a"))

        async def report(self, current: int, revision: int, *, facts: object):
            reports.append((current, revision, facts))
            return SimpleNamespace(value={})

        async def heartbeat(self, current: int, revision: int):
            return SimpleNamespace(value={"generation": current, "revision": revision})

    class FakeFrontendClient:
        def __init__(self, *_args, **kwargs) -> None:
            self.providers = ProviderFacade()
            self.provider_connection = kwargs.get("role") is not None

        async def connect(self):
            return SimpleNamespace(provider_generation=generation, limits={})

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
            return SimpleNamespace(provider_generation=generation, limits={})

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

    monkeypatch.setattr("regie.bridge.runtime.FrontendClient", FakeFrontendClient)
    monkeypatch.setattr("regie.bridge.runtime.ProviderClient", FakeProviderClient)
    monkeypatch.setattr("regie.bridge.runtime.ensure_server", pin)
    monkeypatch.setattr("regie.bridge.runtime.managed_inventory", inventory)

    bridge = TmuxBridge(
        BridgeConfig(
            theater_socket=tmp_path / "frontend.sock",
            state_dir=tmp_path / "state",
            reconnect_initial_seconds=0.001,
            reconnect_max_seconds=0.002,
        )
    )
    task = asyncio.create_task(bridge.run())
    async with asyncio.timeout(1):
        while bridge.status.provider_generation != 2:  # noqa: ASYNC110
            await asyncio.sleep(0)
    assert bridge.status.connection_state == "online"
    assert reports[0][0] == 1 and reports[1][0] == 2
    assert len(providers) == 2

    await bridge.close()
    await task
    assert bridge.status.connection_state == "stopped"
    assert bridge.status.running is False
