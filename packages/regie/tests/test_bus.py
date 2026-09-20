from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from regie.bus import DiagnosticBusController

from theater.frontend import FrontendClient


class _Diagnostics:
    def __init__(self) -> None:
        self.pages = [
            ({"id": 7}, {"id": 8}),
            ({"id": 12}, {"id": 11}),
        ]
        self.calls: list[tuple[int, int]] = []

    async def bus_tail(self, *, after_id: int, limit: int) -> object:
        self.calls.append((after_id, limit))
        return SimpleNamespace(value=SimpleNamespace(items=self.pages.pop(0)))


class _Client:
    def __init__(self) -> None:
        self.diagnostics = _Diagnostics()


@pytest.mark.asyncio
async def test_bus_reports_only_gaps_after_its_initial_cursor() -> None:
    client = _Client()
    controller = DiagnosticBusController(cast(FrontendClient, client), batch=50)

    assert [row["id"] for row in await controller.poll()] == [7, 8]
    assert controller.last_gap == 0
    assert [row["id"] for row in await controller.poll()] == [11, 12]
    assert controller.last_gap == 2
    assert controller.after_id == 12
    assert client.diagnostics.calls == [(0, 50), (8, 50)]


@pytest.mark.asyncio
async def test_malformed_bus_page_cannot_advance_the_cursor_partially() -> None:
    client = _Client()
    controller = DiagnosticBusController(cast(FrontendClient, client), batch=50)

    assert [row["id"] for row in await controller.poll()] == [7, 8]
    client.diagnostics.pages[0] = ({"id": 9}, "malformed", {"id": 10})

    with pytest.raises(TypeError, match="bus items"):
        await controller.poll()
    assert controller.after_id == 8
    assert controller.last_gap == 0
