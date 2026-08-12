"""What the régie owes tmux when it exits.

The régie borrows two things from tmux while it runs: an agent's pane, joined
into its own window, and the session's `mouse` option. Both have to go back.
Neither failure is loud — the agent keeps running in a window whose other
occupant has died, and the mouse stays on in a session the user never asked to
change — so they are asserted here rather than noticed later.

The app is constructed but never run: teardown is deliberately callable without
a mounted UI, because it also has to work from `on_unmount` while Textual is
already shutting down.
"""

from __future__ import annotations

import pytest

from theater.regie import app as app_mod
from theater.regie.app import RegieApp


@pytest.fixture
def tmux_calls(monkeypatch):
    """Record every tmux call teardown makes, in order."""
    calls: list[tuple] = []

    async def fake_break_pane(pane_id, *, target_window=None):
        calls.append(("break", pane_id))

    async def fake_set_option(name, value, *, target):
        calls.append(("set", name, value, target))

    async def fake_unset_option(name, *, target):
        calls.append(("unset", name, target))

    monkeypatch.setattr(app_mod.panes, "break_pane", fake_break_pane)
    monkeypatch.setattr(app_mod.tmux, "set_option", fake_set_option)
    monkeypatch.setattr(app_mod.tmux, "unset_option", fake_unset_option)
    return calls


def _app(*, staged=None, session="$1", mouse_set=True, mouse_prev=None) -> RegieApp:
    app = RegieApp()
    app.staged_pane = staged
    app.my_session = session
    app._mouse_set = mouse_set
    app._mouse_prev = mouse_prev
    return app


# ---- unstaging ----------------------------------------------------------


async def test_a_staged_pane_is_broken_out_on_exit(tmux_calls):
    """Otherwise the agent shares a window with a TUI that no longer exists."""
    await _app(staged="%7")._teardown()
    assert ("break", "%7") in tmux_calls


async def test_nothing_is_broken_out_when_nothing_is_staged(tmux_calls):
    await _app(staged=None)._teardown()
    assert not [c for c in tmux_calls if c[0] == "break"]


async def test_teardown_runs_once_even_though_two_paths_call_it(tmux_calls):
    """`action_quit` and `on_unmount` both call it; the second must be a no-op."""
    app = _app(staged="%7")
    await app._teardown()
    await app._teardown()
    assert [c for c in tmux_calls if c[0] == "break"] == [("break", "%7")]


async def test_a_failed_unstage_does_not_block_the_mouse_restore(monkeypatch, tmux_calls):
    """A dead pane is a normal way to exit; the option still has to go back."""
    async def boom(pane_id, *, target_window=None):
        raise RuntimeError("pane is gone")

    monkeypatch.setattr(app_mod.panes, "break_pane", boom)
    await _app(staged="%7")._teardown()
    assert ("unset", "mouse", "$1") in tmux_calls


# ---- mouse option -------------------------------------------------------


async def test_an_option_we_added_is_removed_not_pinned(tmux_calls):
    """The session had no override before us, so it must have none after."""
    await _app(mouse_prev=None)._teardown()
    assert ("unset", "mouse", "$1") in tmux_calls


async def test_a_pre_existing_value_is_put_back_verbatim(tmux_calls):
    await _app(mouse_prev="off")._teardown()
    assert ("set", "mouse", "off", "$1") in tmux_calls


async def test_an_option_we_never_set_is_left_alone(tmux_calls):
    """Enable can fail. Restoring then would clobber a setting we never touched."""
    await _app(mouse_set=False)._teardown()
    assert not [c for c in tmux_calls if c[0] in ("set", "unset")]


async def test_the_mouse_is_left_alone_when_the_session_is_unknown(tmux_calls):
    """Outside tmux there is no session id, and so nothing to scope an option to."""
    await _app(session=None)._teardown()
    assert not [c for c in tmux_calls if c[0] in ("set", "unset")]


# ---- quit ---------------------------------------------------------------


async def test_quitting_tears_down_before_exiting(monkeypatch, tmux_calls):
    """Teardown cannot wait for unmount: awaits get cancelled during shutdown."""
    app = _app(staged="%7")
    exited: list[bool] = []
    monkeypatch.setattr(app, "exit", lambda *a, **k: exited.append(True))
    await app.action_quit()
    assert ("break", "%7") in tmux_calls
    assert exited == [True]
