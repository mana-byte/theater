"""Tests for the tree panel's reconciliation behaviour.

The panel reconciles by key rather than rebuilding on every refresh, so that
per-widget state (a hover class, an animation timer) survives a tick. What
matters is identity — the same participant id across two refreshes must yield
the same widget object — and correctness of removal, insertion, and ordering
when the set of rows changes.

Uses the same fake daemon and tmux fixtures as test_regie_app, and drives the
real app through Textual's test pilot. No test sleeps.
"""

from __future__ import annotations

import pytest

from theater.config import Config, RegieSection
from theater.regie import app as app_mod
from theater.regie.app import AgentLeaf, RegieApp
from theater.regie.tree import SEND_STYLE, render_tree

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

THIRD = {
    "id": "cccccccccccc",
    "tier": "spawned",
    "harness": "opencode",
    "status": "idle",
    "cwd": "/tmp/proj/third",
    "tmux_pane": "%12",
    "addressable": True,
    "children": [],
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


def make_app(**regie) -> RegieApp:
    """An app with slow timers so the only refreshes are the ones at mount."""
    values = {"tree_interval": 60, "bus_interval": 60, "startup_reveal": False}
    values.update(regie)
    settings = Config(regie=RegieSection(**values))
    app = RegieApp(settings)
    app.notify = lambda msg, **kw: None  # type: ignore[method-assign]
    return app


def _panel(app: RegieApp) -> app_mod.TreePanel:
    return app.query_one("#tree-panel", app_mod.TreePanel)


def _styles(widget) -> list[str]:
    return [span.style for span in widget.render().spans]


def _finish_leaf_reveal(app: RegieApp) -> None:
    for _ in range(300):
        if not app._leaf_reveal.active:
            return
        app._tick_leaf_reveal()
    pytest.fail("leaf reveal did not finish")


async def test_same_ids_across_refresh_yield_same_widgets(daemon, tmux):
    """The property that matters: identity, not just equality."""
    app = make_app()
    async with app.run_test():
        panel = _panel(app)
        before = list(panel.children)
        assert len(before) == 2  # parent and child

        # Refresh with the same data.
        await app._refresh_tree()
        after = list(panel.children)
        assert len(after) == 2
        assert before[0] is after[0]
        assert before[1] is after[1]


async def test_a_disappeared_participant_is_unmounted(daemon, tmux):
    """When an agent dies, its widget is gone from the panel."""
    app = make_app()
    async with app.run_test() as pilot:
        panel = _panel(app)
        assert len(panel.children) == 2

        # Hold the child widget reference before it disappears.
        child_key = ("p", CHILD["id"])
        child_widget = panel._key_widgets[child_key]

        # Child disappears.
        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[])]
        await app._refresh_tree()
        await pilot.pause()

        assert len(panel.children) == 1
        # The surviving widget is the parent's.
        assert panel._key_widgets[("p", PARENT["id"])] is panel.children[0]
        # The gone widget is genuinely unmounted, not orphaned.
        assert child_widget not in list(panel.children)
        assert child_widget.is_running is False
        assert child_widget.parent is None


async def test_agent_spawned_leaf_retires_before_unmount(daemon, tmux, monkeypatch):
    monkeypatch.setattr(app_mod, "STARTUP_REVEAL_INTERVAL", 60.0)
    child = {**CHILD, "parent_id": PARENT["id"]}
    daemon["answers"]["participants.tree"] = [{**PARENT, "children": [child]}]
    app = make_app(startup_reveal=True)

    async with app.run_test() as pilot:
        panel = _panel(app)
        key = ("p", child["id"])
        leaf = panel._key_widgets[key]
        _finish_leaf_reveal(app)
        panel.apply_cursor(1, child["tmux_pane"])

        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[])]
        await app._refresh_tree()

        assert key not in panel._key_widgets
        assert leaf in panel.children
        assert not leaf.has_class("tree-cursor")
        assert not leaf.has_class("tree-staged")
        assert not leaf.has_class("tree-trajectory-staged")
        assert leaf._stage_marker is None
        assert app._leaf_retirement_timer is not None

        width = leaf.required_reveal_width
        app._tick_leaf_retirement()
        assert leaf._reveal is not None and leaf._reveal < width
        while app._leaf_retirement.active:
            app._tick_leaf_retirement()
        await pilot.pause()

        assert leaf not in panel.children
        assert leaf.parent is None
        assert app._leaf_retirement_timer is None


