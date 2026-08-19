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
from theater.regie.tree import SEND_STYLE, send_path

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

SEND_ROW = {
    "id": 1,
    "ts": 1723000000,
    "kind": "agent.send",
    "from_id": PARENT["id"],
    "to_id": CHILD["id"],
    "payload": {"handle": "h1", "prompt": "do the thing"},
}

SPAWN_ROW = {
    "id": 1,
    "ts": 1723000000,
    "kind": "participant.created",
    "from_id": PARENT["id"],
    "to_id": CHILD["id"],
    "payload": {
        "tier": "spawned",
        "harness": CHILD["harness"],
        "cwd": CHILD["cwd"],
        "has_prompt": True,
    },
}

AWAIT_START_ROW = {
    "id": 1,
    "ts": 1723000000,
    "kind": "job.await.start",
    "from_id": PARENT["id"],
    "to_id": CHILD["id"],
    "payload": {"handle": CHILD["id"], "token": "await-token"},
}

AWAIT_END_ROW = {
    **AWAIT_START_ROW,
    "id": 2,
    "kind": "job.await.end",
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

    async def bind_key_if_free(table, key, command, *, note):
        calls.append(("bind", table, key, tuple(command)))
        return True

    async def unbind_key_if_owned(table, key, *, note):
        calls.append(("unbind", table, key))

    monkeypatch.setattr(app_mod.tmux, "current_pane", lambda: "%1")
    monkeypatch.setattr(app_mod.tmux, "display_message", display_message)
    monkeypatch.setattr(app_mod.tmux, "show_option", show_option)
    monkeypatch.setattr(app_mod.tmux, "set_option", set_option)
    monkeypatch.setattr(app_mod.tmux, "unset_option", unset_option)
    monkeypatch.setattr(app_mod.tmux, "bind_key_if_free", bind_key_if_free)
    monkeypatch.setattr(app_mod.tmux, "unbind_key_if_owned", unbind_key_if_owned)
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


def _styles(widget) -> list[str]:
    return [span.style for span in widget.render().spans]


def _overlay_styles(widget) -> list[str]:
    styles = []
    for glyph in (widget._overlay or {}).values():
        styles.append(glyph[1] if isinstance(glyph, tuple) else glyph)
    return styles


def _overlay_glyphs(widget) -> list[str]:
    glyphs = []
    for glyph in (widget._overlay or {}).values():
        glyphs.append(glyph[0] if isinstance(glyph, tuple) else glyph)
    return glyphs


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


async def test_every_tree_row_shares_one_left_inset(daemon, tmux):
    """Leaves and separators are different widgets on one visual column.

    A participant row is an AgentLeaf and the "unmanaged" divider is a plain
    Label, styled by two different rules. Nothing but this test makes them
    agree, and a one-cell disagreement is the kind of thing that reads as a
    rendering bug rather than a stylesheet one.

    The height assertion guards the other half: the leaf draws exactly three
    rows in exactly three cells, so vertical padding does not space the rows
    out, it truncates the cwd line off the bottom.
    """
    daemon["answers"]["participants.unmanaged"] = [
        {"pane": "%20", "command": "vibe", "harness": "vibe", "cwd": "/tmp/x"},
    ]
    app, _ = make_app()
    async with app.run_test():
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        leaves = [w for w in panel.children if isinstance(w, app_mod.AgentLeaf)]
        labels = [w for w in panel.children if not isinstance(w, app_mod.AgentLeaf)]
        assert leaves and labels, "need both row kinds on screen to compare them"

        insets = {w.styles.padding.left for w in panel.children} | {
            w.styles.padding.right for w in panel.children
        }
        assert insets == {2}

        for leaf in leaves:
            assert leaf.styles.padding.top == 0
            assert leaf.styles.padding.bottom == 0
            assert leaf.styles.margin.top == 0
            assert leaf.styles.margin.bottom == 0
            assert leaf.styles.height.value == 3


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
        await pilot.press("l")
    assert ("select", "%10") in tmux


async def test_focus_stages_first_when_nothing_is_staged(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        await pilot.press("l")
        assert app.staged_pane == "%10"
    assert ("join", "%10", "@7") in tmux
    assert ("select", "%10") in tmux


async def test_focus_does_not_refocus_a_stale_pane_after_a_failed_switch(daemon, tmux, monkeypatch):
    async def refuse(pane, *, target_window=None):
        raise RuntimeError("no such window")

    app, notes = make_app()
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert app.staged_pane == "%10"
        monkeypatch.setattr(app_mod.panes, "join_pane", refuse)
        await pilot.press("j")
        await pilot.press("l")
    assert not any(call[0] == "select" for call in tmux)
    assert any("stage failed" in msg and sev == "error" for msg, sev in notes)


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


# ---- tree-route animation ------------------------------------------------


async def test_a_send_animates_while_the_bus_panel_is_hidden(daemon, tmux):
    """The animation reads the bus on its own cursor, so hiding costs nothing.

    The panel's cursor must stay where it was — it has drawn nothing — while
    the animation's own cursor moves past the row it consumed. Two readers of
    one log; this is the test that keeps them apart.
    """
    app, _ = make_app()  # bus hidden
    async with app.run_test():
        daemon["answers"]["bus.tail"] = [SEND_ROW]
        await app._refresh_anim()
        assert len(app._route_anims) == 1
        assert app.anim_cursor == 1
        assert app.bus_cursor == 0
        assert not app.query_one("#bus-panel", app_mod.RichLog).lines


async def test_the_first_poll_only_takes_the_cursor(daemon, tmux):
    """Sends already in the log happened before the régie was looking.

    Without priming, starting the régie would replay the daemon's whole
    buffer as a burst of traces for deliveries that are long finished.
    """
    daemon["answers"]["bus.tail"] = [SEND_ROW]
    app, _ = make_app()
    async with app.run_test():
        # The mount poll primed the cursor and animated nothing.
        assert app._route_anims == []
        assert app.anim_cursor == 1
        # A row arriving after that does animate.
        daemon["answers"]["bus.tail"] = [dict(SEND_ROW, id=2)]
        await app._refresh_anim()
        assert len(app._route_anims) == 1


async def test_only_sends_animate(daemon, tmux):
    """Other bus traffic moves the cursor and nothing else."""
    app, _ = make_app()
    async with app.run_test():
        daemon["answers"]["bus.tail"] = [BUS_ROW]
        await app._refresh_anim()
        assert app._route_anims == []
        assert app.anim_cursor == 1


async def test_a_send_with_no_visible_sender_or_target_is_dropped(daemon, tmux):
    """A CLI send, an external agent, a row that has died — all just skipped."""
    app, _ = make_app()
    async with app.run_test():
        app.start_route_anim(None, CHILD["id"])
        app.start_route_anim("cli", CHILD["id"])
        app.start_route_anim(PARENT["id"], "ffffffffffff")
        app.start_route_anim(PARENT["id"], PARENT["id"])
        assert app._route_anims == []
        assert app._anim_timer is None


async def test_the_trace_starts_on_the_sender_and_reaches_the_target(daemon, tmux):
    app, _ = make_app()
    async with app.run_test():
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        sender = panel._key_widgets[("p", PARENT["id"])]
        target = panel._key_widgets[("p", CHILD["id"])]

        app.start_route_anim(PARENT["id"], CHILD["id"])
        app._tick_route_anims()
        assert SEND_STYLE in _styles(sender)

        path = send_path(app.tree_lines, PARENT["id"], CHILD["id"])
        assert path is not None
        for _ in range(len(path) - 1):
            app._tick_route_anims()
        assert SEND_STYLE in _styles(target)


async def test_a_spawn_animates_from_parent_to_new_child(daemon, tmux):
    """participant.created refreshes the tree so the new child can receive a trace."""
    daemon["answers"]["participants.tree"] = [dict(PARENT, children=[])]
    app, _ = make_app()
    async with app.run_test():
        assert len(app.tree_lines) == 1

        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[dict(CHILD)])]
        daemon["answers"]["bus.tail"] = [SPAWN_ROW]
        await app._refresh_anim()

        assert len(app.tree_lines) == 2
        assert len(app._route_anims) == 1
        app._tick_route_anims()
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        parent_widget = panel._key_widgets[("p", PARENT["id"])]
        assert SEND_STYLE in _styles(parent_widget)


