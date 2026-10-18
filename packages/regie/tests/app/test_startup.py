"""App construction, startup ordering, shutdown and projection refresh."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import replace
from types import MappingProxyType
from typing import cast

import pytest
from regie.app import RegieApp
from regie.contracts import PresentationTarget, RegieSettings
from regie.controllers.actions import ActionRecord, ActionState
from regie.state import StateController
from regie.widgets import ParticipantTree

from packages.regie.tests.support.app_fakes import (
    FakeState,
    build_app,
    make_participant,
    make_projection,
)
from theater.frontend import (
    EventCursor,
    FrontendClient,
    FrontendTransportError,
)


async def test_startup_reveals_state_before_catalog_discovery_and_usage(  # noqa: PLR0915
    monkeypatch,
    caplog,
) -> None:
    app, client, _presentation = build_app()
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
        assert sorted(timers) == ["_refresh_local_projection", "usage"]  # bus reads long-poll
        assert any(message.startswith("startup.ready ") for message in caplog.messages)
        assert app.selected_participant_id == "participant-2"


@pytest.mark.parametrize("reader", ["follow", "action"])
async def test_late_projection_work_renders_the_latest_installed_state(monkeypatch, reader):
    app, _client, _presentation = build_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        fresh = replace(app._state.projection, cursor=EventCursor("stream-a", 2))
        shown = []

        async def auxiliary_read(make_projection, **_kwargs):
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


async def test_unchanged_projection_skips_redundant_tree_render(monkeypatch):
    app, _client, _presentation = build_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._startup_task is not None
        await app._startup_task
        await pilot.pause()
        tree = app.query_one(ParticipantTree)
        calls = []
        real_show = tree.show_projection

        def counted_show(*args, **kwargs):
            calls.append(args)
            return real_show(*args, **kwargs)

        monkeypatch.setattr(tree, "show_projection", counted_show)
        state = cast(FakeState, app._state)
        assert state.projection is not None
        installed = state.projection

        # A follow update with identical inputs must not re-render the tree.
        await app._synchronize_projection()
        await pilot.pause()
        assert len(calls) == 0

        # The tree reads neither the cursor nor providers: neither alone re-renders.
        advanced = replace(installed, cursor=EventCursor("stream-a", 2))
        provider = next(iter(advanced.providers.values()))
        state.projection = replace(
            advanced,
            providers=MappingProxyType({provider.provider_id: replace(provider, generation=2)}),
        )
        await app._synchronize_projection()
        await pilot.pause()
        assert len(calls) == 0

        busier = replace(
            advanced,
            participants=MappingProxyType(
                {
                    **advanced.participants,
                    "participant-1": replace(
                        advanced.participants["participant-1"], status="working"
                    ),
                }
            ),
        )
        state.projection = busier
        await app._synchronize_projection()
        await pilot.pause()
        assert len(calls) == 1

        state.projection = replace(busier, cursor=EventCursor("stream-a", 3))
        await app._synchronize_projection()
        await pilot.pause()
        assert len(calls) == 1


@pytest.mark.parametrize("reader", ["state", "usage", "bus"])
async def test_late_reader_does_not_paint_after_shutdown(monkeypatch, reader):
    app, _client, _presentation = build_app()
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
    app, client, presentation = build_app()
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
    app, _client, _presentation = build_app()
    initialize = app._state.initialize
    state = cast(FakeState, app._state)
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
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        await pilot.pause()
        [first, *_rest] = app.query_one(ParticipantTree).tree_lines

    assert "◈" in str(first[0])


@pytest.mark.asyncio
async def test_projection_keeps_the_nearest_row_selected_when_one_disappears() -> None:
    app, _client, _presentation = build_app()
    first = make_participant("participant-1", name="first")
    second = make_participant("participant-2", name="second")
    third = make_participant("participant-3", name="third")
    initial = replace(
        make_projection(),
        participants=MappingProxyType(
            {
                first.participant_id: first,
                second.participant_id: second,
                third.participant_id: third,
            }
        ),
    )
    state = FakeState(initial)
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
    app, _client, _presentation = build_app()
    messages: list[str] = []
    monkeypatch.setattr(app, "notify", lambda message, **_kwargs: messages.append(str(message)))
    if not initialized:
        monkeypatch.setattr(app._state, "projection", None)

    error = FrontendTransportError("offline")
    app._show_state_error(error)
    app._show_state_error(error)
    assert messages == ([] if initialized else ["state unavailable: offline"])


@pytest.mark.asyncio
async def test_orphaned_agent_child_keeps_spawn_animation_provenance() -> None:
    app, _client, _presentation = build_app()
    orphan = make_participant("participant-3", name="orphan", parent_id="missing-parent")
    state = FakeState(
        replace(
            make_projection(),
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


@pytest.mark.asyncio
async def test_invalid_display_settings_report_rc9_config_guidance() -> None:
    app, _client, _presentation = build_app()
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


class Client:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"RegieApp construction unexpectedly accessed client.{name}")


class Presentation:
    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def can_stage(self, target: PresentationTarget) -> tuple[bool, str | None]:
        return False, "fixture"

    async def target_window(self) -> str:
        raise AssertionError("construction must not discover a target window")

    async def terminal_exists(self, target: PresentationTarget) -> bool:
        return False

    async def stage_terminal(self, target: PresentationTarget, *, target_window: str) -> None:
        raise AssertionError("construction must not stage a terminal")

    async def unstage_terminal(self, target: PresentationTarget) -> None:
        raise AssertionError("construction must not unstage a terminal")

    async def focus_terminal(self, target: PresentationTarget) -> None:
        raise AssertionError("construction must not focus a terminal")

    async def resize_regie(self, *, width: int) -> None:
        raise AssertionError("construction must not resize the Régie pane")

    async def resize_pane(
        self,
        pane_id: str,
        *,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        raise AssertionError("construction must not resize a pane")


def test_regie_app_requires_explicit_public_client_settings_and_presentation() -> None:
    app = RegieApp(
        client=cast(FrontendClient, Client()),
        settings=RegieSettings(),
        presentation=Presentation(),
    )

    assert app.projection is None
    assert app.settings.sidebar_width == 52


def test_regie_keeps_the_hidden_high_priority_tmux_return_signal() -> None:
    binding = next(item for item in RegieApp.BINDINGS if item.key == "ctrl+g")

    assert binding.action == "return_to_tree"
    assert binding.show is False
    assert binding.priority is True


def test_startup_imports_defer_bridge_worker_and_optional_trajectory_widgets():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import regie.cli, regie.app, sys; "
            "assert 'regie.bridge.runtime' not in sys.modules; "
            "assert 'regie.trajectory.rich.view' not in sys.modules; "
            "from regie.trajectory import TrajectoryView; "
            "from regie.trajectory.rich.view import TrajectoryView as View; "
            "assert TrajectoryView is View",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
