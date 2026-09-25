from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import cast

import pytest
from regie.app import RegieApp
from regie.contracts import PresentationTarget, RegieSettings, UnmanagedPane
from regie.controllers.actions import ActionRecord, ActionState
from regie.controllers.staging import StageOutcome, StageResult
from regie.controllers.surface import SurfaceMode
from regie.controllers.transcripts import TranscriptBindingController, TranscriptBindState
from regie.dashboard.widgets import WelcomeDashboard
from regie.render.glyphs import visible_name_span
from regie.state import StateController
from regie.trajectory.rich import TrajectoryView
from regie.widgets import ParticipantTree, UsageBreakdownPanel, UsageMetricTile
from regie.widgets.leaf import AgentLeaf
from regie.widgets.name_editor import NameEditor
from regie.widgets.prompts import (
    SpawnDirectoryScreen,
    TranscriptTransferScreen,
)
from textual.command import CommandInput, CommandPalette
from textual.geometry import Offset

from tests.rig.waiting import wait_until
from theater.frontend import (
    AcceptedOperation,
    ErrorValue,
    EventCursor,
    FrontendClient,
    FrontendResponseError,
    FrontendTransportError,
    Participant,
    Provider,
    ResponseValidationError,
    StateProjection,
    TranscriptBindResult,
    TranscriptCandidate,
)
from theater.frontend import ResumeCandidate as PublicResumeCandidate
from theater.frontend.dto import Response
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
    transcript_identity: dict[str, str | None] | None = None,
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
            "transcript_identity": transcript_identity,
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
        self.projection: StateProjection | None = projection
        self.initialize_calls = 0
        self.catalog_acknowledgements: list[int] = []

    async def initialize(self) -> StateProjection:
        self.initialize_calls += 1
        assert self.projection is not None
        return self.projection

    async def synchronize(self) -> StateProjection:
        assert self.projection is not None
        return self.projection

    async def follow(self) -> StateProjection:
        await asyncio.Future()

    def acknowledge_catalogs(self, generation: int) -> bool:
        self.catalog_acknowledgements.append(generation)
        assert self.projection is not None
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
                "binary": "codex",
                "icon": "◈",
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
        self.by_participant_calls: list[dict[str, object]] = []
        self.participant_cost_microcents = 42_000_000

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

    async def by_participant(
        self,
        *,
        since: float | None,
        participant_ids: tuple[str, ...] | None = None,
        limit: int,
    ) -> object:
        self.by_participant_calls.append(
            {"since": since, "participant_ids": participant_ids, "limit": limit}
        )
        return SimpleNamespace(
            value={
                "since": since,
                "truncated": False,
                "participants": [
                    {
                        "participant_id": "participant-1",
                        "harness": "codex",
                        "models": ["fixture-model"],
                        "input_tokens": 12,
                        "output_tokens": 8,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "reasoning_output_tokens": 0,
                        "cost_microcents": self.participant_cost_microcents,
                        "first_at": 1.0,
                        "last_at": 2.0,
                    }
                ],
            }
        )


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
        self.spawned: list[tuple[str, str | None, str]] = []
        self.spawn_options: list[dict[str, object]] = []
        self.resume_calls: list[dict[str, object]] = []
        self.dead_rows: tuple[PublicResumeCandidate, ...] = ()
        self.renames: list[dict[str, object]] = []
        self.rename_error: FrontendResponseError | None = None

    async def terminate(self, participant_id: str, *, idempotency_key: str) -> object:
        self.terminated.append(participant_id)
        return _accepted(participant_id)

    async def update(
        self,
        participant_id: str,
        *,
        idempotency_key: str,
        name: object = None,
        description: object = None,
    ) -> object:
        del description
        self.renames.append(
            {"participant_id": participant_id, "name": name, "idempotency_key": idempotency_key}
        )
        if self.rename_error is not None:
            raise self.rename_error
        return SimpleNamespace(value=SimpleNamespace(participant_id=participant_id))

    async def spawn(
        self,
        harness: str,
        prompt: str | None,
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

    async def resume_candidates(self, **params: object) -> object:
        self.resume_calls.append(params)
        return SimpleNamespace(value=SimpleNamespace(items=self.dead_rows, next_cursor=None))


class _Transcripts:
    def __init__(self) -> None:
        self.candidate_rows: tuple[TranscriptCandidate, ...] = ()
        self.candidate_calls: list[str] = []
        self.bind_calls: list[dict[str, object]] = []
        self.bind_outcomes: list[object] = []

    async def candidates(self, participant_id: str) -> object:
        self.candidate_calls.append(participant_id)
        return SimpleNamespace(value=SimpleNamespace(items=self.candidate_rows))

    async def bind(
        self,
        participant_id: str,
        location: str,
        *,
        idempotency_key: str,
        prior_owner_id: str | None = None,
    ) -> object:
        self.bind_calls.append(
            {
                "participant_id": participant_id,
                "location": location,
                "prior_owner_id": prior_owner_id,
                "idempotency_key": idempotency_key,
            }
        )
        if self.bind_outcomes:
            outcome = self.bind_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return SimpleNamespace(
            value=TranscriptBindResult(
                participant_id,
                location,
                prior_owner_id=prior_owner_id,
            )
        )


class _Trajectory:
    def __init__(self) -> None:
        self.closed: list[str] = []
        self.snapshot_limits: list[int] = []

    async def snapshot(
        self,
        participant_id: str,
        *,
        limit: int,
        before: str | None = None,
    ) -> object:
        del before
        self.snapshot_limits.append(limit)
        return SimpleNamespace(
            value={
                "panel_state": {"state": "ready", "participant_state": "live"},
                "stream_id": "trajectory-a",
                "cursor": "cursor-a",
                "older_cursor": None,
                "has_older": False,
                "records": [
                    {
                        "record_id": "record-a",
                        "participant_id": participant_id,
                        "revision": 1,
                        "source_epoch": "epoch-a",
                        "lane": "model",
                        "kind": "assistant",
                        "source": "claude",
                        "summary": "public trajectory record",
                        "status": "completed",
                        "turn_id": "turn-a",
                    },
                    {
                        "record_id": "record-b",
                        "participant_id": participant_id,
                        "revision": 2,
                        "source_epoch": "epoch-a",
                        "lane": "model",
                        "kind": "assistant",
                        "source": "claude",
                        "summary": "second public trajectory record",
                        "status": "completed",
                        "turn_id": "turn-b",
                    },
                ],
                "groups": [],
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
        self.transcripts = _Transcripts()
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
        self.unmanaged: tuple[UnmanagedPane, ...] = ()
        self.copied: list[str] = []
        self.terminals_exist = True

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
        return self.terminals_exist

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

    async def copy_text(self, text: str) -> None:
        self.copied.append(text)

    async def unmanaged_panes(
        self, *, harness_commands: Mapping[str, tuple[str, ...]]
    ) -> tuple[UnmanagedPane, ...]:
        assert "codex" in harness_commands
        return self.unmanaged


def _accepted(participant_id: str) -> object:
    return SimpleNamespace(
        value=AcceptedOperation(
            operation_id=f"operation-{participant_id}",
            state="succeeded",
            participant_id=participant_id,
        )
    )


def _app(
    *,
    usage_visible: bool = True,
    tree_layout_path: Path | None = None,
    projection: StateProjection | None = None,
) -> tuple[RegieApp, _Client, _Presentation]:
    """Most UI tests exercise the footer, so they opt into showing it."""
    client = _Client()
    presentation = _Presentation()
    app = RegieApp(
        client=cast(FrontendClient, client),
        settings=RegieSettings(
            tree_interval=60,
            bus_interval=60,
            startup_reveal=False,
            usage_visible=usage_visible,
        ),
        presentation=presentation,
        tree_layout_path=tree_layout_path,
    )
    app._state = cast(StateController, _State(projection or _projection()))
    return app, client, presentation


async def test_startup_reveals_state_before_catalog_discovery_and_usage(  # noqa: PLR0915
    monkeypatch,
    caplog,
) -> None:
    app, client, _presentation = _app()
    app.settings = replace(app.settings, startup_reveal=True)
    caplog.set_level("INFO", logger="regie.latency")
    catalog_release = asyncio.Event()
    usage_release = asyncio.Event()
    snapshot_started = asyncio.Event()
    snapshot_release = asyncio.Event()
    synchronized = asyncio.Event()
    load_catalog = client.catalogs.harnesses
    initialize = app._state.initialize
    timers: list[str] = []
    set_interval = app.set_interval

    async def catalog():
        assert any(message.startswith("startup.first_frame ") for message in caplog.messages)
        await catalog_release.wait()
        return await load_catalog()

    async def snapshot():
        snapshot_started.set()
        await snapshot_release.wait()
        return await initialize()

    async def usage():
        await usage_release.wait()

    async def synchronize():
        synchronized.set()
        return app._state.projection

    def track_interval(interval, callback, **kwargs):
        timers.append(callback.__name__)
        return set_interval(interval, callback, **kwargs)

    monkeypatch.setattr(client.catalogs, "harnesses", catalog)
    monkeypatch.setattr(app._state, "initialize", snapshot)
    monkeypatch.setattr(app._state, "follow", synchronize)
    monkeypatch.setattr(app, "_refresh_usage", usage)
    monkeypatch.setattr(app, "set_interval", track_interval)

    async with asyncio.timeout(5), app.run_test() as pilot:
        await snapshot_started.wait()
        await pilot.pause()
        tree = app.query_one(ParticipantTree)
        assert tree.loading and not tree._leaf_reveal.started
        assert "_refresh_local_projection" not in timers and "usage" not in timers
        await pilot.press("j")
        assert app._usage_panel.keyboard_metric is None

        snapshot_release.set()
        await synchronized.wait()
        await pilot.pause()
        assert not tree.loading and tree._leaf_reveal.started
        assert not catalog_release.is_set() and not usage_release.is_set()
        assert "_refresh_local_projection" not in timers
        await pilot.press("j")
        assert app.selected_participant_id == "participant-2"

        catalog_release.set()
        await app.wait_for_catalog()
        await pilot.pause()

        assert not tree.loading and not app.query_one("#sidebar").loading
        assert tree._leaf_reveal.started
        assert "◈" in str(tree.tree_lines[0][0])
        assert app.selected_participant_id == "participant-2"
        assert "_refresh_local_projection" in timers and "usage" not in timers
        await synchronized.wait()
        assert any(message.startswith("startup.participants_ready ") for message in caplog.messages)
        assert not any(message.startswith("startup.ready ") for message in caplog.messages)
        usage_release.set()
        assert app._startup_task is not None
        await app._startup_task
        await pilot.pause()
        assert sorted(timers) == [
            "_refresh_animations",
            "_refresh_bus",
            "_refresh_local_projection",
            "usage",
        ]
        assert any(message.startswith("startup.ready ") for message in caplog.messages)
        assert app.selected_participant_id == "participant-2"


@pytest.mark.parametrize("reader", ["follow", "action"])
async def test_late_projection_work_renders_the_latest_installed_state(monkeypatch, reader):
    app, _client, _presentation = _app()
    async with app.run_test() as pilot:
        await pilot.pause()
        fresh = replace(app._state.projection, cursor=EventCursor("stream-a", 2))
        shown = []

        async def auxiliary_read(_projection, **_kwargs):
            app._state.projection = fresh

        monkeypatch.setattr(app, "_refresh_unmanaged", auxiliary_read)
        monkeypatch.setattr(app, "_show_projection", shown.append)
        if reader == "follow":
            await app._synchronize_projection()
        else:
            await app._reconcile_completed_action(
                ActionRecord("spawn", "target", "action", state=ActionState.SUCCEEDED)
            )
            # Local pane discovery decorates after the action's first render.
            await app.workers.wait_for_complete()
        assert shown[-1] is fresh


@pytest.mark.parametrize("reader", ["state", "usage", "bus"])
async def test_late_reader_does_not_paint_after_shutdown(monkeypatch, reader):
    app, _client, _presentation = _app()
    started = asyncio.Event()
    release = asyncio.Event()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._startup_task is not None
        await app._startup_task
        owner, method, callback = {
            "state": (app._state, "synchronize", app._tick_synchronize),
            "usage": (app._usage, "refresh", app._refresh_usage),
            "bus": (app._bus, "poll", app._refresh_bus),
        }[reader]
        original = getattr(owner, method)
        app._bus_visible = True

        async def blocked(*args, **kwargs):
            result = await original(*args, **kwargs)
            started.set()
            await release.wait()
            return result

        monkeypatch.setattr(owner, method, blocked)
        task = asyncio.create_task(callback())
        await started.wait()
    release.set()
    await task


@pytest.mark.parametrize("before_start", [False, True])
async def test_quit_joins_startup_before_restoring_presentation(monkeypatch, before_start) -> None:
    app, client, presentation = _app()
    started = asyncio.Event()
    events: list[str] = []

    async def catalog():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("read cancelled")

    async def close():
        assert app._startup_task is not None and app._startup_task.done()
        events.append("presentation restored")

    monkeypatch.setattr(client.catalogs, "harnesses", catalog)
    monkeypatch.setattr(presentation, "close", close)
    if before_start:
        app._start_initial_load()
        await app.action_quit()
    else:
        async with asyncio.timeout(5), app.run_test() as pilot:
            await started.wait()
            await pilot.press("q")
        assert client.closed
    assert events == (["read cancelled"] if not before_start else []) + ["presentation restored"]


async def test_initial_snapshot_failure_leaves_navigation_and_refresh_recoverable(monkeypatch):
    app, _client, _presentation = _app()
    initialize = app._state.initialize
    state = cast(_State, app._state)
    projection = state.projection
    state.projection = None  # type: ignore[assignment]

    async def unavailable():
        raise FrontendTransportError("offline")

    monkeypatch.setattr(state, "initialize", unavailable)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._startup_task is not None
        await app._startup_task
        assert not app.query_one(ParticipantTree).loading
        assert app.check_action("cursor_down", ()) is not False
        assert app._last_state_error is not None
        state.projection = projection
        monkeypatch.setattr(state, "initialize", initialize)
        await app._tick_synchronize()
        assert app.selected_participant_id == "participant-1"


@pytest.mark.asyncio
async def test_public_catalog_icon_reaches_the_participant_tree() -> None:
    app, _client, _presentation = _app()

    async with app.run_test() as pilot:
        await pilot.pause()
        [first, *_rest] = app.query_one(ParticipantTree).tree_lines

    assert "◈" in str(first[0])


async def test_slow_stage_keeps_navigation_responsive_and_captures_queued_targets(monkeypatch):
    app, _client, presentation = _app()
    entered, release = asyncio.Event(), asyncio.Event()
    stage = presentation.stage_terminal
    seen = []

    async def delayed(target, *, target_window):
        seen.append(target.terminal_id)
        if len(seen) == 1:
            entered.set()
            await release.wait()
        await stage(target, target_window=target_window)

    monkeypatch.setattr(presentation, "stage_terminal", delayed)
    async with asyncio.timeout(5), app.run_test() as pilot:
        try:
            await pilot.pause()
            await pilot.press("enter")
            await entered.wait()
            await pilot.press("j", "enter", "k")
            assert app.selected_participant_id == "participant-1"
            assert seen == ["%1"]
            release.set()
            await app._presentation_queue.run("barrier", lambda: asyncio.sleep(0))
            assert seen == ["%1", "%2"]
            assert presentation.staged[-1].terminal_id == "%2"
            assert app.selected_participant_id == "participant-1"
        finally:
            release.set()


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
        view = app.query_one("#trajectory-view", TrajectoryView)
        await view.wait_until_loaded()
        assert client.trajectory.snapshot_limits == [200]
        assert view.state_store.page_size == 30
        assert [record.summary for record in view.state.records.values()] == [
            "public trajectory record",
            "second public trajectory record",
        ]
        assert view.state.selected_id == "record-b"
        await pilot.press("h")
        assert app.query_one("#trajectory-timeline").has_focus
        await pilot.press("h")
        assert view.state.selected_id == "record-a"
        await pilot.press("l")
        assert view.state.selected_id == "record-b"
        await pilot.press("escape")
        await pilot.pause()
        # Leaving a trajectory closes it and returns to the dashboard.
        assert not app.query("#trajectory-view")
        assert app._surface.mode is SurfaceMode.DASHBOARD
        assert app.query_one("#catalog-dashboard").display is True


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["route", "provider"])
async def test_stage_recovers_missing_route_or_provider_from_snapshot(monkeypatch, missing) -> None:
    app, _client, presentation = _app()
    state = cast(_State, app._state)
    fresh = state.projection
    if missing == "route":
        participant = replace(fresh.participants["participant-1"], terminal_route=None)
        stale = replace(
            fresh,
            participants=MappingProxyType(
                {**fresh.participants, participant.participant_id: participant}
            ),
        )
    else:
        stale = replace(fresh, providers=MappingProxyType({}))

    async with app.run_test() as pilot:
        await pilot.pause()
        state.projection = stale
        app._show_projection(stale)

        async def refresh() -> StateProjection:
            state.projection = fresh
            return fresh

        monkeypatch.setattr(state, "initialize", refresh)
        result = await app.stage_participant("participant-1")

        assert result is not None and result.outcome is StageOutcome.STAGED
        assert [target.terminal_id for target in presentation.staged] == ["%1"]
        assert state.projection is fresh


@pytest.mark.asyncio
async def test_focused_trajectory_without_a_selection_warns() -> None:
    app, _client, _presentation = _app()
    state = cast(_State, app._state)
    state.projection = replace(state.projection, participants=MappingProxyType({}))
    messages: list[str] = []
    app.notify = lambda message, **_kwargs: messages.append(str(message))  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_stage_and_focus_trajectory()

    assert messages == ["nothing to inspect"]


@pytest.mark.asyncio
async def test_focused_trajectory_keeps_its_keys_and_leaves_on_return_signal() -> None:
    app, _client, presentation = _app()

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("h", "h")
        view = app.query_one("#trajectory-view", TrajectoryView)
        await view.wait_until_loaded()
        assert view.has_focus_within

        await pilot.press("l")
        assert presentation.staged == []
        assert app._surface.mode is SurfaceMode.TRAJECTORY

        await pilot.press("/")
        assert view.state.search_open
        assert app.focused is not None and app.focused.id == "trajectory-search"
        await pilot.press("escape", "ctrl+g")  # the tmux return key (prefix h)
        await pilot.pause()
        assert not app.query("#trajectory-view")  # leaves the trajectory, as Esc does
        assert app._surface.mode is SurfaceMode.DASHBOARD


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["l", "shift+l"])
async def test_staging_from_trajectory_retains_dashboard_after_unstage(key: str) -> None:
    app, _client, presentation = _app()

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("h")
        await app.query_one("#trajectory-view", TrajectoryView).wait_until_loaded()
        assert app._surface.mode is SurfaceMode.TRAJECTORY

        await pilot.press(key)
        await pilot.pause()
        leaf = app.query_one(ParticipantTree)._key_widgets[("p", "participant-1")]
        assert app._surface.mode is SurfaceMode.DASHBOARD
        assert app._surface.trajectory_participant_id is None
        assert leaf.has_class("tree-staged")
        assert not leaf.has_class("tree-trajectory-staged")
        if key == "shift+l":
            assert presentation.focused == presentation.staged

        app._show_stage_result(await app._staging.unstage())
        await pilot.pause()
        assert app.query_one("#catalog-dashboard").display is True
        assert not app.query("#trajectory-view")
        assert not leaf.has_class("tree-staged")
        assert not leaf.has_class("tree-trajectory-staged")


@pytest.mark.asyncio
async def test_stage_failure_uses_rc9_message_and_error_severity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _client, _presentation = _app()
    notes: list[tuple[str, str]] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            app,
            "notify",
            lambda message, **kwargs: notes.append((str(message), str(kwargs["severity"]))),
        )
        app._show_stage_result(
            StageResult(StageOutcome.FAILED, None, "stage failed: pane disappeared")
        )

    assert notes == [("stage failed: pane disappeared", "error")]


