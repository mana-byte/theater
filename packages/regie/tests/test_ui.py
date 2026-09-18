from __future__ import annotations

from types import MappingProxyType, SimpleNamespace
from typing import cast

import pytest
from regie.app import RegieApp
from regie.contracts import PresentationTarget, RegieSettings
from regie.state import StateController
from regie.widgets.prompts import SpawnPromptScreen

from theater.frontend import (
    AcceptedOperation,
    EventCursor,
    FrontendClient,
    Participant,
    Provider,
    StateProjection,
)
from theater.frontend.dto.catalogs import HarnessCatalogEntry


def _capability() -> dict[str, object]:
    return {"supported": True, "route_available": True, "admissible": True}


def _participant(participant_id: str, *, name: str) -> Participant:
    return Participant.from_wire(
        {
            "participant_id": participant_id,
            "origin": "spawned",
            "harness": "codex",
            "status": "idle",
            "owner": {"kind": "local_operator", "revision": 1},
            "name": name,
            "addressable": True,
            "presence": "absent",
            "actions": {
                "send": _capability(),
                "steer": _capability(),
                "queue_followup": _capability(),
                "interrupt": _capability(),
                "settings_update": _capability(),
            },
            "terminal_route": {
                "identity": {
                    "provider_id": "provider-a",
                    "provider_generation": 1,
                    "terminal_id": f"%{participant_id[-1]}",
                    "terminal_incarnation": f"incarnation-{participant_id}",
                    "occupant": {"harness": "codex"},
                    "process": None,
                },
                "health": "healthy",
            },
        }
    )


def _projection() -> StateProjection:
    first = _participant("participant-1", name="first")
    second = _participant("participant-2", name="second")
    provider = Provider.from_wire(
        {
            "provider_id": "provider-a",
            "selector": "tmux",
            "kind": "tmux",
            "generation": 1,
            "health": "healthy",
            "capabilities": [],
        }
    )
    return StateProjection(
        cursor=EventCursor("stream-a", 1),
        participants=MappingProxyType({first.participant_id: first, second.participant_id: second}),
        operations=MappingProxyType({}),
        jobs=MappingProxyType({}),
        providers=MappingProxyType({provider.provider_id: provider}),
        workspaces=MappingProxyType({}),
    )


class _State:
    def __init__(self, projection: StateProjection) -> None:
        self.projection = projection

    async def initialize(self) -> StateProjection:
        return self.projection

    async def synchronize(self) -> StateProjection:
        return self.projection


class _Catalogs:
    async def harnesses(self) -> object:
        entry = HarnessCatalogEntry.from_wire(
            {
                "name": "codex",
                "installed": True,
                "compatible": True,
                "supported_wiring": ["tmux"],
                "requires_terminal": True,
                "provider_ready": True,
                "launch_available": True,
                "reason": None,
                "detail": None,
            }
        )
        return SimpleNamespace(value=SimpleNamespace(items=(entry,)))


class _Usage:
    async def totals(self, *, since: float) -> object:
        del since
        return SimpleNamespace(value={"tokens": 1})

    async def summary(self, *, since: float) -> object:
        del since
        return SimpleNamespace(value={})

    async def by_harness(self, *, since: float) -> object:
        del since
        return SimpleNamespace(value={})


class _Controls:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str | None]] = []

    async def send(self, participant_id: str, prompt: str, *, idempotency_key: str) -> object:
        self.requests.append(("send", participant_id, prompt))
        return _accepted(participant_id)

    async def steer(self, participant_id: str, prompt: str, *, idempotency_key: str) -> object:
        self.requests.append(("steer", participant_id, prompt))
        return _accepted(participant_id)

    async def queue_followup(
        self,
        participant_id: str,
        prompt: str,
        *,
        idempotency_key: str,
    ) -> object:
        self.requests.append(("queue_followup", participant_id, prompt))
        return _accepted(participant_id)

    async def interrupt(self, participant_id: str, *, idempotency_key: str) -> object:
        self.requests.append(("interrupt", participant_id, None))
        return _accepted(participant_id)

    async def update_settings(
        self,
        participant_id: str,
        *,
        idempotency_key: str,
        **settings: str,
    ) -> object:
        self.requests.append(("settings", participant_id, settings.get("model")))
        return _accepted(participant_id)

    async def get(self, participant_id: str) -> object:
        return SimpleNamespace(value={"participant_id": participant_id})


class _Participants:
    def __init__(self) -> None:
        self.terminated: list[str] = []
        self.spawned: list[tuple[str, str, str]] = []

    async def terminate(self, participant_id: str, *, idempotency_key: str) -> object:
        self.terminated.append(participant_id)
        return _accepted(participant_id)

    async def spawn(
        self,
        harness: str,
        prompt: str,
        approval: str,
        *,
        idempotency_key: str,
    ) -> object:
        self.spawned.append((harness, prompt, approval))
        return _accepted("participant-spawned")


