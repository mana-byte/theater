"""The régie, driven for real through Textual's test pilot.

Everything else about the régie is tested against pure functions — rendering in
test_regie_tree, bus lines in test_regie_bus, teardown in test_regie_teardown.
What those cannot see is the app itself: whether mount wires the panels up,
whether a keypress reaches the action it is bound to, and whether the tmux
calls that stage a pane happen in the right order with the right arguments.

The two boundaries are faked and nothing else is: `DaemonClient` becomes a
recorder with canned answers, and the tmux module functions become recorders.
The widgets, the reactives, the bindings and the render path are the real ones.
"""

from __future__ import annotations

import pytest

from theater.config import Config, RegieSection
from theater.regie import app as app_mod
from theater.regie.app import RegieApp

PARENT = {
    "id": "aaaaaaaaaaaa",
    "tier": "spawned",
    "harness": "vibe",
    "status": "idle",
    "cwd": "/tmp/proj",
    "tmux_pane": "%10",
    "addressable": True,
    "children": [],
}

CHILD = {
    "id": "bbbbbbbbbbbb",
    "tier": "spawned",
    "harness": "claude",
    "status": "working",
    "cwd": "/tmp/proj/child",
    "tmux_pane": "%11",
    "addressable": True,
    "children": [],
}

BUS_ROW = {
    "id": 1,
    "ts": 1723000000,
    "kind": "agent.assistant",
    "from_id": "aaaaaaaaaaaa",
    "to_id": None,
    "payload": {"text": "hello", "tool": None, "ts": None, "turn_end": True, "index": 0},
}


class FakeClient:
    """A DaemonClient that answers from a dict and remembers what was asked."""

    def __init__(self, answers: dict, broken: set[str]):
        self.answers = answers
        self.broken = broken
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    async def connect(self) -> None:
        pass

    async def call(self, method: str, **params):
        self.calls.append((method, params))
        if method in self.broken:
            raise RuntimeError(f"{method} is unavailable")
        return self.answers.get(method, [])

    async def aclose(self) -> None:
        self.closed = True

    def asked(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]


@pytest.fixture
def daemon(monkeypatch):
    """Install a fake DaemonClient and hand the test its recorder."""
    state: dict = {
        "answers": {
            "participants.tree": [dict(PARENT, children=[dict(CHILD)])],
            "participants.unmanaged": [],
            "bus.tail": [],
            "harnesses": [{"name": "vibe"}, {"name": "claude"}],
        },
        "broken": set(),
        "client": None,
    }

    def factory(*_args, **_kwargs):
        client = FakeClient(state["answers"], state["broken"])
        state["client"] = client
        return client

    monkeypatch.setattr(app_mod, "DaemonClient", factory)
    return state


@pytest.fixture
def tmux(monkeypatch):
    """Fake the tmux surface the app touches; record the calls in order."""
    calls: list[tuple] = []

    async def display_message(fmt, *, target=None):
        return {
            "#{window_id}": "@7",
            "#{session_id}": "$2",
            "#{session_name}": "work",
        }[fmt]

    async def show_option(name, *, target):
        return None

    async def set_option(name, value, *, target):
        calls.append(("set", name, value))

    async def unset_option(name, *, target):
        calls.append(("unset", name))

    async def join_pane(pane, *, target_window=None):
        calls.append(("join", pane, target_window))

    async def break_pane(pane, *, target_window=None):
        calls.append(("break", pane))

    async def resize_pane(pane, *, width=None):
        calls.append(("resize", pane, width))

    async def select_pane(pane):
        calls.append(("select", pane))

    monkeypatch.setattr(app_mod.tmux, "current_pane", lambda: "%1")
    monkeypatch.setattr(app_mod.tmux, "display_message", display_message)
    monkeypatch.setattr(app_mod.tmux, "show_option", show_option)
    monkeypatch.setattr(app_mod.tmux, "set_option", set_option)
    monkeypatch.setattr(app_mod.tmux, "unset_option", unset_option)
    monkeypatch.setattr(app_mod.panes, "join_pane", join_pane)
    monkeypatch.setattr(app_mod.panes, "break_pane", break_pane)
    monkeypatch.setattr(app_mod.panes, "resize_pane", resize_pane)
    monkeypatch.setattr(app_mod.panes, "select_pane", select_pane)
    return calls