async def test_a_promptless_spawn_does_not_animate(daemon, tmux):
    """A bare child pane should appear on the next normal tree refresh, without a trace."""
    daemon["answers"]["participants.tree"] = [dict(PARENT, children=[])]
    app, _ = make_app()
    async with app.run_test():
        row = {**SPAWN_ROW, "payload": {**SPAWN_ROW["payload"], "has_prompt": False}}
        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[dict(CHILD)])]
        daemon["answers"]["bus.tail"] = [row]
        await app._refresh_anim()

        assert len(app.tree_lines) == 1
        assert app._route_anims == []
        assert app.anim_cursor == 1


async def test_an_await_pulses_grey_between_caller_and_target(daemon, tmux):
    app, _ = make_app()
    async with app.run_test():
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        sender = panel._key_widgets[("p", PARENT["id"])]
        target = panel._key_widgets[("p", CHILD["id"])]

        daemon["answers"]["bus.tail"] = [AWAIT_START_ROW]
        await app._refresh_anim()
        assert len(app._await_anims) == 1

        app._tick_route_anims()
        # The caller is the parent here: the line departs along its own branch
        # — the only way across to the column its child's rail hangs in — then
        # drops to the child and reaches for its name.
        assert set(_overlay_glyphs(sender)) == set("┕━")
        assert set(_overlay_glyphs(target)) == set("┃┗━")
        assert (1, len(app.tree_lines[1][3])) not in target._overlay
        assert app_mod._await_route_style(0, 0) in _overlay_styles(sender)
        assert app_mod._await_route_style(0, 6) in _overlay_styles(target)
        # No bold: it would promote the grey into the bright palette and make
        # the await line brighter than the working agents it runs between.
        assert not any("bold" in style for style in _overlay_styles(target))
        assert any(style.startswith("#") for style in _overlay_styles(target))
        assert app._anim_timer is not None

        daemon["answers"]["bus.tail"] = [AWAIT_END_ROW]
        await app._refresh_anim()
        assert app._await_anims == {}
        assert app._anim_timer is None
        assert sender._overlay is None
        assert target._overlay is None


