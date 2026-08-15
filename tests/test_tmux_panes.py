"""Assert the exact argv passed to tmux by the staging primitives.

Same principle as test_tmux_client.py: argv is the only thing that can be
checked without a tmux server, and it is the most likely thing to break
silently.
"""

from __future__ import annotations

import pytest

from theater.tmux import client, panes


@pytest.fixture(autouse=True)
def _reset_version_cache():
    client.reset_version_cache()
    yield
    client.reset_version_cache()


def _set_version(monkeypatch, version: str | None):
    """Pre-seed the version cache so break_pane sees the desired tmux version."""
    client._VERSION_CACHE[0] = version


async def _capture_argv(monkeypatch):
    captured: list[list[str]] = []

    async def fake_run(*args: str, check: bool = True) -> str:
        captured.append(list(args))
        # break-pane -P -F '#{window_id}' returns a window id; everything
        # else that checks the return expects a pane id.
        if args and args[0] == "break-pane" and "-P" in args:
            return "@99"
        return "%99"

    monkeypatch.setattr(panes, "run", fake_run)
    return captured


async def test_break_pane_basic(monkeypatch):
    _set_version(monkeypatch, "3.4")
    captured = await _capture_argv(monkeypatch)
    await panes.break_pane("%5")
    argv = captured[0]
    assert argv[0] == "break-pane"
    assert "-d" in argv
    assert "-s" in argv
    assert argv[argv.index("-s") + 1] == "%5"
    assert "-n" not in argv
    assert "-P" not in argv
    assert "-F" not in argv
    assert len(captured) == 1


async def test_break_pane_with_window_name(monkeypatch):
    _set_version(monkeypatch, "3.4")
    captured = await _capture_argv(monkeypatch)
    await panes.break_pane("%5", target_window="vibe-parked")
    argv = captured[0]
    assert "-n" in argv
    assert argv[argv.index("-n") + 1] == "vibe-parked"
    assert "-P" not in argv
    assert "-F" not in argv
    assert len(captured) == 1


async def test_break_pane_37_without_window_name_passes_placeholder_n(monkeypatch):
    """On tmux exactly 3.7, -n is always passed to avoid the segfault."""
    _set_version(monkeypatch, "3.7")
    captured = await _capture_argv(monkeypatch)
    await panes.break_pane("%5")
    argv = captured[0]
    assert argv[0] == "break-pane"
    assert "-d" in argv
    assert "-s" in argv
    assert argv[argv.index("-s") + 1] == "%5"
    assert "-n" in argv
    assert argv[argv.index("-n") + 1] == "theater"
    assert "-P" in argv
    assert "-F" in argv
    assert argv[argv.index("-F") + 1] == "#{window_id}"
    # No rename-window when no target name was requested.
    assert len(captured) == 1


async def test_break_pane_37_with_window_name_issues_rename(monkeypatch):
    """On 3.7, -n is ignored, so the real name is set via rename-window."""
    _set_version(monkeypatch, "3.7")
    captured = await _capture_argv(monkeypatch)
    await panes.break_pane("%5", target_window="vibe-parked")
    assert len(captured) == 2

    break_argv = captured[0]
    assert break_argv[0] == "break-pane"
    assert "-n" in break_argv
    assert break_argv[break_argv.index("-n") + 1] == "vibe-parked"
    assert "-P" in break_argv
    assert "-F" in break_argv

    rename_argv = captured[1]
    assert rename_argv[0] == "rename-window"
    assert rename_argv[rename_argv.index("-t") + 1] == "@99"
    assert rename_argv[-1] == "vibe-parked"


async def test_break_pane_37a_is_not_gated(monkeypatch):
    """3.7a reverted the bug, so the 3.7 workaround must not fire."""
    _set_version(monkeypatch, "3.7a")
    captured = await _capture_argv(monkeypatch)
    await panes.break_pane("%5")
    argv = captured[0]
    assert "-n" not in argv
    assert "-P" not in argv
    assert len(captured) == 1


async def test_break_pane_37b_is_not_gated(monkeypatch):
    _set_version(monkeypatch, "3.7b")
    captured = await _capture_argv(monkeypatch)
    await panes.break_pane("%5")
    argv = captured[0]
    assert "-n" not in argv
    assert "-P" not in argv
    assert len(captured) == 1