class _Trajectory:
    def __init__(self) -> None:
        self.closed: list[str] = []

    async def snapshot(
        self,
        participant_id: str,
        *,
        limit: int,
        before: str | None = None,
    ) -> object:
        del limit, before
        return SimpleNamespace(
            value={
                "stream_id": "trajectory-a",
                "cursor": "cursor-a",
                "records": [
                    {
                        "record_id": "record-a",
                        "participant_id": participant_id,
                        "revision": 1,
                        "kind": "tool",
                        "summary": "public trajectory record",
                    },
                    {
                        "record_id": "record-b",
                        "participant_id": participant_id,
                        "revision": 2,
                        "kind": "prompt",
                        "summary": "second public trajectory record",
                    },
                ],
            }
        )

    async def follow(self, stream_id: str, cursor: str, *, wait_seconds: int) -> object:
        del stream_id, cursor, wait_seconds
        return SimpleNamespace(value={"stream_id": "trajectory-a", "upserts": []})

    async def search(self, participant_id: str, query: str, *, limit: int) -> object:
        del participant_id, query, limit
        return SimpleNamespace(value=SimpleNamespace(items=()))

    async def close(self, stream_id: str) -> object:
        self.closed.append(stream_id)
        return SimpleNamespace(value={"released": True})


class _Client:
    def __init__(self) -> None:
        self.catalogs = _Catalogs()
        self.usage = _Usage()
        self.controls = _Controls()
        self.participants = _Participants()
        self.trajectory = _Trajectory()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Presentation:
    def __init__(self) -> None:
        self.staged: list[PresentationTarget] = []
        self.focused: list[PresentationTarget] = []
        self.target_window_calls = 0

    def can_stage(self, target: PresentationTarget) -> tuple[bool, str | None]:
        return True, None

    async def target_window(self) -> str:
        self.target_window_calls += 1
        return "@regie"

    async def terminal_exists(self, target: PresentationTarget) -> bool:
        return True

    async def stage_terminal(self, target: PresentationTarget, *, target_window: str) -> None:
        assert target_window == "@regie"
        self.staged.append(target)

    async def unstage_terminal(self, target: PresentationTarget) -> None:
        self.staged.remove(target)

    async def focus_terminal(self, target: PresentationTarget) -> None:
        self.focused.append(target)

    async def resize_pane(
        self,
        pane_id: str,
        *,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        del pane_id, width, height


def _accepted(participant_id: str) -> object:
    return SimpleNamespace(
        value=AcceptedOperation(
            operation_id=f"operation-{participant_id}",
            state="succeeded",
            participant_id=participant_id,
        )
    )


def _app() -> tuple[RegieApp, _Client, _Presentation]:
    client = _Client()
    presentation = _Presentation()
    app = RegieApp(
        client=cast(FrontendClient, client),
        settings=RegieSettings(tree_interval=60, bus_interval=60),
        presentation=presentation,
    )
    app._state = cast(StateController, _State(_projection()))
    return app, client, presentation


@pytest.mark.asyncio
async def test_textual_keys_navigate_stage_focus_return_and_trajectory() -> None:
    app, _client, presentation = _app()

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.selected_participant_id == "participant-1"

        await pilot.press("l")
        assert len(presentation.staged) == 1
        await pilot.press("l")
        assert presentation.focused == presentation.staged

        await pilot.press("j")
        assert app.selected_participant_id == "participant-2"
        await pilot.press("enter")
        assert presentation.target_window_calls == 2
        assert len(presentation.staged) == 1
        await pilot.press("l")
        assert presentation.focused[-1:] == presentation.staged

        await pilot.press("h")
        assert app.query_one("#trajectory-view").display is True
        assert "public trajectory record" in str(app.query_one("#trajectory-ledger").render())
        assert app.query_one("#trajectory-view").selected_record_id == "record-a"
        await pilot.press("j")
        assert app.query_one("#trajectory-view").selected_record_id == "record-b"
        await pilot.press("escape")
        assert app.query_one("#catalog-dashboard").display is True


@pytest.mark.asyncio
async def test_textual_prompts_palette_kill_bus_and_safe_quit() -> None:
    app, client, presentation = _app()

    async with app.run_test() as pilot:
        await pilot.press("s")
        prompt = app.screen.query_one("#control-prompt-input")
        prompt.value = "hello"
        await pilot.press("enter")
        await pilot.pause()
        assert client.controls.requests == [("send", "participant-1", "hello")]

        await pilot.press("i")
        await pilot.pause()
        assert client.controls.requests[-1] == ("interrupt", "participant-1", None)

        await pilot.press("g")
        app.screen.query_one("#settings-model").value = "model-a"
        await pilot.press("enter")
        await pilot.pause()
        assert client.controls.requests[-1] == ("settings", "participant-1", "model-a")

        await pilot.press("ctrl+p")
        palette = app.screen.query_one("#palette-input")
        palette.value = "spawn codex"
        await pilot.press("enter")
        assert isinstance(app.screen, SpawnPromptScreen)
        app.screen.query_one("#spawn-prompt-input").value = "inspect"
        await pilot.press("enter")
        await pilot.pause()
        assert client.participants.spawned == [("codex", "inspect", "manual")]

        await pilot.press("x")
        await pilot.pause()
        assert client.participants.terminated == ["participant-1"]
        assert app.bus_visible is False
        await pilot.press("v")
        assert app.bus_visible is True

        await pilot.press("enter")
        assert presentation.staged
        await pilot.press("q")
        assert presentation.staged == []
        assert client.participants.terminated == ["participant-1"]
