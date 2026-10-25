"""The options menu model: which rows exist for the cursor, the open options and the filter."""

from __future__ import annotations

from regie.menu import Loaded, MenuItem, MenuView, find_item, menu_nodes


def _choices(prefix: str, count: int) -> Loaded:
    return Loaded(tuple(MenuItem(f"{prefix}{n}", f"{prefix} {n}") for n in range(count)))


def _items() -> tuple[MenuItem, ...]:
    return (
        MenuItem("spawn", "Spawn", "start a session", choices=_choices("harness", 3)),
        MenuItem("resume", "Resume", "a dead session", choices=_choices("session", 20)),
        MenuItem("bus", "Show bus panel", run=lambda: None),
    )


def _ids(node) -> list[str]:
    return [child["id"] for child in node["children"]]


def test_folded_the_menu_is_its_root_alone():
    root = menu_nodes(_items(), MenuView(), open=False)

    assert root["kind"] == "menu_root" and root["children"] == [] and not root["open"]


def test_open_it_lists_its_options_and_marks_those_that_unfold():
    root = menu_nodes(_items(), MenuView(), open=True)

    assert root["open"] and _ids(root) == ["spawn", "resume", "bus"]
    assert [child.get("chevron") for child in root["children"]] == ["▸", "▸", None]
    assert all(child["children"] == [] for child in root["children"])  # options never auto-unfold


def test_an_unfolded_option_shows_its_choices_as_grandchildren():
    view = MenuView(expanded=frozenset({"spawn"}))
    spawn = menu_nodes(_items(), view, open=True)["children"][0]

    assert spawn["chevron"] == "▾" and _ids(spawn) == [
        "spawn/harness0",
        "spawn/harness1",
        "spawn/harness2",
    ]


def test_a_long_list_shows_five_then_a_leaf_for_the_rest_until_asked():
    resume = lambda view: menu_nodes(_items(), view, open=True)["children"][1]  # noqa: E731

    cut = resume(MenuView(expanded=frozenset({"resume"})))
    assert _ids(cut)[-1] == "resume/…" and len(cut["children"]) == 6
    assert cut["children"][-1]["title"] == "… 15 more"

    whole = resume(MenuView(expanded=frozenset({"resume"}), more=frozenset({"resume"})))
    assert len(whole["children"]) == 20


def test_a_list_one_over_the_preview_is_not_cut_for_a_single_row():
    items = (MenuItem("themes", "Theme", choices=_choices("theme", 6)),)
    view = MenuView(expanded=frozenset({"themes"}))

    assert len(menu_nodes(items, view, open=True)["children"][0]["children"]) == 6


def test_the_filter_keeps_matches_unfolded_and_ignores_the_preview_cut():
    only_bus = menu_nodes(_items(), MenuView(query="bus"), open=True)
    assert _ids(only_bus) == ["bus"]

    hit = menu_nodes(_items(), MenuView(query="harness 2"), open=True)["children"]
    assert [child["id"] for child in hit] == ["spawn"]
    assert _ids(hit[0]) == ["spawn/harness2"]  # unfolded without being asked

    options = menu_nodes(_items(), MenuView(query="session"), open=True)["children"]
    sessions = next(child for child in options if child["id"] == "resume")
    assert len(sessions["children"]) == 20


def test_a_list_that_is_loading_failed_or_empty_says_so_on_a_row_that_is_not_a_choice():
    def only(loaded):
        item = MenuItem("resume", "Resume", choices=loaded)
        view = MenuView(expanded=frozenset({"resume"}))
        return menu_nodes((item,), view, open=True)["children"][0]["children"]

    assert only(Loaded(state="loading"))[0]["kind"] == "info"
    assert only(Loaded(state="failed", note="daemon refused"))[0]["title"] == "daemon refused"
    assert only(Loaded(note="no dead sessions"))[0]["title"] == "no dead sessions"


def test_a_disabled_choice_stays_listed_with_its_reason():
    item = MenuItem(
        "spawn",
        "Spawn",
        choices=Loaded((MenuItem("vibe", "vibe", "vibe is unavailable", enabled=False),)),
    )
    spawn = menu_nodes((item,), MenuView(expanded=frozenset({"spawn"})), open=True)["children"][0]

    assert spawn["children"][0]["enabled"] is False
    assert spawn["children"][0]["help"] == "vibe is unavailable"


def test_a_row_is_found_by_the_path_in_its_id():
    items = _items()

    assert find_item(items, "bus") is items[2]
    assert find_item(items, "spawn/harness1") is items[0].choices.items[1]  # type: ignore[union-attr]
    assert find_item(items, "spawn/nope") is None and find_item(items, "nope") is None


def test_the_filter_ignores_case_and_surrounding_blanks_and_the_root_shows_it():
    root = menu_nodes(_items(), MenuView(query="  BUS "), open=True)

    assert _ids(root) == ["bus"] and root["filter"] == "  BUS "
