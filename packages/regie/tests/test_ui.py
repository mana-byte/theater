from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import cast

import pytest
from regie.app import RegieApp
from regie.contracts import PresentationTarget, RegieSettings
from regie.state import StateController
from regie.widgets import ParticipantTree, UsageBreakdownPanel, UsageMetricTile
from regie.widgets.leaf import AgentLeaf
from regie.widgets.prompts import ResumePromptScreen, SpawnDirectoryScreen
from textual.command import CommandInput, CommandPalette

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


def _participant(
    participant_id: str,
    *,
    name: str,
    status: str = "idle",
    cwd: str | None = None,
    description: str | None = None,
    parent_id: str | None = None,
    trusted_identity: dict[str, str] | None = None,
) -> Participant:
    return Participant.from_wire(
        {
            "participant_id": participant_id,
            "origin": "spawned",
            "harness": "codex",
            "status": status,
            "owner": {"kind": "local_operator", "revision": 1},
            "name": name,
            "cwd": cwd,
            "description": description,
            "parent_id": parent_id,
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
            "trusted_identity": trusted_identity,
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
        self.catalog_acknowledgements: list[int] = []

    async def initialize(self) -> StateProjection:
        return self.projection

    async def synchronize(self) -> StateProjection:
        return self.projection

    def acknowledge_catalogs(self, generation: int) -> bool:
        self.catalog_acknowledgements.append(generation)
        if not self.projection.catalog_dirty or generation != self.projection.catalog_generation:
            return False
        self.projection = replace(self.projection, catalog_dirty=False)
        return True


class _Catalogs:
    def __init__(self) -> None:
        self.calls = 0

    async def harnesses(self) -> object:
        self.calls += 1
        entry = HarnessCatalogEntry.from_wire(
            {
                "name": "codex",
                "installed": True,
                "compatible": True,
                "supported_wiring": ["tmux"],
                "requires_terminal": True,
                "provider_ready": True,
                "launch_available": True,
                "approvals": ["manual", "edits", "yolo"],
                "reason": None,
                "detail": None,
            }
        )
        return SimpleNamespace(value=SimpleNamespace(items=(entry,)))


class _Usage:
    def __init__(self) -> None:
        self.by_harness_calls: list[dict[str, object]] = []

    async def totals(self, *, since: float) -> object:
        del since
        return SimpleNamespace(value={"tokens": 1})

    async def summary(self, *, since: float) -> object:
        del since
        return SimpleNamespace(value={})

    async def by_harness(
        self,
        *,
        since: float | None,
        detailed: bool = False,
    ) -> object:
        self.by_harness_calls.append({"since": since, "detailed": detailed})
        period = {
            "input_tokens": 12,
            "output_tokens": 8,
            "reasoning_output_tokens": 2,
            "cache_read_input_tokens": 4,
            "cache_creation_input_tokens": 1,
            "cost_microcents": 50_000_000,
            "active_days": 1,
        }
        row: dict[str, object] = {
            "harness": "codex",
            "today": period,
            "week": period,
            "month": period,
        }
        if detailed:
            row["models"] = [{"model": "fixture-model", **row}]
            value = {
                "harnesses": [row],
                "totals": {"today": period, "week": period, "month": period},
            }
        else:
            value = {"harnesses": [row]}
        return SimpleNamespace(value=value)


class _Diagnostics:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.calls: list[dict[str, int]] = []

    async def bus_tail(self, *, after_id: int, limit: int) -> object:
        self.calls.append({"after_id": after_id, "limit": limit})
        items = tuple(row for row in self.rows if int(row["id"]) > after_id)[:limit]
        return SimpleNamespace(value=SimpleNamespace(items=items))


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
        self.spawn_options: list[dict[str, object]] = []
        self.list_calls: list[dict[str, object]] = []
        self.dead_rows: tuple[Participant, ...] = ()

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
        cwd: str | None = None,
        resume: str | None = None,
    ) -> object:
        self.spawned.append((harness, prompt, approval))
        self.spawn_options.append(
            {"cwd": cwd, "resume": resume, "idempotency_key": idempotency_key}
        )
        return _accepted("participant-spawned")

    async def list(self, **params: object) -> object:
        self.list_calls.append(params)
        return SimpleNamespace(value=SimpleNamespace(items=self.dead_rows, next_cursor=None))


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
        self.diagnostics = _Diagnostics()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Presentation:
    def __init__(self) -> None:
        self.staged: list[PresentationTarget] = []
        self.focused: list[PresentationTarget] = []
        self.target_window_calls = 0

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

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

    async def resize_regie(self, *, width: int) -> None:
        del width

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
        settings=RegieSettings(tree_interval=60, bus_interval=60, startup_reveal=False),
        presentation=presentation,
    )
    app._state = cast(StateController, _State(_projection()))
    return app, client, presentation


