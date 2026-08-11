"""Assert the exact argv passed to tmux.

argv is the only thing that can be checked without a tmux server, and it is the
only thing that ever broke silently. These tests patch `theater.tmux.client.run`
and `theater.tmux.client.run_sync` at the lowest level, so any argv drift
downstream of the public functions is caught.
"""

from __future__ import annotations

import pytest

from theater.tmux import client


# ---- new_window --------------------------------------------------------


async def _new_window_argv(monkeypatch, **kw):
    captured: list[list[str]] = []

    async def fake_run(*args: str, check: bool = True) -> str:
        captured.append(list(args))
        return "%99"

    monkeypatch.setattr(client, "run", fake_run)
    defaults = dict(
        session="0",
        name="vibe-abc",
        cwd="/tmp",
        command=["vibe", "say hello"],
    )
    defaults.update(kw)
    pane_id = await client.new_window(**defaults)
    assert pane_id == "%99"
    assert len(captured) == 1
    return captured[0]


async def test_new_window_session_target_carries_trailing_colon(monkeypatch):
    """A session named `0` must not be sent bare; tmux would read it as window 0."""
    argv = await _new_window_argv(monkeypatch)
    assert "-t" in argv
    assert argv[argv.index("-t") + 1] == "0:"


async def test_new_window_session_already_ending_colon_not_doubled(monkeypatch):
    argv = await _new_window_argv(monkeypatch, session="myname:")
    assert argv[argv.index("-t") + 1] == "myname:"


async def test_new_window_named_session_keeps_colon(monkeypatch):
    argv = await _new_window_argv(monkeypatch, session="dev")
    assert argv[argv.index("-t") + 1] == "dev:"


async def test_new_window_background_inserts_d_before_P(monkeypatch):
    argv = await _new_window_argv(monkeypatch, background=True)
    assert argv[0:2] == ["new-window", "-d"]
    assert "-P" in argv


async def test_new_window_foreground_omits_d(monkeypatch):
    argv = await _new_window_argv(monkeypatch, background=False)
    assert "-d" not in argv


async def test_new_window_pane_id_format_present(monkeypatch):
    argv = await _new_window_argv(monkeypatch)
    i = argv.index("-F")
    assert argv[i + 1] == "#{pane_id}"
    assert "-P" in argv


async def test_new_window_env_each_var_its_own_e(monkeypatch):
    argv = await _new_window_argv(
        monkeypatch,
        env={"THEATER_ID": "abc", "VIBE_MCP_SERVERS": "[{}]"},
    )
    e_args = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert "THEATER_ID=abc" in e_args
    assert "VIBE_MCP_SERVERS=[{}]" in e_args
    assert len(e_args) == 2


async def test_new_window_command_separated_by_double_dash(monkeypatch):
    argv = await _new_window_argv(monkeypatch, command=["vibe", "say hello"])
    assert "--" in argv
    after = argv[argv.index("--") + 1:]
    assert after == ["vibe", "say hello"]


async def test_new_window_returns_pane_id_and_rejects_non_percent(monkeypatch):
    """A non-%-prefixed response is garbage, not a pane id."""
    bad = {"0": "not-a-pane"}

    async def fake_run(*args: str, check: bool = True) -> str:
        return bad.get(args[0], "0")

    # the helper above asserts == "%99" already; test the failure path directly
    monkeypatch.setattr(client, "run", fake_run)

    async def returns_garbage(*a, **kw) -> str:
        return "garbage"

    monkeypatch.setattr(client, "run", returns_garbage)
    with pytest.raises(client.TmuxError):
        await client.new_window(
            session="0:", name="x", cwd="/tmp", command=["vibe"]
        )


# ---- list_panes --------------------------------------------------------


async def _list_panes_argv(monkeypatch, session=None):
    captured: list[list[str]] = []

    async def fake_run(*args: str, check: bool = True) -> str:
        captured.append(list(args))
        return ""

    monkeypatch.setattr(client, "run", fake_run)
    await client.list_panes(session=session) if session is not None else await client.list_panes()
    assert len(captured) == 1
    return captured[0]


async def test_list_panes_no_session_uses_a(monkeypatch):
    argv = await _list_panes_argv(monkeypatch)
    assert "-a" in argv
    assert "-s" not in argv
    assert "-t" not in argv


async def test_list_panes_session_scope_uses_s_and_t_with_colon(monkeypatch):
    argv = await _list_panes_argv(monkeypatch, session="0")
    assert "-s" in argv
    i = argv.index("-t")
    assert argv[i + 1] == "0:"


async def test_list_panes_named_session_gets_colon(monkeypatch):
    argv = await _list_panes_argv(monkeypatch, session="dev")
    assert argv[argv.index("-t") + 1] == "dev:"


async def test_list_panes_session_already_colon_terminated_not_doubled(monkeypatch):
    argv = await _list_panes_argv(monkeypatch, session="dev:")
    assert argv[argv.index("-t") + 1] == "dev:"


async def test_list_panes_uses_pane_format(monkeypatch):
    argv = await _list_panes_argv(monkeypatch, session="dev")
    i = argv.index("-F")
    assert argv[i + 1] == client._PANE_FORMAT


# ---- kill_pane / send_keys: targets are pane ids, not sessions ---------


async def test_kill_pane_targets_pane_id_directly(monkeypatch):
    captured: list[list[str]] = []

    async def fake_run(*args: str, check: bool = True) -> str:
        captured.append(list(args))
        return ""

    monkeypatch.setattr(client, "run", fake_run)
    await client.kill_pane("%42")
    argv = captured[0]
    assert argv[0] == "kill-pane"
    assert "-t" in argv
    assert argv[argv.index("-t") + 1] == "%42"


async def test_send_keys_uses_l_and_double_dash(monkeypatch):
    captured: list[list[str]] = []

    async def fake_run(*args: str, check: bool = True) -> str:
        captured.append(list(args))
        return ""

    monkeypatch.setattr(client, "run", fake_run)
    await client.send_keys("%7", "echo hi", enter=True)
    assert len(captured) == 2
    first, second = captured
    assert first[0] == "send-keys"
    assert "-t" in first and first[first.index("-t") + 1] == "%7"
    assert "-l" in first
    i = first.index("--")
    assert first[i + 1:] == ["echo hi"]
    assert second == ["send-keys", "-t", "%7", "Enter"]


async def test_send_keys_no_enter_is_single_call(monkeypatch):
    captured: list[list[str]] = []

    async def fake_run(*args: str, check: bool = True) -> str:
        captured.append(list(args))
        return ""

    monkeypatch.setattr(client, "run", fake_run)
    await client.send_keys("%7", "text", enter=False)
    assert len(captured) == 1