async def test_user_killed_child_unmounts_without_retirement(daemon, tmux, monkeypatch):
    monkeypatch.setattr(app_mod, "STARTUP_REVEAL_INTERVAL", 60.0)
    child = {**CHILD, "parent_id": PARENT["id"]}
    daemon["answers"]["participants.tree"] = [{**PARENT, "children": [child]}]
    app = make_app(startup_reveal=True)

    async with app.run_test() as pilot:
        panel = _panel(app)
        key = ("p", child["id"])
        leaf = panel._key_widgets[key]
        _finish_leaf_reveal(app)
        app.cursor = 1
        client = daemon["client"]
        original_call = client.call

        async def remove_child(method: str, **params):
            result = await original_call(method, **params)
            if method == "participant.kill":
                daemon["answers"]["participants.tree"] = [dict(PARENT, children=[])]
            return result

        client.call = remove_child
        await app.action_kill()
        await pilot.pause()

        assert leaf not in panel.children
        assert not app._leaf_retirement.active
        assert app._leaf_retirement_timer is None


async def test_retiring_middle_leaf_keeps_its_prior_slot(daemon, tmux, monkeypatch):
    monkeypatch.setattr(app_mod, "STARTUP_REVEAL_INTERVAL", 60.0)
    child = {**CHILD, "parent_id": PARENT["id"]}
    third = {**THIRD, "parent_id": PARENT["id"]}
    daemon["answers"]["participants.tree"] = [{**PARENT, "children": [child, third]}]
    app = make_app(startup_reveal=True)

    async with app.run_test():
        panel = _panel(app)
        parent = panel._key_widgets[("p", PARENT["id"])]
        leaf = panel._key_widgets[("p", child["id"])]
        sibling = panel._key_widgets[("p", third["id"])]
        _finish_leaf_reveal(app)

        daemon["answers"]["participants.tree"] = [{**PARENT, "children": [third]}]
        await app._refresh_tree()

        assert list(panel.children)[:3] == [parent, leaf, sibling]


async def test_sequential_retirements_preserve_their_interleaved_order(daemon, tmux, monkeypatch):
    monkeypatch.setattr(app_mod, "STARTUP_REVEAL_INTERVAL", 60.0)
    child = {**CHILD, "parent_id": PARENT["id"]}
    middle = {**THIRD, "parent_id": PARENT["id"]}
    later = {**CHILD, "id": "dddddddddddd", "parent_id": PARENT["id"]}
    daemon["answers"]["participants.tree"] = [{**PARENT, "children": [child, middle, later]}]
    app = make_app(startup_reveal=True)

    async with app.run_test():
        panel = _panel(app)
        parent = panel._key_widgets[("p", PARENT["id"])]
        child_leaf = panel._key_widgets[("p", child["id"])]
        middle_leaf = panel._key_widgets[("p", middle["id"])]
        later_leaf = panel._key_widgets[("p", later["id"])]
        _finish_leaf_reveal(app)

        daemon["answers"]["participants.tree"] = [{**PARENT, "children": [middle, later]}]
        await app._refresh_tree()
        daemon["answers"]["participants.tree"] = [{**PARENT, "children": [middle]}]
        await app._refresh_tree()

        assert list(panel.children)[:4] == [parent, child_leaf, middle_leaf, later_leaf]


async def test_retiring_mid_reveal_never_expands_or_leaves_a_reveal_timer(
    daemon, tmux, monkeypatch
):
    monkeypatch.setattr(app_mod, "STARTUP_REVEAL_INTERVAL", 60.0)
    daemon["answers"]["participants.tree"] = [dict(PARENT, children=[])]
    app = make_app(startup_reveal=True)

    async with app.run_test():
        panel = _panel(app)
        _finish_leaf_reveal(app)
        child = {**CHILD, "parent_id": PARENT["id"]}
        daemon["answers"]["participants.tree"] = [{**PARENT, "children": [child]}]
        await app._refresh_tree()
        leaf = panel._key_widgets[("p", child["id"])]
        app._tick_leaf_reveal()
        visible = leaf.visible_reveal_width
        assert visible < leaf.required_reveal_width

        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[])]
        await app._refresh_tree()

        assert leaf._reveal == visible
        assert app._leaf_reveal_timer is None


