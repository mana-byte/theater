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
from theater.regie.tree import render_tree

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
    settings = Config(regie=RegieSection(tree_interval=60, bus_interval=60, **regie))
    app = RegieApp(settings)
    app.notify = lambda msg, **kw: None  # type: ignore[method-assign]
    return app


def _panel(app: RegieApp) -> app_mod.TreePanel:
    return app.query_one("#tree-panel", app_mod.TreePanel)


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
        assert "no participants" in str(panel.children[0].render())

        # Back to populated.
        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[dict(CHILD)])]
        await app._refresh_tree()
        await pilot.pause()
        assert len(panel.children) == 2
        assert "no participants" not in str(panel.children[0].render())
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
