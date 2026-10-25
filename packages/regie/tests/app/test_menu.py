"""The options menu's keys and clicks: they act only while the cursor is in it."""

from __future__ import annotations

import pytest
from regie.menu import MenuItem
from regie.widgets import ParticipantTree
from regie.widgets.name_editor import DirectoryEditor, FilterEditor

from packages.regie.tests.support.app_fakes import build_app
from packages.regie.tests.support.waiting import wait_until
from theater.frontend import ResumeCandidate as PublicResumeCandidate
from theater.frontend import TranscriptCandidate

ROOT = ("g", "options")
CODEX = ("m", "spawn/codex")


async def _ready(app, pilot) -> ParticipantTree:
    tree = app.query_one(ParticipantTree)
    await wait_until(pilot, lambda: app._catalog_ready.is_set() and ROOT in tree._key_widgets)
    return tree


@pytest.mark.asyncio
async def test_o_opens_the_menu_on_the_first_harness_and_x_goes_back_where_the_cursor_was() -> None:
    app, client, _presentation = build_app()

    async with app.run_test(size=(60, 40)) as pilot:
        tree = await _ready(app, pilot)
        tree.select("participant-1")

        await pilot.press("o")
        await wait_until(pilot, lambda: tree.selected_key == CODEX)
        assert tree.menu_return == ("p", "participant-1")

        await pilot.press("x")  # inside the menu x closes it; it does not kill anything
        await wait_until(pilot, lambda: tree.selected_key == ("p", "participant-1"))
        assert not tree.in_menu and client.participants.terminated == []


@pytest.mark.asyncio
async def test_outside_the_menu_the_same_keys_still_act_on_the_agent() -> None:
    app, client, _presentation = build_app()

    async with app.run_test(size=(60, 40)) as pilot:
        tree = await _ready(app, pilot)
        tree.select("participant-2")

        await pilot.press("x")
        await wait_until(pilot, lambda: client.participants.terminated == ["participant-2"])


@pytest.mark.asyncio
async def test_enter_on_a_harness_leaves_the_menu_and_asks_for_the_directory() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 40)) as pilot:
        tree = await _ready(app, pilot)
        tree.select("participant-1")
        await pilot.press("o")
        await wait_until(pilot, lambda: tree.selected_key == CODEX)

        await pilot.press("enter")
        await wait_until(pilot, lambda: bool(app.query(DirectoryEditor)))
        assert not tree.in_menu and ROOT not in tree._key_widgets  # the menu steps aside


@pytest.mark.asyncio
async def test_a_sub_menu_unfolds_on_enter_l_or_a_click_and_h_folds_it_back() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 40)) as pilot:
        tree = await _ready(app, pilot)
        tree.open_menu()
        await wait_until(pilot, lambda: ("m", "spawn") in tree._key_widgets)
        await pilot.press("j")
        assert tree.selected_key == ("m", "spawn")

        await pilot.press("l")  # unfolds and walks in
        await wait_until(pilot, lambda: tree.selected_key == CODEX)

        await pilot.press("h")  # a choice folds its option and goes up to it
        await wait_until(pilot, lambda: tree.selected_key == ("m", "spawn"))
        assert CODEX not in tree._key_widgets

        await pilot.press("space")  # toggles without moving
        await wait_until(pilot, lambda: CODEX in tree._key_widgets)
        assert tree.selected_key == ("m", "spawn")

        await pilot.press("h")  # an unfolded option folds
        await wait_until(pilot, lambda: CODEX not in tree._key_widgets)
        await pilot.press("h")  # a folded one goes up to the menu
        await wait_until(pilot, lambda: tree.selected_key == ROOT)

        await pilot.click(tree._key_widgets[("m", "spawn")])  # a click unfolds in place
        await wait_until(pilot, lambda: CODEX in tree._key_widgets)
        assert tree.selected_key == ("m", "spawn")


@pytest.mark.asyncio
async def test_a_toggle_option_does_its_thing_and_closes_the_menu() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 40)) as pilot:
        tree = await _ready(app, pilot)
        tree.select("participant-1")
        tree.open_menu()
        await wait_until(pilot, lambda: ("m", "bus") in tree._key_widgets)
        tree.select_key(("m", "bus"))
        assert not app.bus_visible

        await pilot.press("enter")
        await wait_until(pilot, lambda: app.bus_visible)
        assert tree.selected_key == ("p", "participant-1") and not tree.in_menu


@pytest.mark.asyncio
async def test_add_separator_works_on_the_row_the_cursor_came_from() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 40)) as pilot:
        tree = await _ready(app, pilot)
        tree.select("participant-2")
        tree.open_menu()
        await wait_until(pilot, lambda: ("m", "separator") in tree._key_widgets)
        tree.select_key(("m", "separator"))

        await pilot.press("enter")
        await wait_until(pilot, lambda: any(key[0] == "s" for key in tree.selectable_keys))
        assert not tree.in_menu