async def test_empty_hint_waits_for_last_retiring_leaf(daemon, tmux, monkeypatch):
    monkeypatch.setattr(app_mod, "STARTUP_REVEAL_INTERVAL", 60.0)
    child = {**CHILD, "parent_id": PARENT["id"]}
    daemon["answers"]["participants.tree"] = [{**PARENT, "children": [child]}]
    app = make_app(startup_reveal=True)

    async with app.run_test() as pilot:
        panel = _panel(app)
        leaf = panel._key_widgets[("p", child["id"])]
        _finish_leaf_reveal(app)
        daemon["answers"]["participants.tree"] = []
        await app._refresh_tree()

        assert leaf in panel.children
        assert not list(panel.query(app_mod.EmptyTreeState))
        while app._leaf_retirement.active:
            app._tick_leaf_retirement()
        await pilot.pause()

        assert leaf not in panel.children
        assert panel.query_one(app_mod.EmptyTreeState)


async def test_palette_root_disappears_immediately(daemon, tmux, monkeypatch):
    monkeypatch.setattr(app_mod, "STARTUP_REVEAL_INTERVAL", 60.0)
    app = make_app(startup_reveal=True)

    async with app.run_test() as pilot:
        panel = _panel(app)
        root = panel._key_widgets[("p", PARENT["id"])]
        _finish_leaf_reveal(app)
        daemon["answers"]["participants.tree"] = []
        await app._refresh_tree()
        await pilot.pause()

        assert root not in panel.children
        assert app._leaf_retirement_timer is None


async def test_disabled_retirement_removes_agent_spawned_leaf_immediately(daemon, tmux):
    child = {**CHILD, "parent_id": PARENT["id"]}
    daemon["answers"]["participants.tree"] = [{**PARENT, "children": [child]}]
    app = make_app(startup_reveal=False)

    async with app.run_test() as pilot:
        panel = _panel(app)
        leaf = panel._key_widgets[("p", child["id"])]
        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[])]
        await app._refresh_tree()
        await pilot.pause()

        assert leaf not in panel.children
        assert app._leaf_retirement_timer is None


async def test_retiring_leaf_reappears_without_a_duplicate_widget(daemon, tmux, monkeypatch):
    monkeypatch.setattr(app_mod, "STARTUP_REVEAL_INTERVAL", 60.0)
    child = {**CHILD, "parent_id": PARENT["id"]}
    daemon["answers"]["participants.tree"] = [{**PARENT, "children": [child]}]
    app = make_app(startup_reveal=True)

    async with app.run_test():
        panel = _panel(app)
        key = ("p", child["id"])
        leaf = panel._key_widgets[key]
        _finish_leaf_reveal(app)

        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[])]
        await app._refresh_tree()
        assert key not in panel._key_widgets

        daemon["answers"]["participants.tree"] = [{**PARENT, "children": [child]}]
        await app._refresh_tree()

        assert panel._key_widgets[key] is leaf
        assert list(panel.children).count(leaf) == 1
        assert app._leaf_retirement_timer is None


async def test_a_new_participant_does_not_disturb_existing_widgets(daemon, tmux):
    """A newly appeared agent's widget is new; the old ones stay put."""
    app = make_app()
    async with app.run_test():
        panel = _panel(app)
        before = list(panel.children)
        assert len(before) == 2

        # A third agent appears.
        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[dict(CHILD), dict(THIRD)])]
        await app._refresh_tree()

        after = list(panel.children)
        assert len(after) == 3
        # The first two widgets are the same objects.
        assert before[0] is after[0]
        assert before[1] is after[1]
        # The third is new.
        assert after[2] is not before[0]
        assert after[2] is not before[1]


async def test_row_order_matches_data_order_after_reconcile(daemon, tmux):
    """Children appear in the same order as the rows from render_tree."""
    app = make_app()
    async with app.run_test():
        panel = _panel(app)
        lines = render_tree(
            daemon["answers"]["participants.tree"],
            daemon["answers"]["participants.unmanaged"],
        )
        keys = [key for _, _, key, _, _ in lines]
        assert list(panel._key_widgets) == keys
        assert [panel._key_widgets[k] for k in keys] == list(panel.children)


