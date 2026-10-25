"""Staging, focus, kill and quit keys, and the terminal identity they act on."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import MappingProxyType
from typing import cast

import pytest
from regie.app import RegieApp
from regie.contracts import UnmanagedPane
from regie.controllers.actions import ActionRecord, ActionState
from regie.controllers.staging import StageOutcome, StageResult
from regie.controllers.surface import SurfaceMode
from regie.trajectory.rich import TrajectoryView
from regie.widgets import ParticipantTree
from regie.widgets.command_bar import CommandBar
from textual.geometry import Offset

from packages.regie.tests.support.app_fakes import (
    FakeState,
    build_app,
    make_projection,
)
from packages.regie.tests.support.waiting import wait_until
from theater.frontend import (
    FrontendTransportError,
    StateProjection,
)


async def test_slow_stage_keeps_navigation_responsive_and_captures_queued_targets(monkeypatch):
    app, _client, presentation = build_app()
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
    app, client, presentation = build_app()

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
    app, _client, presentation = build_app()
    state = cast(FakeState, app._state)
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
    app, _client, _presentation = build_app()
    state = cast(FakeState, app._state)
    state.projection = replace(state.projection, participants=MappingProxyType({}))
    messages: list[str] = []
    app.notify = lambda message, **_kwargs: messages.append(str(message))  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_stage_and_focus_trajectory()

    assert messages == ["nothing to inspect"]


@pytest.mark.asyncio
async def test_focused_trajectory_keeps_its_keys_and_leaves_on_return_signal() -> None:
    app, _client, presentation = build_app()

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
    app, _client, presentation = build_app()

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
async def test_stage_failure_notifies_the_failure_message_with_error_severity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _client, _presentation = build_app()
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
    app, _client, presentation = build_app()

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
    app, client, presentation = build_app()
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
    app, client, presentation = build_app()

    async with app.run_test() as pilot:
        await pilot.pause()
        assert "Keys" not in {command.title for command in app.get_system_commands(app.screen)}

        bar = app.query_one(CommandBar)
        await pilot.press("s")
        await wait_until(pilot, lambda: bar.is_open)
        assert app.screen is app.screen_stack[0]  # no floating window
        assert "send → first" in str(bar.query_one("#command-bar-title").render())
        await pilot.press(*"hello", "enter")
        await wait_until(pilot, lambda: not bar.is_open)
        await pilot.pause()
        assert client.controls.requests == [("send", "participant-1", "hello")]

        await pilot.press("i")
        await pilot.pause()
        assert client.controls.requests[-1] == ("interrupt", "participant-1", None)

        await pilot.press("f")
        await wait_until(pilot, lambda: bar.is_open)
        await pilot.press(*"after this", "enter")
        await pilot.pause()
        assert client.controls.requests[-1] == ("queue_followup", "participant-1", "after this")

        await pilot.press("s")
        await wait_until(pilot, lambda: bar.is_open)
        await pilot.press("j", "escape")  # typed keys stay in the bar; esc sends nothing
        await wait_until(pilot, lambda: not bar.is_open)
        await pilot.press("s")
        await wait_until(pilot, lambda: bar.is_open)
        await pilot.click("#right-surface", offset=Offset(4, 4))  # a click elsewhere leaves it
        await wait_until(pilot, lambda: not bar.is_open)
        assert len(client.controls.requests) == 3

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
@pytest.mark.parametrize("failure", [False, True])
async def test_staged_termination_releases_focus_before_requesting_kill(monkeypatch, failure):
    app, client, presentation = build_app()
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
    app, _client, presentation = build_app()
    projection = make_projection()
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
async def test_completed_action_currently_unstages_after_a_concurrent_stage(monkeypatch) -> None:
    app, _client, presentation = build_app()
    checked, release = asyncio.Event(), asyncio.Event()
    unstage = app._staging.unstage

    async def delayed_unstage() -> StageResult:
        checked.set()
        await release.wait()
        return await unstage()

    monkeypatch.setattr(app._staging, "unstage", delayed_unstage)
    async with asyncio.timeout(5), app.run_test() as pilot:
        try:
            await pilot.pause()
            projection = app.projection
            assert projection is not None
            first = projection.participants["participant-1"]
            second = projection.participants["participant-2"]
            assert (
                await app._staging.stage(first, projection.providers)
            ).outcome is StageOutcome.STAGED
            reconciliation = asyncio.create_task(
                app._reconcile_completed_action(
                    ActionRecord(
                        "terminate",
                        "participant-1",
                        "terminate-key",
                        participant_id="participant-1",
                        state=ActionState.SUCCEEDED,
                    )
                )
            )
            await checked.wait()
            assert (
                await app._staging.stage(second, projection.providers)
            ).outcome is StageOutcome.STAGED
            assert app._staging.staged_target is not None
            assert app._staging.staged_target.terminal_id == "%2"
            release.set()
            await reconciliation
            assert app._staging.staged_target is None
            assert presentation.staged == []
        finally:
            release.set()


@pytest.mark.asyncio
async def test_terminated_trajectory_switches_immediately_when_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _client, _presentation = build_app()

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


def test_kill_binding_is_visible_with_the_kill_label() -> None:
    bindings = [binding for binding in RegieApp.BINDINGS if binding.key == "x"]
    assert (
        bindings[0].action == "menu('close')"
    )  # first: it acts only while the cursor is in the menu
    binding = next(binding for binding in bindings if binding.action == "kill")

    assert binding.action == "kill"
    assert binding.description == "kill"
    assert binding.show
