"""Action results in the UI: notifications, tree reconciliation and pointer actions."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace
from typing import cast

import pytest
from regie.controllers.actions import ActionRecord, ActionState
from regie.state import StateController
from regie.widgets import ParticipantTree
from regie.widgets.leaf import AgentLeaf

from packages.regie.tests.support.app_fakes import (
    FakeState,
    build_app,
    make_participant,
    make_projection,
)
from theater.frontend import (
    AcceptedOperation,
    FrontendTransportError,
    Operation,
    StateProjection,
)


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
    app, _client, _presentation = build_app()
    notes: list[str] = []
    monkeypatch.setattr(app, "notify", lambda _message, **kwargs: notes.append(kwargs["severity"]))
    monkeypatch.setattr(app._action_presentation, "begin_reconciliation", lambda _record: False)
    record = ActionRecord("terminate", "participant-1", "key", state=state)

    app._show_action(record)
    app._show_action(record)

    assert notes == ([] if severity is None else [severity])


@pytest.mark.asyncio
async def test_successful_durable_action_automatically_reconciles_the_tree(monkeypatch) -> None:
    app, client, _presentation = build_app()
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
                timed_out=False,
                operation=Operation.from_wire(
                    {
                        "operation_id": "operation-new",
                        "kind": "spawn",
                        "state": "succeeded",
                        "phase": "completed",
                        "actor": {"client_id": "client-a"},
                    }
                ),
            )
        )

    monkeypatch.setattr(client.participants, "spawn", spawn)
    client.operations = SimpleNamespace(wait=wait)

    async with app.run_test() as pilot:
        await pilot.pause()
        state = cast(FakeState, app._state)
        initial_snapshots = state.initialize_calls
        spawned = make_participant("participant-spawned", name="spawned")
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
    app, _client, _presentation = build_app()
    child = make_participant("participant-3", name="child", parent_id="participant-1")
    state = cast(FakeState, app._state)
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
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._startup_task is not None
        await app._startup_task
        state = cast(FakeState, app._state)
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
async def test_rich_leaves_reconcile_animations_and_pointer_actions() -> None:
    app, _client, presentation = build_app()
    first = make_participant(
        "participant-1",
        name="first",
        status="working",
        cwd="/workspace/first",
        description="a deliberately long participant description " * 4,
    )
    second = make_participant("participant-2", name="second", cwd="/workspace/second")
    projection = replace(
        make_projection(),
        participants=MappingProxyType({first.participant_id: first, second.participant_id: second}),
    )
    state = FakeState(projection)
    app._state = cast(StateController, state)

    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        tree = app.query_one(ParticipantTree)
        first_leaf = tree._key_widgets[("p", first.participant_id)]
        assert isinstance(first_leaf, AgentLeaf)
        assert len(str(first_leaf.render()).splitlines()) == 3
        assert first_leaf._spinner_sub is not None
        assert first_leaf._marquee_sub is not None

        previous_frame = str(first_leaf.render())
        first_leaf._tick()
        assert str(first_leaf.render()) != previous_frame

        updated_first = make_participant(
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
        assert first_leaf._spinner_sub is None
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