async def test_inserted_child_in_the_middle_preserves_order(daemon, tmux):
    """A child inserted between two existing rows does not shift identities."""
    app = make_app()
    async with app.run_test():
        panel = _panel(app)

        # Start: parent -> child.
        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[dict(CHILD)])]
        await app._refresh_tree()
        before = list(panel.children)
        assert len(before) == 2

        # Now: parent -> third -> child (third inserted in the middle).
        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[dict(THIRD), dict(CHILD)])]
        await app._refresh_tree()
        after = list(panel.children)
        assert len(after) == 3

        # Parent is still first.
        assert before[0] is after[0]
        # Child is still the child widget (now at index 2).
        child_widget = panel._key_widgets[("p", CHILD["id"])]
        assert child_widget is after[2]
        # Third is new, in the middle.
        third_widget = panel._key_widgets[("p", THIRD["id"])]
        assert third_widget is after[1]


async def test_zebra_stripe_updates_after_insertion(daemon, tmux):
    """tree-alt is recomputed every tick so insertions keep parity correct."""
    app = make_app()
    async with app.run_test():
        panel = _panel(app)

        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[dict(CHILD)])]
        await app._refresh_tree()

        parent_w = panel._key_widgets[("p", PARENT["id"])]
        child_w = panel._key_widgets[("p", CHILD["id"])]
        assert parent_w.has_class("tree-alt") is True
        assert child_w.has_class("tree-alt") is False

        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[dict(THIRD), dict(CHILD)])]
        await app._refresh_tree()

        third_w = panel._key_widgets[("p", THIRD["id"])]
        assert parent_w.has_class("tree-alt") is True
        assert third_w.has_class("tree-alt") is False
        assert child_w.has_class("tree-alt") is True


async def test_zebra_stripe_ignores_separator_rows(daemon, tmux):
    """A separator between participants must not consume parity."""
    app = make_app()
    async with app.run_test():
        panel = _panel(app)

        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[])]
        daemon["answers"]["participants.unmanaged"] = [
            {"pane": "%99", "harness": "codex", "cwd": "/tmp/other"}
        ]
        await app._refresh_tree()

        participant_widgets = [
            panel._key_widgets[k]
            for k in [("p", PARENT["id"]), ("u", "%99")]
            if k in panel._key_widgets
        ]
        assert len(participant_widgets) == 2
        assert participant_widgets[0].has_class("tree-alt") is True
        assert participant_widgets[1].has_class("tree-alt") is False


async def test_empty_tree_and_back(daemon, tmux):
    """Going to an empty tree and recovering works."""
    app = make_app()
    async with app.run_test() as pilot:
        panel = _panel(app)
        assert len(panel.children) == 2

        # Empty.
        daemon["answers"]["participants.tree"] = []
        daemon["answers"]["participants.unmanaged"] = []
        await app._refresh_tree()
        await pilot.pause()
        assert len(panel.children) == 1
        empty = panel.query_one(app_mod.EmptyTreeState)
        assert str(empty.render()) == app_mod.EMPTY_TREE_HINT
        assert empty.region == panel.content_region
        assert empty.styles.content_align == ("center", "middle")

        # Back to populated.
        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[dict(CHILD)])]
        await app._refresh_tree()
        await pilot.pause()
        assert len(panel.children) == 2
        assert panel._EMPTY_KEY not in panel._key_widgets
        assert not panel.query(app_mod.EmptyTreeState)
        assert ("p", PARENT["id"]) in panel._key_widgets
        assert ("p", CHILD["id"]) in panel._key_widgets


# ---- AgentLeaf and spinner timer ----------------------------------------


async def test_one_widget_per_participant(daemon, tmux):
    """Each participant is one AgentLeaf, not three rows."""
    app = make_app()
    async with app.run_test():
        panel = _panel(app)
        assert len(panel.children) == 2
        assert all(isinstance(w, AgentLeaf) for w in panel.children)


async def test_spinner_timer_exists_only_while_working(daemon, tmux):
    """A working leaf has a timer; an idle leaf does not."""
    app = make_app()
    async with app.run_test():
        panel = _panel(app)
        # PARENT is idle, CHILD is working.
        parent_widget = panel._key_widgets[("p", PARENT["id"])]
        child_widget = panel._key_widgets[("p", CHILD["id"])]
        assert parent_widget._timer is None
        assert child_widget._timer is not None