@pytest.mark.asyncio
async def test_textual_keys_navigate_stage_focus_return_and_trajectory() -> None:
    app, client, presentation = _app()

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
        assert client.trajectory.closed == ["trajectory-a"]


@pytest.mark.asyncio
async def test_textual_prompts_palette_kill_bus_and_safe_quit() -> None:
    app, client, presentation = _app()

    async with app.run_test() as pilot:
        assert "Keys" not in {command.title for command in app.get_system_commands(app.screen)}

        await pilot.press("s")
        prompt = app.screen.query_one("#control-prompt-input")
        prompt.value = "hello"
        await pilot.press("enter")
        await pilot.pause()
        assert client.controls.requests == [("send", "participant-1", "hello")]

        await pilot.press("i")
        await pilot.pause()
        assert client.controls.requests[-1] == ("interrupt", "participant-1", None)

        await pilot.press("a")
        app.screen.query_one("#control-prompt-input").value = "change course"
        await pilot.press("enter")
        await pilot.pause()
        assert client.controls.requests[-1] == ("steer", "participant-1", "change course")

        await pilot.press("f")
        app.screen.query_one("#control-prompt-input").value = "after this"
        await pilot.press("enter")
        await pilot.pause()
        assert client.controls.requests[-1] == ("queue_followup", "participant-1", "after this")

        await pilot.press("g")
        app.screen.query_one("#settings-model").value = "model-a"
        await pilot.press("enter")
        await pilot.pause()
        assert client.controls.requests[-1] == ("settings", "participant-1", "model-a")

        await pilot.press("ctrl+p")
        palette = app.screen.query_one(CommandInput)
        palette.value = "spawn"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, CommandPalette)
        palette = app.screen.query_one(CommandInput)
        palette.value = "codex"
        await pilot.press("enter")
        await pilot.pause()
        assert client.participants.spawned == [("codex", "\n", "manual")]
        assert client.participants.spawn_options[-1]["cwd"] == str(Path.cwd())

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


@pytest.mark.asyncio
async def test_spawn_palette_accepts_a_completed_explicit_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "project with spaces"
    target.mkdir()
    monkeypatch.chdir(tmp_path)
    app, client, _presentation = _app()

    async with app.run_test() as pilot:
        await pilot.press("o")
        await pilot.pause()
        palette = app.screen.query_one(CommandInput)
        palette.value = "directory"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, SpawnDirectoryScreen)

        cwd_input = app.screen.query_one("#spawn-cwd")
        cwd_input.value = "proj"
        await pilot.press("tab")
        assert cwd_input.value == f"project with spaces{os.sep}"
        await pilot.press("enter")
        await pilot.pause()

    assert client.participants.spawned == [("codex", "\n", "manual")]
    assert client.participants.spawn_options[0]["cwd"] == str(target)


@pytest.mark.asyncio
async def test_textual_refetches_and_acknowledges_coalesced_catalog_invalidations() -> None:
    app, client, _presentation = _app()
    dirty = replace(_projection(), catalog_dirty=True, catalog_generation=7)
    state = _State(dirty)
    app._state = cast(StateController, state)

    async with app.run_test() as pilot:
        await pilot.pause()
        client.catalogs.calls = 0
        await app._synchronize_projection()
        await app._synchronize_projection()

    assert client.catalogs.calls == 1
    assert state.catalog_acknowledgements == [7]
    assert state.projection.catalog_dirty is False


@pytest.mark.asyncio
async def test_textual_resume_uses_a_bounded_public_dead_session_and_trusted_context() -> None:
    app, client, _presentation = _app()
    resumable = _participant(
        "dead-resumable",
        name="old session",
        status="dead",
        cwd="/workspace/original",
        trusted_identity={"session_id": "trusted-session", "provenance": "exact"},
    )
    unavailable = _participant(
        "dead-untrusted",
        name="old untrusted session",
        status="dead",
        cwd="/workspace/other",
    )
    client.participants.dead_rows = (resumable, unavailable)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, ResumePromptScreen)
        assert client.participants.list_calls == [{"status": "dead", "limit": 50}]
        candidates = app.screen.query_one("#resume-candidates")
        assert "dead-resumable" in str(candidates.render())
        assert "no trusted resume session is available" in str(candidates.render())

        app.screen.query_one("#resume-participant-id").value = "dead-resumable"
        app.screen.query_one("#resume-approval").value = "edits"
        await pilot.press("enter")
        await pilot.pause()

        assert client.participants.spawned == [
            ("codex", "Resume the trusted prior session.", "edits")
        ]
        assert client.participants.spawn_options[0]["cwd"] == "/workspace/original"
        assert client.participants.spawn_options[0]["resume"] == "trusted-session"