@pytest.mark.asyncio
async def test_missing_staged_terminal_is_reconciled_on_the_tmux_poll() -> None:
    app, _client, presentation = _app()

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_stage()
        assert app._staging.staged_target is not None

        presentation.terminals_exist = False
        projection = app.projection
        assert projection is not None
        await app._refresh_unmanaged(projection, force=True)

        assert app._staging.staged_target is None
        assert app.query_one("#catalog-dashboard").display is True


@pytest.mark.asyncio
async def test_unmanaged_selection_can_stage_but_never_becomes_a_control_target() -> None:
    app, client, presentation = _app()
    presentation.unmanaged = (
        UnmanagedPane("%1", "codex", "/already-managed"),
        UnmanagedPane("%9", "zsh", "/workspace/shell"),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one(ParticipantTree)
        assert ("u", "%1") not in tree.selectable_keys
        assert ("u", "%9") in tree.selectable_keys
        assert app._navigation.selected_id == "participant-1"

        app.select_tree_item(("u", "%9"), "%9")
        assert tree.selected_unmanaged_pane == "%9"
        assert app.selected_participant_id is None
        assert app._navigation.selected_id == "participant-1"

        await app.action_stage()
        assert [target.terminal_id for target in presentation.staged] == ["%9"]
        app.action_kill()
        await pilot.pause()

    assert client.participants.terminated == []


@pytest.mark.asyncio
async def test_textual_prompts_kill_bus_and_safe_quit() -> None:
    app, client, presentation = _app()

    async with app.run_test() as pilot:
        await pilot.pause()
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
        assert isinstance(app.screen, SpawnDirectoryScreen)
        assert client.participants.spawned == []

        cwd_input = app.screen.query_one("#spawn-cwd")
        assert cwd_input.value == str(tmp_path)
        await pilot.press("x")  # the caret sits after the prefilled path, nothing selected
        assert cwd_input.value == f"{tmp_path}x"
        cwd_input.value = "proj"
        await pilot.press("tab")
        assert cwd_input.value == f"project with spaces{os.sep}"
        await pilot.press("enter")
        await pilot.pause()

    assert client.participants.spawned == [("codex", None, "manual")]
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
async def test_malformed_remote_catalog_falls_back_to_local_harnesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, _presentation = _app()
    fallback = HarnessCatalogEntry.from_wire(
        {
            "name": "claude",
            "binary": "claude",
            "binaries": [],
            "icon": "◆",
            "installed": True,
            "compatible": True,
            "supported_wiring": ["tmux"],
            "requires_terminal": True,
            "provider_ready": True,
            "launch_available": True,
            "approvals": ["manual"],
            "reason": None,
            "detail": None,
        }
    )

    async def malformed_catalog() -> object:
        raise TypeError("invalid catalog payload")

    monkeypatch.setattr(client.catalogs, "harnesses", malformed_catalog)
    monkeypatch.setattr("regie.app_parts.startup.local_harness_catalog", lambda: (fallback,))
    monkeypatch.setattr(
        _Presentation,
        "unmanaged_panes",
        lambda *_args, **_kwargs: _async_value(()),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._harnesses == (fallback,)
        assert "claude" in str(app.query_one("#dashboard-harnesses").render())


@pytest.mark.asyncio
async def test_projection_keeps_the_nearest_row_selected_when_one_disappears() -> None:
    app, _client, _presentation = _app()
    first = _participant("participant-1", name="first")
    second = _participant("participant-2", name="second")
    third = _participant("participant-3", name="third")
    initial = replace(
        _projection(),
        participants=MappingProxyType(
            {
                first.participant_id: first,
                second.participant_id: second,
                third.participant_id: third,
            }
        ),
    )
    state = _State(initial)
    app._state = cast(StateController, state)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.select_participant(second.participant_id)
        state.projection = replace(
            initial,
            participants=MappingProxyType(
                {first.participant_id: first, third.participant_id: third}
            ),
        )
        app._show_projection(state.projection)
        await pilot.pause()

        assert app.selected_participant_id == third.participant_id


@pytest.mark.asyncio
@pytest.mark.parametrize("initialized", [True, False])
async def test_state_refresh_failures_only_notify_when_initially_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    initialized: bool,
) -> None:
    app, _client, _presentation = _app()
    messages: list[str] = []
    monkeypatch.setattr(app, "notify", lambda message, **_kwargs: messages.append(str(message)))
    if not initialized:
        monkeypatch.setattr(app._state, "projection", None)

    error = FrontendTransportError("offline")
    app._show_state_error(error)
    app._show_state_error(error)
    assert messages == ([] if initialized else ["state unavailable: offline"])


@pytest.mark.parametrize(
    ("state", "severity"),
    [
        (ActionState.PENDING, None),
        (ActionState.SUCCEEDED, None),
        (ActionState.REFUSED, "warning"),
        (ActionState.UNCERTAIN, "warning"),
        (ActionState.FAILED, "error"),
    ],
)
def test_action_notifications_are_actionable_and_not_duplicated(monkeypatch, state, severity):
    app, _client, _presentation = _app()
    notes: list[str] = []
    monkeypatch.setattr(app, "notify", lambda _message, **kwargs: notes.append(kwargs["severity"]))
    monkeypatch.setattr(app._action_presentation, "begin_reconciliation", lambda _record: False)
    record = ActionRecord("terminate", "participant-1", "key", state=state)

    app._show_action(record)
    app._show_action(record)

    assert notes == ([] if severity is None else [severity])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_staged_termination_releases_focus_before_requesting_kill(monkeypatch, failure):
    app, client, presentation = _app()
    terminate = client.participants.terminate

    async def checked_terminate(participant_id, *, idempotency_key):
        assert presentation.staged == []
        return await terminate(participant_id, idempotency_key=idempotency_key)

    monkeypatch.setattr(client.participants, "terminate", checked_terminate)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_stage()
        await app._staging.focus()
        assert presentation.focused == presentation.staged
        if failure:

            async def failed_unstage(_target):
                raise RuntimeError("pane identity could not be verified")

            monkeypatch.setattr(presentation, "unstage_terminal", failed_unstage)
        record = await app.submit_termination("participant-1")

        assert client.participants.terminated == ([] if failure else ["participant-1"])
        assert record.state is (ActionState.REFUSED if failure else ActionState.SUCCEEDED)
        assert (app._staging.staged_target is not None) is failure


@pytest.mark.asyncio
@pytest.mark.parametrize("same_pane", [False, True])
async def test_termination_never_unstages_another_terminal_identity(same_pane):
    app, _client, presentation = _app()
    projection = _projection()
    participant = projection.participants["participant-1"]
    await app._staging.stage(participant, projection.providers)
    target = app._staging.staged_target
    if same_pane:
        route = participant.terminal_route
        assert route is not None
        participant = replace(
            participant,
            terminal_route=replace(
                route, identity=replace(route.identity, terminal_incarnation="replacement")
            ),
        )
    else:
        participant = projection.participants["participant-2"]

    assert await app._staging.unstage_participant(participant) is None
    assert presentation.staged == [target]


@pytest.mark.asyncio
async def test_textual_resume_uses_a_bounded_public_dead_session_and_trusted_context() -> None:
    app, client, _presentation = _app()
    resumable = PublicResumeCandidate.from_wire(
        {
            "participant_id": "dead-resumable",
            "harness": "codex",
            "name": "old session",
            "cwd": "/workspace/original",
            "resume_state": "resumable",
            "transcript_identity": {
                "state": "trusted",
                "session_id": "trusted-session",
                "provenance": "exact",
                "location": "/tmp/transcript-a",
                "domain": None,
                "detail": None,
            },
        }
    )
    unavailable = PublicResumeCandidate.from_wire(
        {
            "participant_id": "dead-untrusted",
            "harness": "codex",
            "name": "old untrusted session",
            "cwd": "/workspace/other",
            "resume_state": "untrusted",
            "transcript_identity": {
                "state": "untrusted",
                "session_id": "untrusted-session",
                "provenance": "heuristic",
                "location": "/tmp/transcript-b",
                "domain": None,
                "detail": "not verified",
            },
        }
    )
    client.participants.dead_rows = (resumable, unavailable)

    async with app.run_test() as pilot:
        await pilot.pause()
        discovery = await app.load_resume_sessions()
        assert client.participants.resume_calls == [{"limit": 20}]
        candidates = {candidate.participant_id: candidate for candidate in discovery.candidates}
        assert candidates["dead-untrusted"].reason == "transcript identity could not be verified"

        app.resume_dead_session(candidates["dead-resumable"])
        await wait_until(pilot, lambda: bool(client.participants.spawned))

        assert client.participants.spawned[0][:2] == ("codex", None)
        assert client.participants.spawn_options[0]["cwd"] == "/workspace/original"
        assert client.participants.spawn_options[0]["resume"] == "trusted-session"


@pytest.mark.asyncio
async def test_rejected_transcript_candidate_is_never_bound() -> None:
    app, client, _presentation = _app()
    candidate = TranscriptCandidate(
        "/tmp/rejected.jsonl",
        session_id="session-rejected",
        rejection_reason="candidate belongs to another workspace",
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        app._transcript_recovery_target = "participant-1"
        app.select_transcript_candidate(candidate)
        await pilot.pause()

    assert client.transcripts.bind_calls == []


@pytest.mark.asyncio
async def test_transcript_recovery_hides_for_trusted_identity_and_filters_rejections() -> None:
    app, client, _presentation = _app()
    trusted = _participant(
        "participant-1",
        name="first",
        transcript_identity={
            "state": "trusted",
            "session_id": "session-a",
            "provenance": "exact",
            "location": "/tmp/trusted.jsonl",
            "domain": None,
            "detail": None,
        },
    )
    state = _State(
        replace(
            _projection(),
            participants=MappingProxyType(
                {
                    trusted.participant_id: trusted,
                    "participant-2": _participant("participant-2", name="second"),
                }
            ),
        )
    )
    app._state = cast(StateController, state)
    accepted = TranscriptCandidate(
        "/tmp/accepted.jsonl",
        session_id="accepted",
        provenance="exact",
    )
    rejected = TranscriptCandidate(
        "/tmp/rejected.jsonl",
        session_id="rejected",
        rejection_reason="belongs to another participant",
    )
    client.transcripts.candidate_rows = (accepted, rejected)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert not app.transcript_recovery_available("participant-1")
        assert app.transcript_recovery_available("participant-2")
        app._transcript_recovery_target = "participant-2"
        assert await app.load_transcript_candidates() == (accepted,)


@pytest.mark.asyncio
async def test_transcript_recovery_palette_preserves_target_through_selection_and_cancel() -> None:
    app, client, _presentation = _app()
    candidate = TranscriptCandidate(
        "/tmp/accepted.jsonl",
        session_id="accepted",
        provenance="exact",
    )
    client.transcripts.candidate_rows = (candidate,)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_recover_transcript()
        await pilot.pause()
        assert app._transcript_recovery_target == "participant-1"

        palette = app.screen.query_one(CommandInput)
        palette.value = "accepted.jsonl"
        await pilot.press("enter")
        for _ in range(20):
            if client.transcripts.bind_calls:
                break
            await pilot.pause()

        assert client.transcripts.bind_calls[0]["participant_id"] == "participant-1"
        assert app._transcript_recovery_target is None
        assert app.focused is None

        app.action_recover_transcript()
        await pilot.pause()
        assert app._transcript_recovery_target == "participant-1"
        await pilot.press("escape")
        await pilot.pause()
        assert app._transcript_recovery_target is None
        assert app.focused is None


async def _async_value(value: object) -> object:
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_field", ["owner_id", "tombstone_id"])
async def test_transcript_transfer_requires_and_sends_exact_prior_owner(
    owner_field: str,
) -> None:
    app, client, _presentation = _app()
    prior_owner_id = f"prior-{owner_field}"
    candidate = TranscriptCandidate(
        "/tmp/transfer.jsonl",
        session_id="session-transfer",
        **{owner_field: prior_owner_id},
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        state = cast(_State, app._state)
        initial_snapshots = state.initialize_calls
        app._transcript_recovery_target = "participant-1"
        app.select_transcript_candidate(candidate)
        await pilot.pause()
        assert isinstance(app.screen, TranscriptTransferScreen)

        confirmation = app.screen.query_one("#transcript-transfer-confirmation")
        confirmation.value = "wrong-owner"
        await pilot.press("enter")
        assert isinstance(app.screen, TranscriptTransferScreen)
        assert client.transcripts.bind_calls == []

        confirmation.value = prior_owner_id
        await pilot.press("enter")
        await pilot.pause()
        assert state.initialize_calls == initial_snapshots + 1

    assert len(client.transcripts.bind_calls) == 1
    assert client.transcripts.bind_calls[0]["prior_owner_id"] == prior_owner_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_error",
    [
        FrontendTransportError("connection dropped after submission"),
        ResponseValidationError("invalid bind response"),
    ],
)
async def test_uncertain_transcript_bind_retries_original_parameters_and_key(
    first_error: Exception,
) -> None:
    client = _Client()
    bind_client = _Client()
    bind_client.transcripts.bind_outcomes = [
        first_error,
        SimpleNamespace(
            value=TranscriptBindResult(
                "participant-1",
                "/tmp/original.jsonl",
                prior_owner_id="prior-owner",
            )
        ),
    ]
    controller = TranscriptBindingController(
        cast(FrontendClient, client),
        client_factory=lambda: cast(FrontendClient, bind_client),
    )

    first = await controller.bind(
        "participant-1",
        "/tmp/original.jsonl",
        prior_owner_id="prior-owner",
    )
    assert first.state is TranscriptBindState.UNCERTAIN
    original_key = first.idempotency_key
    second = await controller.bind(
        "participant-1",
        "/tmp/original.jsonl",
        prior_owner_id="changed-owner",
    )

    assert first is second
    assert first.state is TranscriptBindState.SUCCEEDED
    assert [call["prior_owner_id"] for call in bind_client.transcripts.bind_calls] == [
        "prior-owner",
        "prior-owner",
    ]
    assert {call["idempotency_key"] for call in bind_client.transcripts.bind_calls} == {
        original_key
    }
    assert bind_client.closed


@pytest.mark.asyncio
async def test_concurrent_transcript_binds_receive_independent_clients() -> None:
    root = _Client()
    created: list[_Client] = []

    def factory() -> FrontendClient:
        client = _Client()
        created.append(client)
        return cast(FrontendClient, client)

    controller = TranscriptBindingController(
        cast(FrontendClient, root),
        client_factory=factory,
    )
    first, second = await asyncio.gather(
        controller.bind("participant-1", "/tmp/one.jsonl", prior_owner_id=None),
        controller.bind("participant-2", "/tmp/two.jsonl", prior_owner_id=None),
    )

    assert first.state is TranscriptBindState.SUCCEEDED
    assert second.state is TranscriptBindState.SUCCEEDED
    assert len(created) == 2
    assert created[0] is not created[1]
    assert [client.closed for client in created] == [True, True]


@pytest.mark.asyncio
async def test_terminated_trajectory_switches_immediately_when_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _client, _presentation = _app()

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("h")
        await app.query_one("#trajectory-view", TrajectoryView).wait_until_loaded()

        async def fail_initialize() -> StateProjection:
            raise FrontendTransportError("offline after termination")

        monkeypatch.setattr(app._state, "initialize", fail_initialize)
        await app._reconcile_completed_action(
            ActionRecord(
                "terminate",
                "participant-1",
                "terminate-key",
                participant_id="participant-1",
                state=ActionState.SUCCEEDED,
            )
        )

        assert app._surface.mode is SurfaceMode.DASHBOARD
        assert app.query_one("#catalog-dashboard").display is True
        assert app.query_one("#trajectory-view").display is False


@pytest.mark.asyncio
async def test_completed_spawn_is_selected_staged_and_focused(caplog) -> None:
    caplog.set_level("INFO", logger="regie")
    app, _client, presentation = _app()

    async with app.run_test() as pilot:
        await pilot.pause()
        state = cast(_State, app._state)
        spawned = _participant("participant-3", name="spawned")
        state.projection = replace(
            state.projection,
            participants=MappingProxyType(
                {**state.projection.participants, spawned.participant_id: spawned}
            ),
        )

        await app._reconcile_completed_action(
            ActionRecord(
                "spawn",
                "spawn-target",
                "spawn-key",
                participant_id=spawned.participant_id,
                state=ActionState.SUCCEEDED,
                operation_id="spawn-operation",
            )
        )
        await app.workers.wait_for_complete()
        for _ in range(50):  # staging runs on the presentation queue
            if presentation.focused:
                break
            await pilot.pause()

        assert app.selected_participant_id == spawned.participant_id
        assert ("p", spawned.participant_id) in app.query_one(ParticipantTree)._key_widgets
        assert [target.terminal_id for target in presentation.staged] == ["%3"]
        assert [target.terminal_id for target in presentation.focused] == ["%3"]
        phases = [
            row.message
            for row in caplog.records
            if row.name == "regie.latency" and row.message.startswith("action.spawn.")
        ]
        assert [line.split()[0] for line in phases] == [
            "action.spawn.snapshot",
            "action.spawn.projection",
            "action.spawn.unmanaged",
        ]
        assert all("operation=spawn-operation result=success" in line for line in phases)
        for _ in range(50):  # rendering is acknowledged after the next refresh
            if "after_projection_ms=" in caplog.text:
                break
            await pilot.pause()
        assert "after_projection_ms=" in caplog.text


@pytest.mark.asyncio
async def test_successful_durable_action_automatically_reconciles_the_tree(monkeypatch) -> None:
    app, client, _presentation = _app()
    finished = asyncio.Event()

    async def spawn(*args, **kwargs):
        return SimpleNamespace(
            value=AcceptedOperation(
                operation_id="operation-new", state="accepted", participant_id="participant-spawned"
            )
        )

    async def wait(*args, **kwargs):
        await finished.wait()
        return SimpleNamespace(
            value=SimpleNamespace(
                timed_out=False, operation=SimpleNamespace(state="succeeded", error=None)
            )
        )

    monkeypatch.setattr(client.participants, "spawn", spawn)
    client.operations = SimpleNamespace(wait=wait)

    async with app.run_test() as pilot:
        await pilot.pause()
        state = cast(_State, app._state)
        initial_snapshots = state.initialize_calls
        spawned = _participant("participant-spawned", name="spawned")
        state.projection = replace(
            state.projection,
            participants=MappingProxyType(
                {**state.projection.participants, spawned.participant_id: spawned}
            ),
        )

        app._start_action(app.submit_spawn("codex", "", "manual", cwd="/workspace"))
        await pilot.pause()
        assert state.initialize_calls == initial_snapshots
        finished.set()
        for _ in range(20):
            if state.initialize_calls > initial_snapshots:
                break
            await pilot.pause()

        assert state.initialize_calls == initial_snapshots + 1
        assert ("p", spawned.participant_id) in app.query_one(ParticipantTree)._key_widgets
        assert len(app._action_presentation.reconciled) == 1


@pytest.mark.asyncio
async def test_explicit_kill_removes_an_already_retiring_child() -> None:
    app, _client, _presentation = _app()
    child = _participant("participant-3", name="child", parent_id="participant-1")
    state = cast(_State, app._state)
    state.projection = replace(
        state.projection, participants=MappingProxyType({child.participant_id: child})
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one(ParticipantTree)
        tree._leaf_retirement._enabled = True
        state.projection = replace(state.projection, participants=MappingProxyType({}))
        app._show_projection(state.projection)
        key = ("p", child.participant_id)
        assert key in tree._retiring
        tree.remove_without_animation(child.participant_id)
        assert key not in tree._retiring
        assert not tree._leaf_retirement.active
        await pilot.pause()
        assert not tree.query(AgentLeaf)


@pytest.mark.asyncio
async def test_completed_action_retries_reconciliation_after_a_refresh_failure() -> None:
    app, _client, _presentation = _app()

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._startup_task is not None
        await app._startup_task
        state = cast(_State, app._state)
        attempts = 0

        async def initialize() -> StateProjection:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise FrontendTransportError("refresh failed")
            return state.projection

        state.initialize = initialize  # type: ignore[method-assign]
        record = await app.submit_spawn("codex", "", "manual", cwd="/workspace")
        app._show_action(record)
        for _ in range(20):
            await pilot.pause()
            if not app._action_presentation.reconciling(record):
                break

        assert attempts == 1
        assert record.idempotency_key not in app._action_presentation.reconciled

        app._render_pending_actions()
        for _ in range(20):
            await pilot.pause()
            if record.idempotency_key in app._action_presentation.reconciled:
                break

        assert attempts == 2
        assert record.idempotency_key in app._action_presentation.reconciled
        assert not app._action_presentation.reconciling(record)


@pytest.mark.asyncio
async def test_orphaned_agent_child_keeps_spawn_animation_provenance() -> None:
    app, _client, _presentation = _app()
    orphan = _participant("participant-3", name="orphan", parent_id="missing-parent")
    state = _State(
        replace(
            _projection(),
            participants=MappingProxyType({orphan.participant_id: orphan}),
        )
    )
    app._state = cast(StateController, state)

    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one(ParticipantTree)
        key = ("p", orphan.participant_id)
        assert key in tree._animate_new
        assert tree._leaf_retirement._active[key] is True


def test_kill_binding_is_visible_with_the_rc9_label() -> None:
    binding = next(binding for binding in RegieApp.BINDINGS if binding.key == "x")

    assert binding.action == "kill"
    assert binding.description == "kill"
    assert binding.show


@pytest.mark.asyncio
async def test_invalid_display_settings_report_rc9_config_guidance() -> None:
    app, _client, _presentation = _app()
    app.settings = replace(app.settings, theme="missing-theme", cost_window="fortnight")
    notes: list[tuple[str, dict[str, object]]] = []
    app.notify = (  # type: ignore[method-assign]
        lambda message, **kwargs: notes.append((str(message), kwargs))
    )

    async with app.run_test() as pilot:
        await pilot.pause()

    theme_message, theme_options = next(note for note in notes if "unknown theme" in note[0])
    window_message, window_options = next(note for note in notes if "cost_window" in note[0])
    assert theme_message.startswith("unknown theme 'missing-theme' — available: ")
    assert window_message == (
        "unknown cost_window 'fortnight' — using 'day'. available: day, month, week, year"
    )
    assert theme_options == {"title": "config", "severity": "warning", "timeout": 10}
    assert window_options == {"title": "config", "severity": "warning", "timeout": 10}


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
        await pilot.pause()
        tree = app.query_one(ParticipantTree)
        panel = app.query_one(UsageBreakdownPanel)
        await pilot.press("j", "j")
        assert app._usage_panel.keyboard_metric == "input"
        assert panel.has_class("-visible")
        assert not list(tree.query(".tree-cursor"))
        await pilot.press("x")
        assert client.participants.terminated == []

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
async def test_usage_footer_starts_hidden_and_dollar_toggles_only_the_footer() -> None:
    app, _client, _presentation = _app(usage_visible=False)
    footers = ("#usage-period", "#stats-footer", "#price-footer")

    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        assert not any(app.query_one(footer).display for footer in footers)
        # With the footer hidden, moving past the last row must not enter it.
        await pilot.press("j", "j", "j")
        assert not app._usage_panel.in_footer

        await pilot.press("dollar_sign")
        assert all(app.query_one(footer).display for footer in footers)
        await pilot.press("j")
        assert app._usage_panel.in_footer

        await pilot.press("dollar_sign")
        assert not any(app.query_one(footer).display for footer in footers)
        assert not app._usage_panel.in_footer
        assert not app.query_one(UsageBreakdownPanel).has_class("-visible")


@pytest.mark.asyncio
async def test_agent_cost_shows_on_the_selected_row_only_even_with_the_footer_hidden() -> None:
    app, client, _presentation = _app(usage_visible=False)

    async with app.run_test(size=(100, 36)) as pilot:
        tree = app.query_one(ParticipantTree)

        def row(participant_id: str) -> str:
            leaf = tree._key_widgets.get(("p", participant_id))
            return str(leaf.render()) if isinstance(leaf, AgentLeaf) else ""

        await wait_until(pilot, lambda: tree.selected_participant_id is not None, 5.0)
        if tree.selected_participant_id != "participant-1":
            await pilot.press("k")
        await wait_until(pilot, lambda: "$0.42" in row("participant-1"), 5.0)
        assert client.usage.by_participant_calls[0]["participant_ids"] == (
            "participant-1",
            "participant-2",
        )

        await pilot.press("j")
        await wait_until(pilot, lambda: tree.selected_participant_id == "participant-2", 5.0)
        assert "$0.42" not in row("participant-1")


class _LateState(_State):
    """Like the real controller: no projection until the initial snapshot lands."""

    def __init__(self, projection: StateProjection) -> None:
        super().__init__(projection)
        self._loaded = projection
        self.projection = None

    async def initialize(self) -> StateProjection:
        await asyncio.sleep(0.2)
        self.projection = self._loaded
        return await super().initialize()


@pytest.mark.asyncio
async def test_startup_usage_waits_for_participants_so_costs_appear_immediately() -> None:
    app, client, _presentation = _app(usage_visible=False)
    app._state = cast(StateController, _LateState(_projection()))

    async with app.run_test(size=(100, 36)) as pilot:
        tree = app.query_one(ParticipantTree)

        def row() -> str:
            leaf = tree._key_widgets.get(("p", "participant-1"))
            return str(leaf.render()) if isinstance(leaf, AgentLeaf) else ""

        # Well inside the 10 s poll: the first usage read must already carry the agents.
        await wait_until(pilot, lambda: "$0.42" in row(), 4.0)
        assert client.usage.by_participant_calls[0]["participant_ids"] == (
            "participant-1",
            "participant-2",
        )


@pytest.mark.asyncio
async def test_hovering_an_unselected_agent_shows_its_cost_until_the_pointer_leaves() -> None:
    app, _client, _presentation = _app(usage_visible=False)

    async with app.run_test(size=(100, 36)) as pilot:
        tree = app.query_one(ParticipantTree)

        def leaf(participant_id: str) -> AgentLeaf | None:
            widget = tree._key_widgets.get(("p", participant_id))
            return widget if isinstance(widget, AgentLeaf) else None

        def row(participant_id: str) -> str:
            widget = leaf(participant_id)
            return str(widget.render()) if widget is not None else ""

        await wait_until(pilot, lambda: "$0.42" in row("participant-1"), 5.0)
        await pilot.press("j")
        await wait_until(pilot, lambda: tree.selected_participant_id == "participant-2", 5.0)
        assert "$0.42" not in row("participant-1")

        await pilot.hover(leaf("participant-1"))
        await wait_until(pilot, lambda: "$0.42" in row("participant-1"), 5.0)

        await pilot.hover(leaf("participant-2"))
        await wait_until(pilot, lambda: "$0.42" not in row("participant-1"), 5.0)


@pytest.mark.asyncio
async def test_selected_agent_cost_counts_up_like_the_footer_and_others_just_update() -> None:
    app, client, _presentation = _app(usage_visible=False)

    async with app.run_test(size=(100, 36)) as pilot:
        tree = app.query_one(ParticipantTree)

        def row(participant_id: str) -> str:
            leaf = tree._key_widgets.get(("p", participant_id))
            return str(leaf.render()) if isinstance(leaf, AgentLeaf) else ""

        await wait_until(pilot, lambda: tree.selected_participant_id is not None, 5.0)
        assert tree.selected_participant_id == "participant-1"
        # Startup: the first cost counts up from zero, as the footer does.
        await wait_until(pilot, lambda: "$0." in row("participant-1"), 5.0)
        assert "$0.42" not in row("participant-1")
        await wait_until(pilot, lambda: "$0.42" in row("participant-1"), 5.0)

        # Selected: the count passes through intermediate values before settling.
        client.usage.participant_cost_microcents = 142_000_000
        await app._refresh_usage()
        await wait_until(
            pilot,
            lambda: "$0.42" not in row("participant-1") and "$1.42" not in row("participant-1"),
            5.0,
        )
        await wait_until(pilot, lambda: "$1.42" in row("participant-1"), 5.0)

        # Unselected: the value is adopted silently and shows as is on selection.
        await pilot.press("j")
        client.usage.participant_cost_microcents = 242_000_000
        await app._refresh_usage()
        await pilot.press("k")
        await wait_until(pilot, lambda: tree.selected_participant_id == "participant-1", 5.0)
        assert "$2.42" in row("participant-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [FrontendTransportError("footer poll failed"), TypeError("malformed usage")],
)
async def test_usage_footer_poll_failure_does_not_contaminate_overlay_cache(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    app, _client, _presentation = _app()

    async with app.run_test() as pilot:
        await pilot.pause()
        app._usage_panel.active_metric = "input"
        app._usage_panel.breakdown = {"harnesses": [{"harness": "codex"}]}
        app._usage_panel.message = None

        async def fail_refresh(*, window: str, participant_ids: tuple[str, ...]) -> object:
            del window, participant_ids
            raise error

        monkeypatch.setattr(app._usage, "refresh", fail_refresh)
        await app._refresh_usage()

        assert app._usage_panel.breakdown == {"harnesses": [{"harness": "codex"}]}
        assert app._usage_panel.message is None


@pytest.mark.asyncio
async def test_configured_usage_period_survives_an_initial_refresh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _client, _presentation = _app()
    app.settings = replace(app.settings, cost_window="month")

    async def fail_refresh(*, window: str, participant_ids: tuple[str, ...]) -> object:
        assert window == "month"
        del participant_ids
        raise FrontendTransportError("usage unavailable")

    monkeypatch.setattr(app._usage, "refresh", fail_refresh)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert str(app.query_one("#usage-period").render()) == "this month"


@pytest.mark.asyncio
async def test_malformed_bus_pages_are_contained_by_both_pollers(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _client, _presentation = _app()
    messages: list[str] = []
    caplog.set_level("DEBUG", logger="regie")

    async with app.run_test() as pilot:
        await pilot.pause()
        app._bus_visible = True
        monkeypatch.setattr(app, "notify", lambda message, **_kwargs: messages.append(str(message)))

        async def malformed() -> object:
            raise TypeError("malformed diagnostic page")

        monkeypatch.setattr(app._bus, "poll", malformed)
        monkeypatch.setattr(app._animation_bus, "poll", malformed)
        await app._refresh_bus()
        await app._refresh_animations()

    assert messages == []
    assert "diagnostic bus unavailable: malformed diagnostic page" in caplog.text


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


@pytest.mark.asyncio
async def test_repeated_palette_and_inspection_reads_serialize_each_sdk_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, _presentation = _app()
    active = dict.fromkeys(("catalog", "controls", "resume", "transcripts"), 0)
    maximum = dict(active)

    async def observed(name: str, value: object) -> object:
        active[name] += 1
        maximum[name] = max(maximum[name], active[name])
        await asyncio.sleep(0.01)
        active[name] -= 1
        return value

    entry = (await client.catalogs.harnesses()).value.items[0]
    candidate = TranscriptCandidate(
        "/tmp/candidate.jsonl",
        session_id="candidate-session",
        provenance="exact",
    )

    async def catalog() -> object:
        return await observed("catalog", SimpleNamespace(value=SimpleNamespace(items=(entry,))))

    async def controls(_participant_id: str) -> object:
        return await observed(
            "controls",
            SimpleNamespace(value=SimpleNamespace(extra={}, actions={})),
        )

    async def resume_candidates(**_params: object) -> object:
        return await observed(
            "resume",
            SimpleNamespace(value=SimpleNamespace(items=(), next_cursor=None)),
        )

    async def transcript_candidates(_participant_id: str) -> object:
        return await observed(
            "transcripts",
            SimpleNamespace(value=SimpleNamespace(items=(candidate,))),
        )

    monkeypatch.setattr(client.catalogs, "harnesses", catalog)
    monkeypatch.setattr(client.controls, "get", controls)
    monkeypatch.setattr(client.participants, "resume_candidates", resume_candidates)
    monkeypatch.setattr(client.transcripts, "candidates", transcript_candidates)

    async with app.run_test() as pilot:
        await pilot.pause()
        app._transcript_recovery_target = "participant-1"
        await asyncio.gather(
            app._load_catalog(),
            app._load_catalog(),
            app._show_controls("participant-1"),
            app._show_controls("participant-2"),
            app.load_resume_sessions(),
            app.load_resume_sessions(),
            app.load_transcript_candidates(),
            app.load_transcript_candidates(),
        )

    assert maximum == dict.fromkeys(maximum, 1)


@pytest.mark.asyncio
async def test_harnesses_without_an_executable_are_not_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, _presentation = _app()
    installed = (await client.catalogs.harnesses()).value.items[0]
    missing = HarnessCatalogEntry.from_wire(
        {
            **{name: getattr(installed, name) for name in ("supported_wiring", "approvals")},
            "name": "pi",
            "binary": "pi",
            "installed": False,
            "compatible": True,
            "requires_terminal": True,
            "provider_ready": True,
            "launch_available": False,
            "reason": "executable not found",
            "detail": None,
        }
    )

    async def catalog() -> object:
        return SimpleNamespace(value=SimpleNamespace(items=(installed, missing)))

    monkeypatch.setattr(client.catalogs, "harnesses", catalog)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.wait_for_catalog()

        assert [choice.harness for choice in app._spawn_choices()] == ["codex"]
        assert all(entry.name != "pi" for entry in app._installed_harnesses)
        assert app.icon_for_harness("codex") == "◈"  # the full catalog still names icons
        rows = app.query_one(WelcomeDashboard)._harnesses or []
        assert [row["name"] for row in rows] == ["codex"]


async def test_rename_key_edits_the_selected_alias_and_submits_once() -> None:
    app, client, _presentation = _app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await wait_until(pilot, lambda: bool(app.query(NameEditor)))
        editor = app.query_one(NameEditor)
        assert editor.value == "first"
        await pilot.press(*"renamed")
        await pilot.press("enter")
        await wait_until(pilot, lambda: not app.query(NameEditor))
        await wait_until(pilot, lambda: len(client.participants.renames) == 1)
    [rename] = client.participants.renames
    assert rename["participant_id"] == "participant-1"
    assert rename["name"] == "renamed"
    assert isinstance(rename["idempotency_key"], str) and len(rename["idempotency_key"]) == 32


async def test_rename_escape_cancels_without_a_call() -> None:
    app, client, _presentation = _app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await wait_until(pilot, lambda: bool(app.query(NameEditor)))
        await pilot.press("escape")
        await wait_until(pilot, lambda: not app.query(NameEditor))
        await pilot.pause()
    assert client.participants.renames == []


async def test_refused_rename_surfaces_the_daemon_message_and_keeps_the_old_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, _presentation = _app()
    client.participants.rename_error = FrontendResponseError(
        Response(
            request_id=1,
            ok=False,
            error=ErrorValue("name_taken", "another live participant already uses that name"),
        )
    )
    notes: list[tuple[str, str]] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            app,
            "notify",
            lambda message, **kwargs: notes.append((str(message), str(kwargs["severity"]))),
        )
        await pilot.press("r")
        await wait_until(pilot, lambda: bool(app.query(NameEditor)))
        await pilot.press(*"second")
        await pilot.press("enter")
        await wait_until(pilot, lambda: not app.query(NameEditor))
        await wait_until(pilot, lambda: len(client.participants.renames) == 1)
        await pilot.pause()
        assert any("name_taken" in message and severity == "warning" for message, severity in notes)
        row = str(app.query_one(ParticipantTree).tree_lines[0][0])
        assert "first" in row and "second" not in row


async def test_clicking_the_name_opens_the_editor_and_adjacent_cells_do_not() -> None:
    app, client, _presentation = _app()
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one(ParticipantTree)
        leaf = tree._key_widgets[("p", "participant-1")]
        row2 = tree.tree_lines[0][0].plain.splitlines()[1]
        await pilot.click(AgentLeaf, offset=Offset(leaf.gutter.left + row2.index("first"), 1))
        await wait_until(pilot, lambda: bool(app.query(NameEditor)))
        assert app.query_one(NameEditor).value == "first"
        await pilot.press("escape")
        await wait_until(pilot, lambda: not app.query(NameEditor))

        await pilot.click(AgentLeaf, offset=Offset(leaf.gutter.left + row2.index("◈"), 1))
        await pilot.pause()
        assert not app.query(NameEditor)
    assert client.participants.renames == []


async def test_rename_editor_survives_a_projection_refresh() -> None:
    app, _client, _presentation = _app()
    state = cast(_State, app._state)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await wait_until(pilot, lambda: bool(app.query(NameEditor)))
        editor = app.query_one(NameEditor)
        await pilot.press(*"bet")
        before = editor.styles.offset
        child = replace(state.projection.participants["participant-1"], parent_id="participant-2")
        state.projection = replace(
            state.projection,
            participants=MappingProxyType(
                {**state.projection.participants, "participant-1": child}
            ),
        )
        await app._tick_synchronize()
        await pilot.pause()
        assert app.query_one(NameEditor) is editor
        assert editor.value == "bet"
        assert editor.has_focus
        assert editor.styles.offset.x > before.x


def test_partially_revealed_name_has_no_span_until_clipped_in() -> None:
    node = {
        "id": "participant-1",
        "name": "first",
        "harness": "codex",
        "status": "idle",
        "icon": "\u0301",  # one codepoint, zero cells: reveal counts codepoints, columns are cells
    }
    assert visible_name_span(node, reveal=9) is None
    assert visible_name_span(node, reveal=10) == (8, 9)
