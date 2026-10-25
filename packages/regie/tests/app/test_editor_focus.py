"""An open inline editor survives the terminal losing focus, and says why it closed otherwise."""

from __future__ import annotations

import logging

import pytest
from regie.widgets import ParticipantTree
from regie.widgets.name_editor import DirectoryEditor
from textual import events

from packages.regie.tests.support.app_fakes import build_app
from packages.regie.tests.support.waiting import wait_until


async def _open_spawn_prompt(app, pilot) -> DirectoryEditor:
    tree = app.query_one(ParticipantTree)
    await wait_until(pilot, lambda: len(tree.participant_ids) == 2 and bool(app._harnesses))
    app.spawn_harness("codex")
    await wait_until(pilot, lambda: bool(app.query(DirectoryEditor)))
    return app.query_one(DirectoryEditor)


async def test_the_directory_prompt_survives_the_terminal_losing_focus(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="regie.latency")
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        editor = await _open_spawn_prompt(app, pilot)

        app.post_message(events.AppBlur())  # tmux tells Régie its terminal lost focus
        await pilot.pause(0.2)
        assert (
            app._pending_spawn is not None
            and ("n", "new") in app.query_one(ParticipantTree)._key_widgets
        )
        assert editor.is_attached

        app.post_message(events.AppFocus())
        await wait_until(pilot, lambda: app.focused is editor)
        await pilot.press("x")
        assert editor.value.endswith("x")  # it is still the prompt it was
        messages = "\n".join(caplog.messages)  # before teardown blurs everything
    assert "interaction.terminal_lost_focus" in messages
    assert "interaction.terminal_gained_focus" in messages
    assert "interaction.editor.kept the terminal lost focus" in messages
    assert "interaction.editor.cancelled" not in messages


async def test_leaving_the_prompt_inside_regie_still_cancels_it_and_says_how(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="regie.latency")
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        await _open_spawn_prompt(app, pilot)
        app.set_focus(None)  # a click elsewhere: the human's own choice
        await wait_until(pilot, lambda: app._pending_spawn is None)

    assert any(m.startswith("interaction.editor.cancelled by=blur") for m in caplog.messages)


async def test_escape_cancels_the_prompt_and_says_so(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="regie.latency")
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        await _open_spawn_prompt(app, pilot)
        await pilot.press("escape")
        await wait_until(pilot, lambda: app._pending_spawn is None)

    assert any(m.startswith("interaction.editor.cancelled by=escape") for m in caplog.messages)
