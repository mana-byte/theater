"""Renaming a participant from the tree: key, click, refusal and projection refresh."""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType
from typing import cast

import pytest
from regie.render.glyphs import visible_name_span
from regie.widgets import ParticipantTree
from regie.widgets.leaf import AgentLeaf
from regie.widgets.name_editor import NameEditor
from textual.geometry import Offset

from packages.regie.tests.support.app_fakes import (
    FakeState,
    build_app,
)
from packages.regie.tests.support.waiting import wait_until
from theater.frontend import (
    ErrorValue,
    FrontendResponseError,
)
from theater.frontend.dto import Response


async def test_rename_key_edits_the_selected_alias_and_submits_once() -> None:
    app, client, _presentation = build_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await wait_until(pilot, lambda: bool(app.query(NameEditor)))
        editor = app.query_one(NameEditor)
        assert editor.value == "first"
        assert editor.styles.background.a == 0  # transparent: nothing hidden behind it
        leaf = app.query_one(ParticipantTree)._key_widgets[("p", "participant-1")]
        assert "first" not in leaf.render_line(1).text  # the editor draws the name
        await pilot.press(*"renamed")
        await wait_until(pilot, lambda: editor.region.width == len("renamed") + 1)
        await pilot.press("enter")
        await wait_until(pilot, lambda: not app.query(NameEditor))
        await wait_until(pilot, lambda: len(client.participants.renames) == 1)
    [rename] = client.participants.renames
    assert rename["participant_id"] == "participant-1"
    assert rename["name"] == "renamed"
    assert isinstance(rename["idempotency_key"], str) and len(rename["idempotency_key"]) == 32


async def test_rename_escape_or_a_click_elsewhere_cancels_without_a_call() -> None:
    app, client, _presentation = build_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await wait_until(pilot, lambda: bool(app.query(NameEditor)))
        await pilot.press("escape")
        await wait_until(pilot, lambda: not app.query(NameEditor))

        await pilot.press("r")
        await wait_until(pilot, lambda: bool(app.query(NameEditor)))
        await pilot.press(*"typed")
        await pilot.click(ParticipantTree, offset=Offset(2, 12))  # empty tree area below the rows
        await wait_until(pilot, lambda: not app.query(NameEditor))
        await pilot.pause()
    assert client.participants.renames == []


async def test_refused_rename_surfaces_the_daemon_message_and_keeps_the_old_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, _presentation = build_app()
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
    app, client, _presentation = build_app()
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
    app, _client, _presentation = build_app()
    state = cast(FakeState, app._state)
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