async def test_break_pane_37_with_window_name_argv_matches_non_37_plus_rename(monkeypatch):
    """The 3.7 break-pane argv should be the non-3.7 argv plus -P -F -n."""
    _set_version(monkeypatch, "3.7")
    captured = await _capture_argv(monkeypatch)
    await panes.break_pane("%5", target_window="vibe-parked")
    break_argv = captured[0]
    # The core is still break-pane -d -s %5 -n vibe-parked, same as non-3.7,
    # but with -P -F #{window_id} added to capture the new window id.
    assert "break-pane" in break_argv
    assert "-d" in break_argv
    assert "-s" in break_argv
    assert break_argv[break_argv.index("-s") + 1] == "%5"
    assert "-n" in break_argv
    assert break_argv[break_argv.index("-n") + 1] == "vibe-parked"


async def test_join_pane(monkeypatch):
    captured = await _capture_argv(monkeypatch)
    await panes.join_pane("%5", target_window="@3")
    argv = captured[0]
    assert argv[0] == "join-pane"
    assert "-d" in argv
    assert "-h" in argv  # horizontal split: side-by-side, not stacked
    assert argv[argv.index("-s") + 1] == "%5"
    assert argv[argv.index("-t") + 1] == "@3"


async def test_join_pane_vertical_when_requested(monkeypatch):
    captured = await _capture_argv(monkeypatch)
    await panes.join_pane("%5", target_window="@3", horizontal=False)
    argv = captured[0]
    assert "-h" not in argv


async def test_resize_pane_width_and_height(monkeypatch):
    captured = await _capture_argv(monkeypatch)
    await panes.resize_pane("%5", width=120, height=40)
    assert len(captured) == 2
    w, h = captured
    assert w[0] == "resize-pane"
    assert "-x" in w
    assert w[w.index("-x") + 1] == "120"
    assert "-t" in w
    assert w[w.index("-t") + 1] == "%5"
    assert h[h.index("-y") + 1] == "40"


async def test_resize_pane_only_width(monkeypatch):
    captured = await _capture_argv(monkeypatch)
    await panes.resize_pane("%5", width=80)
    assert len(captured) == 1


async def test_select_pane(monkeypatch):
    captured = await _capture_argv(monkeypatch)
    await panes.select_pane("%5")
    argv = captured[0]
    assert argv[0] == "select-pane"
    assert argv[argv.index("-t") + 1] == "%5"


async def test_split_window_vertical(monkeypatch):
    captured = await _capture_argv(monkeypatch)
    result = await panes.split_window(target="@1", command=["vibe"])
    assert result == "%99"
    argv = captured[0]
    assert argv[0] == "split-window"
    assert "-v" in argv
    assert "-d" in argv
    assert "-P" in argv
    assert "-F" in argv


async def test_split_window_horizontal(monkeypatch):
    captured = await _capture_argv(monkeypatch)
    await panes.split_window(target="@1", vertical=False, command=["vibe"])
    argv = captured[0]
    assert "-h" in argv
    assert "-v" not in argv


async def test_new_window_named(monkeypatch):
    captured = await _capture_argv(monkeypatch)
    result = await panes.new_window_named(session="0", name="stage", cwd="/tmp", command=["vibe"])
    assert result == "%99"
    argv = captured[0]
    assert argv[0] == "new-window"
    assert "-P" in argv
    assert "-F" in argv
    assert argv[argv.index("-t") + 1] == "0:"
    assert argv[argv.index("-n") + 1] == "stage"
    assert "--" in argv


async def test_swap_panes(monkeypatch):
    captured = await _capture_argv(monkeypatch)
    await panes.swap_panes("%5", "%6")
    argv = captured[0]
    assert argv[0] == "swap-pane"
    assert argv[argv.index("-s") + 1] == "%5"
    assert argv[argv.index("-t") + 1] == "%6"


async def test_window_for_pane(monkeypatch):
    sep = "\u241e"

    async def fake_run(*args: str, check: bool = True) -> str:
        return f"%5{sep}@3\n%6{sep}@4"

    monkeypatch.setattr(panes, "run", fake_run)
    result = await panes.window_for_pane("%5")
    assert result == "@3"
    result = await panes.window_for_pane("%99")
    assert result is None