async def test_a_child_awaiting_its_parent_draws_the_dashes_on_the_parent(daemon, tmux):
    """The other direction of the same edge, and the only source of ``┕``.

    The route arrives along the parent's own branch from the right, so no
    extension is added and the last cell drawn is the corner itself.
    """
    app, _ = make_app()
    async with app.run_test():
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        parent_widget = panel._key_widgets[("p", PARENT["id"])]

        app.start_await_anim("token", "handle", CHILD["id"], PARENT["id"])
        app._tick_route_anims()

        assert set(_overlay_glyphs(parent_widget)) == set("┕━")
        assert str(parent_widget.render()).split("\n")[1].startswith("┕━━ ")


async def test_the_await_line_reads_as_one_line_from_caller_to_awaited(daemon, tmux):
    """The whole point, read off the screen rather than off the coordinates.

    A parent awaiting its child: the parent's own branch turns heavy, the rail
    between them drops, and the child's branch reaches for its name. One
    unbroken line — bar the cwd row, where the parent's text occupies the
    column the child's rail would continue in and the line is dashed.
    """
    app, _ = make_app()
    async with app.run_test():
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        sender = panel._key_widgets[("p", PARENT["id"])]
        target = panel._key_widgets[("p", CHILD["id"])]
        plain = [str(sender.render()).split("\n"), str(target.render()).split("\n")]

        app.start_await_anim("token", "handle", PARENT["id"], CHILD["id"])
        app._tick_route_anims()
        drawn = [str(sender.render()).split("\n"), str(target.render()).split("\n")]

        assert drawn[0][1].startswith("┕━━ ")  # the caller departs along its own branch
        assert drawn[1][0].startswith("    ┃")  # the rail down to the child
        assert drawn[1][1].startswith("    ┗━━ ")  # into the awaited child
        # Only the rails moved: everything from the status glyph rightwards,
        # and the cwd rows, are the characters the tree drew.
        assert drawn[0][1][4:] == plain[0][1][4:]
        assert drawn[0][2] == plain[0][2]
        assert drawn[1][1][8:] == plain[1][1][8:]
        assert drawn[1][2] == plain[1][2]