@pytest.mark.asyncio
async def test_add_separator_is_disabled_when_the_menu_was_entered_from_nowhere() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 40)) as pilot:
        await _ready(app, pilot)
        item = next(item for item in app._menu_items() if item.id == "separator")
        assert not item.enabled  # no origin row yet: the cursor has not been to the menu


def _dead(n: int, *, state: str = "resumable") -> PublicResumeCandidate:
    session = f"session-{n}"
    return PublicResumeCandidate.from_wire(
        {
            "participant_id": f"dead-{n}",
            "harness": "codex",
            "name": f"old {n}",
            "cwd": f"/workspace/{n}",
            "resume_state": state,
            "transcript_identity": {
                "state": "trusted" if state == "resumable" else "untrusted",
                "session_id": session,
                "provenance": "exact",
                "location": f"/tmp/{n}",
                "domain": None,
                "detail": None,
            },
        }
    )


def _row(tree: ParticipantTree, key) -> list[str]:
    widget = tree._key_widgets[key]
    return [widget.render_line(y).text.strip() for y in range(widget.size.height)]


@pytest.mark.asyncio
async def test_resume_lists_the_dead_sessions_as_dead_agents_and_a_pick_brings_one_back() -> None:
    app, client, _presentation = build_app()
    client.participants.dead_rows = (_dead(1), _dead(2, state="untrusted"))

    async with app.run_test(size=(70, 50)) as pilot:
        tree = await _ready(app, pilot)
        tree.select("participant-1")
        tree.open_menu()
        await wait_until(pilot, lambda: ("m", "resume") in tree._key_widgets)
        tree.select_key(("m", "resume"))

        await pilot.press("enter")
        await wait_until(pilot, lambda: ("m", "resume/dead-1") in tree._key_widgets)
        good, bad = _row(tree, ("m", "resume/dead-1")), _row(tree, ("m", "resume/dead-2"))
        assert "✗ codex  old 1" in good[1] and "/workspace/1" in good[2]
        assert "transcript identity" in bad[2]  # why it cannot come back

        tree.select_key(("m", "resume/dead-1"))
        await pilot.press("enter")
        await wait_until(pilot, lambda: bool(client.participants.spawned))
        assert client.participants.spawn_options[0]["resume"] == "session-1"
        assert not tree.in_menu and tree.selected_key == ("p", "participant-1")


@pytest.mark.asyncio
async def test_a_long_list_shows_five_and_enter_on_the_rest_shows_them_all() -> None:
    app, client, _presentation = build_app()
    client.participants.dead_rows = tuple(_dead(n) for n in range(12))

    async with app.run_test(size=(70, 50)) as pilot:
        tree = await _ready(app, pilot)
        tree.open_menu()
        await wait_until(pilot, lambda: ("m", "resume") in tree._key_widgets)
        tree.select_key(("m", "resume"))
        await pilot.press("l")
        await wait_until(pilot, lambda: ("m", "resume/…") in tree._key_widgets)
        shown = [key for key in tree._key_widgets if key[1].startswith("resume/dead-")]
        assert len(shown) == 5 and "7 more" in _row(tree, ("m", "resume/…"))[1]

        tree.select_key(("m", "resume/…"))
        await pilot.press("enter")
        await wait_until(pilot, lambda: ("m", "resume/dead-11") in tree._key_widgets)
        assert ("m", "resume/…") not in tree._key_widgets
        assert tree.selected_key == ("m", "resume/dead-5")  # the first row that was hidden


@pytest.mark.asyncio
async def test_a_theme_pick_applies_at_once_and_leaves_the_menu_open() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(70, 50)) as pilot:
        tree = await _ready(app, pilot)
        tree.open_menu()
        await wait_until(pilot, lambda: ("m", "theme") in tree._key_widgets)
        tree.select_key(("m", "theme"))
        await pilot.press("l")
        name = next(n for n in sorted(app.available_themes) if n != app.theme)
        await wait_until(pilot, lambda: ("m", f"theme/{name}") in tree._key_widgets)

        tree.select_key(("m", f"theme/{name}"))
        await pilot.press("enter")
        await wait_until(pilot, lambda: app.theme == name)
        assert tree.in_menu and "●" in _row(tree, ("m", f"theme/{name}"))[1]


@pytest.mark.asyncio
async def test_recover_transcript_binds_a_candidate_to_the_row_the_cursor_came_from() -> None:
    app, client, _presentation = build_app()
    client.transcripts.candidate_rows = (
        TranscriptCandidate("/tmp/accepted.jsonl", session_id="accepted", provenance="exact"),
    )

    async with app.run_test(size=(70, 50)) as pilot:
        tree = await _ready(app, pilot)
        tree.select("participant-1")
        tree.open_menu()
        await wait_until(pilot, lambda: ("m", "transcript") in tree._key_widgets)
        tree.select_key(("m", "transcript"))

        await pilot.press("enter")
        await wait_until(pilot, lambda: ("m", "transcript/0") in tree._key_widgets)
        assert "/tmp/accepted.jsonl" in _row(tree, ("m", "transcript/0"))[1]

        await pilot.press("enter")
        await wait_until(pilot, lambda: bool(client.transcripts.bind_calls))
        assert client.transcripts.bind_calls[0]["participant_id"] == "participant-1"