@pytest.mark.asyncio
async def test_rich_leaves_reconcile_animations_and_pointer_actions() -> None:
    app, _client, presentation = _app()
    first = _participant(
        "participant-1",
        name="first",
        status="working",
        cwd="/workspace/first",
        description="a deliberately long participant description " * 4,
    )
    second = _participant("participant-2", name="second", cwd="/workspace/second")
    projection = replace(
        _projection(),
        participants=MappingProxyType({first.participant_id: first, second.participant_id: second}),
    )
    state = _State(projection)
    app._state = cast(StateController, state)

    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        tree = app.query_one(ParticipantTree)
        first_leaf = tree._key_widgets[("p", first.participant_id)]
        assert isinstance(first_leaf, AgentLeaf)
        assert len(str(first_leaf.render()).splitlines()) == 3
        assert first_leaf._timer is not None
        assert first_leaf._marquee_timer is not None

        previous_frame = str(first_leaf.render())
        first_leaf._tick()
        assert str(first_leaf.render()) != previous_frame

        updated_first = _participant(
            first.participant_id,
            name="renamed",
            status="idle",
            cwd="/workspace/renamed",
            description="short description",
        )
        state.projection = replace(
            projection,
            participants=MappingProxyType(
                {updated_first.participant_id: updated_first, second.participant_id: second}
            ),
        )
        app._show_projection(state.projection)
        await pilot.pause()
        assert tree._key_widgets[("p", first.participant_id)] is first_leaf
        assert first_leaf._timer is None
        assert "renamed" in str(first_leaf.render())

        second_leaf = tree._key_widgets[("p", second.participant_id)]
        assert isinstance(second_leaf, AgentLeaf)
        await pilot.click(second_leaf, offset=(1, 2))
        assert app.selected_participant_id == second.participant_id
        assert len(presentation.staged) == 1
        assert second_leaf.has_class("tree-staged")

        await pilot.click(second_leaf, button=3)
        await pilot.pause()
        assert presentation.staged == []
        assert app.query_one("#trajectory-view").display is True
        assert second_leaf.has_class("tree-trajectory-staged")


@pytest.mark.asyncio
async def test_usage_footer_keyboard_pointer_and_detailed_mode_share_state() -> None:
    app, client, _presentation = _app()

    async with app.run_test(size=(100, 36)) as pilot:
        tree = app.query_one(ParticipantTree)
        panel = app.query_one(UsageBreakdownPanel)
        await pilot.press("j", "j")
        assert app._usage_panel.keyboard_metric == "input"
        assert panel.has_class("-visible")
        assert not list(tree.query(".tree-cursor"))

        await pilot.press("right")
        assert app._usage_panel.active_metric == "output"
        cache = app.query_one("#cache-col", UsageMetricTile)
        await pilot.hover(cache)
        await pilot.pause()
        assert app._usage_panel.active_metric == "cache"

        await pilot.click(cache)
        await pilot.pause()
        assert app._usage_panel.detailed
        assert any(call["detailed"] is True for call in client.usage.by_harness_calls)

        await pilot.hover(tree)
        await pilot.pause()
        assert app._usage_panel.active_metric == "output"
        await pilot.press("up")
        assert not app._usage_panel.in_footer
        assert not panel.has_class("-visible")
        assert list(tree.query(".tree-cursor"))


@pytest.mark.asyncio
async def test_hidden_bus_has_an_independent_route_and_await_animation_cursor() -> None:
    app, client, _presentation = _app()

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._animation_primed
        client.diagnostics.rows.append(
            {
                "id": 1,
                "kind": "agent.send",
                "from_id": "participant-1",
                "to_id": "participant-2",
                "payload": {"handle": "send:1"},
            }
        )
        await app._refresh_animations()
        assert len(app._animation.route_anims) == 1
        assert app._animation_bus.after_id == 1
        assert app._bus.after_id == 0

        while app._animation.route_anims:
            app._tick_route_animations()
        client.diagnostics.rows.append(
            {
                "id": 2,
                "kind": "job.await.start",
                "from_id": "participant-1",
                "to_id": "participant-2",
                "payload": {"handle": "send:1", "token": "await:1"},
            }
        )
        await app._refresh_animations()
        assert len(app._animation.await_anims) == 1
        app._tick_route_animations()
        tree = app.query_one(ParticipantTree)
        assert tree._overlaid

        client.diagnostics.rows.append(
            {
                "id": 3,
                "kind": "job.await.end",
                "from_id": "participant-1",
                "to_id": "participant-2",
                "payload": {"handle": "send:1", "token": "await:1"},
            }
        )
        await app._refresh_animations()
        assert app._animation.await_anims == {}
        assert tree._overlaid == set()
