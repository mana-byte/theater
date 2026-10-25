"""An open inline editor survives the terminal losing focus."""

from __future__ import annotations

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


async def test_the_directory_prompt_survives_the_terminal_losing_focus() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        editor = await _open_spawn_prompt(app, pilot)

        app.post_message(events.AppBlur())  # tmux tells Régie its terminal lost focus
        await pilot.pause(0.2)
        assert app._pending_spawn is not None
        assert ("n", "new") in app.query_one(ParticipantTree)._key_widgets
        assert editor.is_attached

        app.post_message(events.AppFocus())
        await wait_until(pilot, lambda: app.focused is editor)
        await pilot.press("x")
        assert editor.value.endswith("x")  # it is still the prompt it was


async def test_leaving_the_prompt_inside_regie_still_cancels_it() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        await _open_spawn_prompt(app, pilot)
        app.set_focus(None)  # a click elsewhere: the human's own choice
        await wait_until(pilot, lambda: app._pending_spawn is None)