@pytest.mark.asyncio
async def test_slash_filters_the_menu_enter_keeps_it_and_x_clears_it_with_the_menu() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(70, 50)) as pilot:
        tree = await _ready(app, pilot)
        tree.select("participant-1")
        tree.open_menu()
        await wait_until(pilot, lambda: ("m", "bus") in tree._key_widgets)

        await pilot.press("slash")
        await wait_until(pilot, lambda: bool(app.query(FilterEditor)))
        await pilot.press("u", "s", "a", "g", "e")
        await wait_until(pilot, lambda: ("m", "bus") not in tree._key_widgets)
        assert ("m", "usage") in tree._key_widgets and tree.selected_key == ROOT

        await pilot.press("enter")  # keep the filter, walk to the first match
        await wait_until(pilot, lambda: tree.selected_key == ("m", "usage"))
        assert not app.query(FilterEditor) and app.focused is None
        assert "/ usage" in "".join(_row(tree, ROOT))

        await pilot.press("x")  # closes the menu, and the filter goes with it
        await wait_until(pilot, lambda: not tree.in_menu)
        assert tree.menu_view.query == ""


@pytest.mark.asyncio
async def test_escape_drops_the_filter_and_a_match_inside_a_sub_menu_shows_unfolded() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(70, 50)) as pilot:
        tree = await _ready(app, pilot)
        tree.open_menu()
        await wait_until(pilot, lambda: ("m", "spawn") in tree._key_widgets)

        await pilot.press("slash")
        await wait_until(pilot, lambda: bool(app.query(FilterEditor)))
        await pilot.press("c", "o", "d", "e", "x")  # o and x are letters while the editor has focus
        await wait_until(pilot, lambda: CODEX in tree._key_widgets)  # codex, under a folded Spawn
        assert ("m", "bus") not in tree._key_widgets

        await pilot.press("escape")
        await wait_until(pilot, lambda: ("m", "bus") in tree._key_widgets)
        assert CODEX not in tree._key_widgets and tree.selected_key == ROOT
        assert tree.menu_view.query == "" and not app.query(FilterEditor)


@pytest.mark.asyncio
async def test_ctrl_p_opens_the_menu_at_its_root_and_no_palette_exists() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(60, 40)) as pilot:
        tree = await _ready(app, pilot)
        tree.select("participant-1")

        await pilot.press("ctrl+p")
        await wait_until(pilot, lambda: tree.selected_key == ROOT and tree.in_menu)
        assert app.screen is app.screen_stack[0]  # nothing was pushed over the tree
        assert not app.use_command_palette


@pytest.mark.asyncio
async def test_a_harness_has_no_description_unless_it_cannot_launch() -> None:
    app, _client, _presentation = build_app()

    async with app.run_test(size=(70, 50)) as pilot:
        tree = await _ready(app, pilot)
        await pilot.press("o")
        await wait_until(pilot, lambda: tree.selected_key == CODEX)

        assert _row(tree, CODEX)[2].strip("│ ") == ""  # nothing under the harness's name


@pytest.mark.asyncio
async def test_a_long_help_scrolls_on_the_row_in_focus_and_settles_when_it_leaves() -> None:
    app, _client, _presentation = build_app()
    long_help = "a description far too long to fit on one line of the narrow tree " * 2
    items = (MenuItem("bus", "Show bus panel", long_help), MenuItem("quit", "Quit", "short"))

    async with app.run_test(size=(40, 50)) as pilot:
        tree = await _ready(app, pilot)
        tree.menu_source = lambda: items
        tree.open_menu()
        await wait_until(pilot, lambda: ("m", "bus") in tree._key_widgets)
        row = tree._key_widgets[("m", "bus")]
        await wait_until(pilot, lambda: row.size.height == 3)
        tree.select_key(("m", "quit"))
        await wait_until(pilot, lambda: row._marquee_sub is None)
        resting = _row(tree, ("m", "bus"))[2]
        assert resting.endswith("…")  # cut, not moving

        tree.select_key(("m", "bus"))
        await wait_until(pilot, lambda: row._marquee_sub is not None)
        seen = {_row(tree, ("m", "bus"))[2]}
        await wait_until(pilot, lambda: seen.add(_row(tree, ("m", "bus"))[2]) or len(seen) >= 3, 10)

        tree.select_key(("m", "quit"))
        await wait_until(pilot, lambda: row._marquee_sub is None)
        assert _row(tree, ("m", "bus"))[2] == resting and row._marquee_offset == 0