def make_app(**regie) -> tuple[RegieApp, list[tuple[str, str]]]:
    """An app with slow timers, and the list its notifications land in.

    The intervals are pushed out of the way so the only refreshes in a test are
    the ones at mount and the ones an action asks for. A one-second tree poll
    would otherwise race every assertion about what was called.
    """
    settings = Config(regie=RegieSection(tree_interval=60, bus_interval=60, **regie))
    app = RegieApp(settings)
    notes: list[tuple[str, str]] = []
    app.notify = lambda msg, **kw: notes.append(  # type: ignore[method-assign]
        (str(msg), kw.get("severity", "information"))
    )
    return app, notes


# ---- mount ---------------------------------------------------------------


async def test_mount_fills_the_tree_and_the_bus(daemon, tmux):
    daemon["answers"]["bus.tail"] = [BUS_ROW]
    app, _ = make_app(bus_visible=True)
    async with app.run_test():
        assert len(app.tree_lines) == 2  # parent and child
        assert app.bus_cursor == 1
        assert app.harnesses == [{"name": "vibe"}, {"name": "claude"}]
        # The panel holds one Label per line, and knows which node each is.
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        assert len(panel.children) == 2
        assert panel._lines_data[1][1]["id"] == CHILD["id"]


async def test_mount_learns_its_own_window_and_session(daemon, tmux):
    app, _ = make_app()
    async with app.run_test():
        assert app.my_pane == "%1"
        assert app.my_window == "@7"
        assert app.my_session_name == "work"
        # Mouse reporting is turned on for that session, not globally.
        assert ("set", "mouse", "on") in tmux
        # tmux's own status line is hidden for the duration, same scope.
        assert ("set", "status", "off") in tmux


async def test_an_empty_tree_says_so_instead_of_rendering_nothing(daemon, tmux):
    daemon["answers"]["participants.tree"] = []
    app, _ = make_app()
    async with app.run_test():
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        assert len(panel.children) == 1
        assert "no participants" in str(panel.children[0].render())


async def test_a_daemon_that_will_not_answer_does_not_stop_the_regie(daemon, tmux):
    """A refresh failure is a missing frame, not a crash: agents outlive it."""
    daemon["broken"] = {"participants.tree", "bus.tail", "harnesses"}
    app, _ = make_app()
    async with app.run_test():
        assert app.tree_lines == []
        assert app.harnesses is None  # the palette reads this as "ask locally"


async def test_an_unknown_theme_warns_and_keeps_the_default(daemon, tmux):
    app, notes = make_app(theme="not-a-theme")
    async with app.run_test():
        assert app.theme != "not-a-theme"
    assert any("unknown theme" in msg for msg, _ in notes)


async def test_a_known_theme_is_applied(daemon, tmux):
    app, notes = make_app(theme="nord")
    async with app.run_test():
        assert app.theme == "nord"
    assert not notes


# ---- cursor --------------------------------------------------------------


async def test_j_and_k_move_the_cursor_and_stop_at_the_ends(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        assert app.cursor == 0
        await pilot.press("k")  # already at the top
        assert app.cursor == 0
        await pilot.press("j")
        assert app.cursor == 1
        await pilot.press("j")  # already at the bottom
        assert app.cursor == 1
        await pilot.press("k")
        assert app.cursor == 0


async def test_the_cursor_line_carries_the_cursor_class(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        await pilot.press("j")
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        assert not panel.children[0].has_class("tree-cursor")
        assert panel.children[1].has_class("tree-cursor")


async def test_a_shorter_tree_pulls_the_cursor_back_in(daemon, tmux):
    """The cursor cannot be left pointing past the end when an agent dies."""
    app, _ = make_app()
    async with app.run_test():
        app.cursor = 1
        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[])]
        await app._refresh_tree()
        assert app.cursor == 0


# ---- staging -------------------------------------------------------------


async def test_enter_joins_the_selected_pane_and_narrows_the_regie(daemon, tmux):
    app, notes = make_app()
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert app.staged_pane == "%10"
    assert ("join", "%10", "@7") in tmux
    assert ("resize", "%1", 52) in tmux
    # Silence is the contract: the pane arriving on the stage is the feedback,
    # and a toast on every Enter was noise on top of a visible result.
    assert notes == []


