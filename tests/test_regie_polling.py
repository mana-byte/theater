"""Focused tests for Régie's daemon polling cadence."""

from __future__ import annotations

from theater.config import RegieSection
from theater.regie.controllers import polling as polling_mod
from theater.regie.controllers.polling import PollingController
from theater.regie.render.layout import render_tree


class FakeClient:
    def __init__(self, tree: list[dict], unmanaged: list[dict] | Exception) -> None:
        self.tree = tree
        self.unmanaged = unmanaged
        self.calls: list[str] = []

    async def call(self, method: str, **_params):
        self.calls.append(method)
        if method == "participants.tree":
            return self.tree
        if method == "participants.unmanaged":
            if isinstance(self.unmanaged, Exception):
                raise self.unmanaged
            return self.unmanaged
        raise AssertionError(f"unexpected RPC: {method}")


async def test_unmanaged_panes_refresh_every_five_seconds(monkeypatch) -> None:
    now = 100.0
    monkeypatch.setattr(polling_mod, "monotonic", lambda: now)
    client = FakeClient([], [{"pane": "%90", "harness": "codex", "cwd": "/tmp"}])
    controller = PollingController(RegieSection())

    first = await controller.poll_tree(2, client, render_tree)
    assert first.lines is not None
    assert [line[2] for line in first.lines] == [("sep", "unmanaged"), ("u", "%90")]
    assert client.calls == ["participants.tree", "participants.unmanaged"]

    now = 104.9
    client.unmanaged = [{"pane": "%91", "harness": "claude", "cwd": "/tmp"}]
    second = await controller.poll_tree(2, client, render_tree)
    assert second.lines is not None
    assert [line[2] for line in second.lines] == [("sep", "unmanaged"), ("u", "%90")]
    assert client.calls == [
        "participants.tree",
        "participants.unmanaged",
        "participants.tree",
    ]

    now = 105.0
    third = await controller.poll_tree(2, client, render_tree)
    assert third.lines is not None
    assert [line[2] for line in third.lines] == [("sep", "unmanaged"), ("u", "%91")]
    assert client.calls[-2:] == ["participants.tree", "participants.unmanaged"]


async def test_cached_unmanaged_pane_disappears_as_soon_as_it_is_managed(monkeypatch) -> None:
    now = 100.0
    monkeypatch.setattr(polling_mod, "monotonic", lambda: now)
    client = FakeClient([], [{"pane": "%90", "harness": "codex", "cwd": "/tmp"}])
    controller = PollingController(RegieSection())

    first = await controller.poll_tree(2, client, render_tree)
    assert first.lines is not None
    assert ("u", "%90") in [line[2] for line in first.lines]

    now = 101.0
    client.tree = [
        {
            "id": "parent",
            "tmux_pane": "%10",
            "children": [
                {
                    "id": "adopted",
                    "tmux_pane": "%90",
                    "status": "idle",
                    "children": [],
                }
            ],
        }
    ]
    second = await controller.poll_tree(2, client, render_tree)

    assert second.lines is not None
    keys = [line[2] for line in second.lines]
    assert ("p", "adopted") in keys
    assert ("u", "%90") not in keys
    assert client.calls[-1] == "participants.tree"


async def test_failed_unmanaged_refresh_retains_cache_without_retrying_each_tick(
    monkeypatch,
) -> None:
    now = 100.0
    monkeypatch.setattr(polling_mod, "monotonic", lambda: now)
    client = FakeClient([], [{"pane": "%90", "harness": "codex", "cwd": "/tmp"}])
    controller = PollingController(RegieSection())
    await controller.poll_tree(2, client, render_tree)

    now = 105.0
    client.unmanaged = RuntimeError("process scan failed")
    failed = await controller.poll_tree(2, client, render_tree)
    assert failed.lines is not None
    assert ("u", "%90") in [line[2] for line in failed.lines]

    now = 106.0
    cached = await controller.poll_tree(2, client, render_tree)
    assert cached.lines is not None
    assert client.calls[-1] == "participants.tree"
    assert client.calls.count("participants.unmanaged") == 2