async def test_an_await_whose_end_row_never_comes_reaps_itself(daemon, tmux):
    """A missed `job.await.end` must not leave a pulse running for the session.

    `bus.tail` returns only the newest rows after the cursor, so the end row
    can be dropped outright — and the daemon can die mid-await. Either way the
    pulse has to expire on its own.
    """
    app, _ = make_app()
    async with app.run_test():
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        target = panel._key_widgets[("p", CHILD["id"])]

        daemon["answers"]["bus.tail"] = [AWAIT_START_ROW]
        await app._refresh_anim()
        app._tick_route_anims()
        assert len(app._await_anims) == 1
        assert target._overlay is not None

        anim = next(iter(app._await_anims.values()))
        anim.started -= app_mod.AWAIT_ANIM_TTL + 1

        app._tick_route_anims()
        assert app._await_anims == {}
        assert app._anim_timer is None
        assert target._overlay is None


async def test_an_expired_await_does_not_keep_a_slot_from_a_live_one(daemon, tmux):
    """The cap counts live pulses; a stale one is reaped before it turns one away."""
    app, _ = make_app()
    async with app.run_test():
        for index in range(app_mod.MAX_AWAIT_ANIMS):
            app.start_await_anim(f"token-{index}", "handle", PARENT["id"], CHILD["id"])
        assert len(app._await_anims) == app_mod.MAX_AWAIT_ANIMS

        app.start_await_anim("one-too-many", "handle", PARENT["id"], CHILD["id"])
        assert len(app._await_anims) == app_mod.MAX_AWAIT_ANIMS

        for anim in app._await_anims.values():
            anim.started -= app_mod.AWAIT_ANIM_TTL + 1
        app.start_await_anim("after-the-reaping", "handle", PARENT["id"], CHILD["id"])
        assert len(app._await_anims) == 1


async def test_a_batch_of_rows_refreshes_the_tree_once(daemon, tmux):
    """One daemon round-trip per batch, not one per row.

    A burst of spawns used to pay for the same answer several times over, in
    sequence, on the frame that could least afford it.
    """
    app, _ = make_app()
    async with app.run_test():
        before = len(daemon["client"].asked("participants.tree"))
        daemon["answers"]["bus.tail"] = [
            dict(SPAWN_ROW, id=10),
            dict(AWAIT_START_ROW, id=11),
            dict(SPAWN_ROW, id=12),
        ]
        await app._refresh_anim()
        assert len(daemon["client"].asked("participants.tree")) == before + 1