async def test_enter_on_the_staged_agent_unstages_it(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert app.staged_pane == "%10"
        await pilot.press("enter")
        assert app.staged_pane is None
    assert ("break", "%10") in tmux


async def test_staging_a_second_agent_breaks_the_first_one_out(daemon, tmux):
    """Two panes must never share the stage; the old one goes back first."""
    app, _ = make_app()
    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.press("j")
        await pilot.press("enter")
        assert app.staged_pane == "%11"
    assert tmux.index(("break", "%10")) < tmux.index(("join", "%11", "@7"))


async def test_staging_without_a_known_window_refuses(daemon, tmux):
    app, notes = make_app()
    async with app.run_test() as pilot:
        app.my_window = None
        await pilot.press("enter")
        assert app.staged_pane is None
    assert any("cannot stage" in msg for msg, _ in notes)


async def test_a_pane_that_will_not_join_is_reported_not_recorded(daemon, tmux, monkeypatch):
    async def refuse(pane, *, target_window=None):
        raise RuntimeError("no such window")

    monkeypatch.setattr(app_mod.panes, "join_pane", refuse)
    app, notes = make_app()
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert app.staged_pane is None
    assert any("stage failed" in msg and sev == "error" for msg, sev in notes)


async def test_the_staged_line_is_marked_in_the_tree(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        await pilot.press("enter")
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        assert panel.children[0].has_class("tree-staged")
        assert not panel.children[1].has_class("tree-staged")


# ---- focus ---------------------------------------------------------------


async def test_focus_selects_the_staged_pane(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.press("z")
    assert ("select", "%10") in tmux


async def test_focus_with_nothing_staged_says_what_to_press(daemon, tmux):
    app, notes = make_app()
    async with app.run_test() as pilot:
        await pilot.press("z")
    assert any("press Enter to stage" in msg for msg, _ in notes)


# ---- kill ----------------------------------------------------------------


async def test_kill_asks_the_daemon_and_refreshes(daemon, tmux):
    app, notes = make_app()
    async with app.run_test() as pilot:
        await pilot.press("x")
        client = daemon["client"]
        assert client.asked("participant.kill") == [{"id": PARENT["id"]}]
        # A kill changes the tree, so it is re-read rather than waited for.
        assert len(client.asked("participants.tree")) == 2
    # The row leaving the tree is the feedback; a successful kill says nothing.
    assert notes == []


async def test_a_refused_kill_is_reported(daemon, tmux):
    daemon["broken"] = {"participant.kill"}
    app, notes = make_app()
    async with app.run_test() as pilot:
        await pilot.press("x")
    assert any("kill failed" in msg and sev == "error" for msg, sev in notes)


async def test_an_unmanaged_pane_cannot_be_killed(daemon, tmux):
    """Unmanaged rows have no id, so there is nothing to address."""
    daemon["answers"]["participants.tree"] = []
    daemon["answers"]["participants.unmanaged"] = [
        {"pane": "%20", "command": "vibe", "harness": "vibe", "cwd": "/tmp/x"},
    ]
    app, notes = make_app()
    async with app.run_test() as pilot:
        await pilot.press("x")
        assert daemon["client"].asked("participant.kill") == []
    assert any(sev == "warning" for _, sev in notes)


# ---- palette spawn -------------------------------------------------------


async def test_the_palette_spawns_into_the_regie_session(daemon, tmux):
    daemon["answers"]["spawn"] = {"id": "cccccccccccc", "tmux_pane": "%30"}
    app, notes = make_app()
    async with app.run_test():
        app.spawn_harness("claude")
        await app.workers.wait_for_complete()
        [params] = daemon["client"].asked("spawn")
    # A bare CLI: no prompt, no parent, and in the window the user is looking at.
    assert params["harness"] == "claude"
    assert params["prompt"] == ""
    assert params["tmux_session"] == "work"
    # The new agent appearing in the tree is the feedback, so nothing is said.
    assert notes == []


async def test_a_failed_spawn_is_reported(daemon, tmux):
    daemon["broken"] = {"spawn"}
    app, notes = make_app()
    async with app.run_test():
        app.spawn_harness("claude")
        await app.workers.wait_for_complete()
    assert any("spawn failed" in msg and sev == "error" for msg, sev in notes)


# ---- bus -----------------------------------------------------------------


async def test_the_bus_advances_its_cursor_and_asks_only_for_new_rows(daemon, tmux):
    app, _ = make_app(bus_visible=True)
    async with app.run_test():
        daemon["answers"]["bus.tail"] = [BUS_ROW, dict(BUS_ROW, id=2)]
        await app._refresh_bus()
        assert app.bus_cursor == 2
        await app._refresh_bus()
        assert daemon["client"].asked("bus.tail")[-1]["after_id"] == 2


async def test_a_gap_in_the_feed_is_admitted(daemon, tmux):
    """Dropping events silently would make the panel lie about being complete."""
    app, _ = make_app(bus_visible=True)
    async with app.run_test():
        daemon["answers"]["bus.tail"] = [BUS_ROW]
        await app._refresh_bus()
        daemon["answers"]["bus.tail"] = [dict(BUS_ROW, id=9)]
        await app._refresh_bus()
        assert app.bus_cursor == 9
        log = app.query_one("#bus-panel", app_mod.RichLog)
        assert any("7 events dropped" in str(line) for line in log.lines)


async def test_the_bus_panel_is_hidden_until_it_is_asked_for(daemon, tmux):
    """Off unless the config says otherwise, and the panel obeys at compose.

    A reactive assigned its own default fires no watcher, so a settings-driven
    initial state has to be applied while the widget is built rather than left
    to `watch_bus_visible`. Both directions are pinned here because getting
    that wrong is invisible in one of them.
    """
    app, _ = make_app()
    async with app.run_test():
        assert app.query_one("#bus-panel", app_mod.RichLog).has_class("-hidden")

    app, _ = make_app(bus_visible=True)
    async with app.run_test():
        assert not app.query_one("#bus-panel", app_mod.RichLog).has_class("-hidden")


async def test_toggling_the_bus_panel_shows_and_hides_it(daemon, tmux):
    app, _ = make_app()
    async with app.run_test():
        log = app.query_one("#bus-panel", app_mod.RichLog)
        assert log.has_class("-hidden")
        app.action_toggle_bus()
        assert not log.has_class("-hidden")
        app.action_toggle_bus()
        assert log.has_class("-hidden")


async def test_a_hidden_bus_does_not_consume_the_events_it_cannot_show(daemon, tmux):
    """A display:none RichLog keeps no writes, so the cursor must not move.

    Otherwise the panel would silently eat every event that arrived while it
    was away, and showing it would resume from a line the user never saw.
    """
    app, _ = make_app()
    async with app.run_test() as pilot:
        daemon["answers"]["bus.tail"] = [BUS_ROW, dict(BUS_ROW, id=2)]
        await app._refresh_bus()
        assert app.bus_cursor == 0
        # Showing it picks the same rows up and draws them. The pause is not
        # decoration: a RichLog that has never been displayed has no width
        # yet, and wraps every write to nothing until a layout pass gives it
        # one. In the app that frame happens long before the next poll.
        app.action_toggle_bus()
        await pilot.pause()
        await app._refresh_bus()
        assert app.bus_cursor == 2
        log = app.query_one("#bus-panel", app_mod.RichLog)
        assert log.lines


# ---- exit ----------------------------------------------------------------


async def test_quitting_unstages_and_gives_the_mouse_back(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.press("q")
    assert ("break", "%10") in tmux
    # No prior session-local value, so ours is removed rather than pinned.
    assert ("unset", "mouse") in tmux
    assert ("unset", "status") in tmux
    assert daemon["client"].closed


# ---- sidebar width --------------------------------------------------------


async def test_configured_sidebar_width_reaches_both_style_and_resize(daemon, tmux):
    """The width is read once and used twice: the #sidebar style and resize_pane."""
    app, _ = make_app(sidebar_width=44)
    async with app.run_test() as pilot:
        sidebar = app.query_one("#sidebar")
        assert sidebar.styles.width.value == 44
        await pilot.press("enter")
    assert ("resize", "%1", 44) in tmux


# ---- mouse --------------------------------------------------------------


async def test_single_click_moves_the_cursor(daemon, tmux):
    """A single click on a leaf moves the cursor to that participant."""
    app, _ = make_app()
    async with app.run_test(size=(80, 40)) as pilot:
        assert app.cursor == 0
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        child_widget = panel._key_widgets[("p", CHILD["id"])]
        await pilot.click(widget=child_widget)
        assert app.cursor == 1


async def test_double_click_stages_the_agent(daemon, tmux):
    """A double click on a leaf stages it, the same as pressing enter."""
    app, _ = make_app()
    async with app.run_test() as pilot:
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        parent_widget = panel._key_widgets[("p", PARENT["id"])]
        await pilot.click(widget=parent_widget, times=2)
        assert app.staged_pane == "%10"
    assert ("join", "%10", "@7") in tmux


async def test_click_on_any_row_of_a_leaf_moves_the_cursor(daemon, tmux):
    """All three rows of a leaf are one click target."""
    app, _ = make_app()
    async with app.run_test(size=(80, 40)) as pilot:
        assert app.cursor == 0
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        child_widget = panel._key_widgets[("p", CHILD["id"])]
        # Click at offset (0, 2) — the third row (cwd), still inside the leaf.
        await pilot.click(widget=child_widget, offset=(0, 2))
        assert app.cursor == 1