async def test_spinner_timer_stops_when_status_leaves_working(daemon, tmux):
    """When a working participant becomes idle, the timer stops."""
    app = make_app()
    async with app.run_test():
        panel = _panel(app)
        child_widget = panel._key_widgets[("p", CHILD["id"])]
        assert child_widget._timer is not None

        # Child becomes idle.
        daemon["answers"]["participants.tree"] = [
            dict(PARENT, children=[{**dict(CHILD), "status": "idle"}])
        ]
        await app._refresh_tree()

        assert child_widget._timer is None


async def test_spinner_timer_is_gone_after_unmount(daemon, tmux):
    """When a leaf is unmounted, its timer is stopped."""
    app = make_app()
    async with app.run_test() as pilot:
        panel = _panel(app)
        child_widget = panel._key_widgets[("p", CHILD["id"])]
        assert child_widget._timer is not None

        # Child disappears.
        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[])]
        await app._refresh_tree()
        await pilot.pause()

        assert child_widget._timer is None
        assert child_widget.is_running is False


async def test_spinner_frame_advances_and_wraps(daemon, tmux):
    """Calling _tick advances the frame and wraps at 10."""
    app = make_app()
    async with app.run_test():
        panel = _panel(app)
        child_widget = panel._key_widgets[("p", CHILD["id"])]
        assert child_widget._frame == 0
        child_widget._tick()
        assert child_widget._frame == 1
        # Advance 9 more to wrap.
        for _ in range(9):
            child_widget._tick()
        assert child_widget._frame == 0


# ---- send-trace overlays --------------------------------------------------


async def test_an_overlay_lands_on_one_leaf_and_leaves_the_others_clean(daemon, tmux):
    app = make_app()
    async with app.run_test():
        panel = _panel(app)
        parent_widget = panel._key_widgets[("p", PARENT["id"])]
        child_widget = panel._key_widgets[("p", CHILD["id"])]

        panel.set_overlays({("p", CHILD["id"]): {(1, 4): "━"}})
        assert SEND_STYLE in _styles(child_widget)
        assert SEND_STYLE not in _styles(parent_widget)


async def test_the_next_frame_clears_the_leaf_the_trace_left(daemon, tmux):
    """set_overlays is given the whole picture, so a leaf it omits is cleared."""
    app = make_app()
    async with app.run_test():
        panel = _panel(app)
        parent_widget = panel._key_widgets[("p", PARENT["id"])]
        child_widget = panel._key_widgets[("p", CHILD["id"])]

        panel.set_overlays({("p", PARENT["id"]): {(1, 4): "━"}})
        panel.set_overlays({("p", CHILD["id"]): {(1, 8): "┃"}})
        assert SEND_STYLE not in _styles(parent_widget)
        assert SEND_STYLE in _styles(child_widget)

        panel.set_overlays({})
        assert SEND_STYLE not in _styles(child_widget)
        assert panel._overlaid == set()


async def test_an_overlay_survives_a_tree_refresh(daemon, tmux):
    """A refresh mid-animation must not blink the trace out for a frame.

    The leaf is reconciled in place, and the overlay is per-widget state on
    exactly the same terms as the spinner's timer — that is what surviving a
    tick is for.
    """
    app = make_app()
    async with app.run_test():
        panel = _panel(app)
        child_widget = panel._key_widgets[("p", CHILD["id"])]
        panel.set_overlays({("p", CHILD["id"]): {(1, 8): "┃"}})

        await app._refresh_tree()

        assert panel._key_widgets[("p", CHILD["id"])] is child_widget
        assert SEND_STYLE in _styles(child_widget)


async def test_an_overlay_on_a_leaf_that_has_gone_is_not_an_error(daemon, tmux):
    """The animation drops a step behind the tree; asking for a dead row is fine."""
    app = make_app()
    async with app.run_test() as pilot:
        panel = _panel(app)
        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[])]
        await app._refresh_tree()
        await pilot.pause()

        panel.set_overlays({("p", CHILD["id"]): {(1, 8): "┃"}})
        panel.set_overlays({})