async def test_the_await_route_is_found_once_per_tree_revision(daemon, tmux):
    """Ten frames a second over a tree that changes once a second: cache it."""
    app, _ = make_app()
    async with app.run_test():
        calls: list[tuple] = []
        real = app_mod.await_highlight_cells

        def counted(lines, from_id, to_id):
            calls.append((from_id, to_id))
            return real(lines, from_id, to_id)

        app_mod.await_highlight_cells = counted  # type: ignore[assignment]
        try:
            app.start_await_anim("token", "handle", PARENT["id"], CHILD["id"])
            app._tick_route_anims()
            app._tick_route_anims()
            app._tick_route_anims()
            assert len(calls) == 1

            await app._refresh_tree()  # the tree moved; the route may have too
            app._tick_route_anims()
            assert len(calls) == 2
        finally:
            app_mod.await_highlight_cells = real  # type: ignore[assignment]


async def test_cross_root_trace_walks_every_cell_to_the_other_roots_child(daemon, tmux):
    """Long root-to-child routes must not be squeezed into a fixed frame count."""
    other_root = {
        **PARENT,
        "id": "cccccccccccc",
        "name": "other-root",
        "tmux_pane": "%12",
        "children": [dict(CHILD)],
    }
    daemon["answers"]["participants.tree"] = [dict(PARENT, children=[]), other_root]
    app, _ = make_app()
    async with app.run_test():
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        path = send_path(app.tree_lines, PARENT["id"], CHILD["id"])
        assert path is not None
        assert len(path) > 13

        app.start_route_anim(PARENT["id"], CHILD["id"])
        for expected in path:
            app._tick_route_anims()
            leaf_index, row_in_leaf = app_mod.cell_leaf(expected)
            key = app.tree_lines[leaf_index][2]
            widget = panel._key_widgets[key]
            assert isinstance(widget, app_mod.AgentLeaf)
            assert set(widget._overlay or {}) == {(row_in_leaf, expected[1])}
            assert next(iter((widget._overlay or {}).values())) in "━┃┏┓┗┛"


async def test_the_animation_timer_runs_only_while_something_is_in_flight(daemon, tmux):
    """It starts on the first trace and stops with the last one, leaving no glyph."""
    app, _ = make_app()
    async with app.run_test():
        assert app._anim_timer is None
        app.start_route_anim(PARENT["id"], CHILD["id"])
        assert app._anim_timer is not None

        path = send_path(app.tree_lines, PARENT["id"], CHILD["id"])
        assert path is not None
        for _ in range(len(path) + 1):
            app._tick_route_anims()

        assert app._route_anims == []
        assert app._anim_timer is None
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        assert panel._overlaid == set()
        for widget in panel._key_widgets.values():
            assert SEND_STYLE not in _styles(widget)


async def test_two_sends_animate_at_once(daemon, tmux):
    """Concurrent rather than queued: a trace shown late lies about when it landed."""
    app, _ = make_app()
    async with app.run_test():
        app.start_route_anim(PARENT["id"], CHILD["id"])
        app.start_route_anim(CHILD["id"], PARENT["id"])
        assert len(app._route_anims) == 2
        app._tick_route_anims()
        assert len(app._route_anims) == 2


async def test_a_flood_of_sends_is_capped(daemon, tmux):
    app, _ = make_app()
    async with app.run_test():
        for _ in range(app_mod.MAX_TRACE_ANIMS + 5):
            app.start_route_anim(PARENT["id"], CHILD["id"])
        assert len(app._route_anims) == app_mod.MAX_TRACE_ANIMS


async def test_a_trace_whose_participant_vanishes_is_dropped_cleanly(daemon, tmux):
    """The tree refreshes every second; an animation must survive losing an end."""
    app, _ = make_app()
    async with app.run_test():
        app.start_route_anim(PARENT["id"], CHILD["id"])
        app._tick_route_anims()
        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[])]
        await app._refresh_tree()
        app._tick_route_anims()
        assert app._route_anims == []
        assert app._anim_timer is None


async def test_a_daemon_that_will_not_answer_leaves_the_animation_alone(daemon, tmux):
    app, notes = make_app()
    async with app.run_test():
        daemon["broken"] = {"bus.tail"}
        await app._refresh_anim()
        assert app._route_anims == []
    assert notes == []


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
