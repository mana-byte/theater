"""Assert the exact argv passed to tmux by the staging primitives.

Same principle as test_tmux_client.py: argv is the only thing that can be
checked without a tmux server, and it is the most likely thing to break
silently.
"""

from __future__ import annotations

from theater.tmux import panes


async def _capture_argv(monkeypatch):
    captured: list[list[str]] = []

    async def fake_run(*args: str, check: bool = True) -> str:
        captured.append(list(args))
        return "%99"

    monkeypatch.setattr(panes, "run", fake_run)
    return captured


async def test_break_pane_basic(monkeypatch):
    captured = await _capture_argv(monkeypatch)
    await panes.break_pane("%5")
    argv = captured[0]
    assert argv[0] == "break-pane"
    assert "-d" in argv
    assert "-s" in argv
    assert argv[argv.index("-s") + 1] == "%5"
    assert "-n" not in argv


async def test_break_pane_with_window_name(monkeypatch):
    captured = await _capture_argv(monkeypatch)
    await panes.break_pane("%5", target_window="vibe-parked")
    argv = captured[0]
    assert "-n" in argv
    assert argv[argv.index("-n") + 1] == "vibe-parked"


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
    async def fake_run(*args: str, check: bool = True) -> str:
        return "%5\t@3\n%6\t@4"

    monkeypatch.setattr(panes, "run", fake_run)
    result = await panes.window_for_pane("%5")
    assert result == "@3"
    result = await panes.window_for_pane("%99")
    assert result is None
