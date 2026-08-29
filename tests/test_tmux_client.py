"""Assert the exact argv passed to tmux.

argv is the only thing that can be checked without a tmux server, and it is the
only thing that ever broke silently. These tests patch `theater.tmux.client.run`
and `theater.tmux.client.run_sync` at the lowest level, so any argv drift
downstream of the public functions is caught.
"""

from __future__ import annotations

import pytest

from theater.tmux import client

#: This module is the one place that wants the real client: it patches `run`
#: and `run_sync` underneath the public functions, so the autouse `fake_tmux`
#: replacing those functions would leave nothing to assert against.
pytestmark = pytest.mark.unpatched_tmux_client


@pytest.fixture(autouse=True)
def _reset_version_cache():
    # Pin None (tmux-absent, the conservative no-flag path) rather than reset
    # to unprobed. If we left it unprobed, every test that does not explicitly
    # set a version would invoke the real `tmux -V` and take whatever version
    # the host has — the suite would behave differently on 3.4 vs 3.7. Tests
    # that need a specific version override this pin as they already do.
    client._VERSION_CACHE[0] = None
    yield
    client.reset_version_cache()


async def test_set_buffer_passes_literal_text_after_double_dash(monkeypatch):
    captured = _capture(monkeypatch)

    await client.set_buffer("-literal\ntext")

    assert captured == [["set-buffer", "--", "-literal\ntext"]]


# ---- new_window --------------------------------------------------------


async def _new_window_argv(monkeypatch, **kw):
    captured: list[list[str]] = []

    async def fake_run(*args: str, check: bool = True) -> str:
        captured.append(list(args))
        return "%99"

    monkeypatch.setattr(client, "run", fake_run)
    defaults = {
        "session": "0",
        "name": "vibe-abc",
        "cwd": "/tmp",
        "command": ["vibe", "say hello"],
    }
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


async def test_new_window_background_inserts_d_before_capital_p(monkeypatch):
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
    after = argv[argv.index("--") + 1 :]
    assert after == ["vibe", "say hello"]


async def test_new_window_returns_pane_id_and_rejects_non_percent(monkeypatch):
    """A non-%-prefixed response is garbage, not a pane id."""

    async def returns_garbage(*a, **kw) -> str:
        return "garbage"

    monkeypatch.setattr(client, "run", returns_garbage)
    with pytest.raises(client.TmuxError):
        await client.new_window(session="0:", name="x", cwd="/tmp", command=["vibe"])


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


async def test_list_panes_session_already_colon_terminated_not_doubled(monkeypatch):
    argv = await _list_panes_argv(monkeypatch, session="dev:")
    assert argv[argv.index("-t") + 1] == "dev:"


async def test_list_panes_uses_pane_format(monkeypatch):
    argv = await _list_panes_argv(monkeypatch, session="dev")
    i = argv.index("-F")
    assert argv[i + 1] == client._PANE_FORMAT


async def test_inventory_observation_keeps_server_identity_with_panes(monkeypatch):
    captured: list[list[str]] = []

    async def fake_run(*args: str, check: bool = True) -> str:
        captured.append(list(args))
        assert check is True
        return "/tmp/tmux-100/default\t123\t456\t%1\n/tmp/tmux-100/default\t123\t456\t%2\n"

    monkeypatch.setattr(client, "run", fake_run)
    inventory = await client.observe_inventory()
    assert inventory is not None
    assert (
        inventory.server_identity
        == client.TmuxServerIdentity("/tmp/tmux-100/default", "123", "456").value
    )
    assert inventory.pane_ids == frozenset({"%1", "%2"})
    assert captured == [
        ["list-panes", "-a", "-F", "#{socket_path}\t#{pid}\t#{start_time}\t#{pane_id}"]
    ]


async def test_inventory_observation_treats_empty_output_as_inconclusive(monkeypatch):
    async def fake_run(*args: str, check: bool = True) -> str:
        return ""

    monkeypatch.setattr(client, "run", fake_run)
    assert await client.observe_inventory() is None


async def test_inventory_observation_rejects_mixed_server_identities(monkeypatch):
    async def fake_run(*args: str, check: bool = True) -> str:
        return "/tmp/tmux-100/default\t123\t456\t%1\n/tmp/tmux-100/default\t124\t456\t%2\n"

    monkeypatch.setattr(client, "run", fake_run)
    with pytest.raises(client.TmuxError, match="invalid server inventory"):
        await client.observe_inventory()


async def test_inventory_observation_rejects_missing_identity_component(monkeypatch):
    async def fake_run(*args: str, check: bool = True) -> str:
        return "/tmp/tmux-100/default\t123\t\t%1\n"

    monkeypatch.setattr(client, "run", fake_run)
    with pytest.raises(client.TmuxError, match="invalid server inventory"):
        await client.observe_inventory()


# ---- kill_pane / deliver_text: targets are pane ids, not sessions ------


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


async def test_conditional_kill_checks_compound_server_identity(monkeypatch):
    captured: list[list[str]] = []

    async def fake_run(*args: str, check: bool = True) -> str:
        captured.append(list(args))
        return "theater-killed"

    identity = client.TmuxServerIdentity("/tmp/tmux-100/default", "123", "456").value
    monkeypatch.setattr(client, "run", fake_run)
    assert await client.kill_pane_if_server_identity("%42", identity) is True
    assert captured == [
        [
            "if-shell",
            "-F",
            "#{&&:#{==:#{socket_path},/tmp/tmux-100/default},#{&&:#{==:#{pid},123},#{==:#{start_time},456}}}",
            "display-message -p theater-killed; kill-pane -t %42",
            "display-message -p theater-identity-mismatch",
        ]
    ]


async def _deliver_argv(monkeypatch, text: str, **kw) -> list[list[str]]:
    captured: list[list[str]] = []

    async def fake_run(*args: str, check: bool = True) -> str:
        captured.append(list(args))
        return ""

    monkeypatch.setattr(client, "run", fake_run)
    await client.deliver_text("%7", text, **kw)
    return captured


async def test_deliver_text_pastes_rather_than_typing(monkeypatch):
    """A prompt must not arrive as keystrokes.

    `send-keys -l` fired the receiver's keybindings -- OpenCode read the `!`
    in "Hey!" as its shell-mode trigger and ran the rest of the sentence
    through zsh. A paste is inert.
    """
    captured = await _deliver_argv(monkeypatch, "Hey! I'm here")

    assert not any(argv[0] == "send-keys" and "-l" in argv for argv in captured), (
        "the prompt must never be typed as keys"
    )

    set_buffer = next(a for a in captured if a[0] == "set-buffer")
    i = set_buffer.index("--")
    assert set_buffer[i + 1 :] == ["Hey! I'm here"], "text passes through unaltered"

    paste = next(a for a in captured if a[0] == "paste-buffer")
    assert paste[paste.index("-t") + 1] == "%7"
    # -p is what makes tmux add bracketed-paste markers, and only for an
    # application that asked for them. Without it the receiver sees keystrokes
    # again and the bug returns.
    assert "-p" in paste


async def test_deliver_text_buffer_is_per_pane(monkeypatch):
    """Two sends in flight must not paste each other's text."""
    captured = await _deliver_argv(monkeypatch, "hello")
    names = {a[a.index("-b") + 1] for a in captured if "-b" in a}
    assert names == {"theater-7"}


async def test_deliver_text_cleans_up_after_a_failed_paste(monkeypatch):
    """A dead pane must not leave its buffer on the stack."""
    captured: list[list[str]] = []

    async def fake_run(*args: str, check: bool = True) -> str:
        captured.append(list(args))
        if args[0] == "paste-buffer":
            raise client.TmuxError("no such pane")
        return ""

    monkeypatch.setattr(client, "run", fake_run)
    with pytest.raises(client.TmuxError):
        await client.deliver_text("%7", "hello")
    assert captured[-1][0] == "delete-buffer"


async def test_deliver_text_sends_enter_as_a_key(monkeypatch):
    """Enter is a key, not text: inside a paste it would be a literal newline."""
    captured = await _deliver_argv(monkeypatch, "hello", enter=True)
    assert captured[-1] == ["send-keys", "-t", "%7", "Enter"]


async def test_deliver_text_without_enter_does_not_submit(monkeypatch):
    captured = await _deliver_argv(monkeypatch, "hello", enter=False)
    assert not any(a[0] == "send-keys" for a in captured)


async def test_deliver_text_adds_s_on_tmux_37_plus(monkeypatch):
    """tmux 3.7+ escapes pasted content via vis(3) by default; -S restores raw."""
    client._VERSION_CACHE[0] = "3.7"
    captured = await _deliver_argv(monkeypatch, "hello")
    paste = next(a for a in captured if a[0] == "paste-buffer")
    assert "-S" in paste


async def test_deliver_text_adds_s_on_tmux_37a(monkeypatch):
    client._VERSION_CACHE[0] = "3.7a"
    captured = await _deliver_argv(monkeypatch, "hello")
    paste = next(a for a in captured if a[0] == "paste-buffer")
    assert "-S" in paste


async def test_deliver_text_adds_s_on_tmux_38(monkeypatch):
    client._VERSION_CACHE[0] = "3.8"
    captured = await _deliver_argv(monkeypatch, "hello")
    paste = next(a for a in captured if a[0] == "paste-buffer")
    assert "-S" in paste


async def test_deliver_text_omits_s_below_tmux_37(monkeypatch):
    """Below 3.7, the paste-buffer argv must be unchanged."""
    client._VERSION_CACHE[0] = "3.4"
    captured = await _deliver_argv(monkeypatch, "hello")
    paste = next(a for a in captured if a[0] == "paste-buffer")
    assert "-S" not in paste
    assert paste == ["paste-buffer", "-b", "theater-7", "-t", "%7", "-p", "-d"]


async def test_deliver_text_omits_s_when_tmux_absent(monkeypatch):
    client._VERSION_CACHE[0] = None
    captured = await _deliver_argv(monkeypatch, "hello")
    paste = next(a for a in captured if a[0] == "paste-buffer")
    assert "-S" not in paste


# ---- display_message ---------------------------------------------------


async def test_display_message_with_target(monkeypatch):
    captured: list[list[str]] = []

    async def fake_run(*args: str, check: bool = True) -> str:
        captured.append(list(args))
        return "@3"

    monkeypatch.setattr(client, "run", fake_run)
    result = await client.display_message("#{window_id}", target="%5")
    assert result == "@3"
    argv = captured[0]
    assert argv[0] == "display-message"
    assert "-p" in argv
    assert "-t" in argv
    assert argv[argv.index("-t") + 1] == "%5"
    assert "#{window_id}" in argv


async def test_display_message_without_target(monkeypatch):
    captured: list[list[str]] = []

    async def fake_run(*args: str, check: bool = True) -> str:
        captured.append(list(args))
        return "%99"

    monkeypatch.setattr(client, "run", fake_run)
    result = await client.display_message("#{pane_id}")
    assert result == "%99"
    argv = captured[0]
    assert argv[0] == "display-message"
    assert "-p" in argv
    assert "-t" not in argv
    assert "#{pane_id}" in argv


# ---- options -----------------------------------------------------------


def _capture(monkeypatch, output: str = ""):
    captured: list[list[str]] = []

    async def fake_run(*args: str, check: bool = True) -> str:
        captured.append(list(args))
        return output

    monkeypatch.setattr(client, "run", fake_run)
    return captured


async def test_show_option_is_session_scoped_not_global(monkeypatch):
    """`-g` would report the server-wide value, which is not what gets restored."""
    captured = _capture(monkeypatch, "mouse on")
    await client.show_option("mouse", target="$2")
    argv = captured[0]
    assert argv[0] == "show-options"
    assert "-g" not in argv
    assert argv[argv.index("-t") + 1] == "$2"
    assert argv[-1] == "mouse"


async def test_show_option_returns_the_value_without_its_name(monkeypatch):
    _capture(monkeypatch, "mouse on\n")
    assert await client.show_option("mouse", target="$2") == "on"


async def test_show_option_returns_none_when_the_session_has_no_override(monkeypatch):
    """An unset option prints nothing, which is not the same as `off`."""
    _capture(monkeypatch, "")
    assert await client.show_option("mouse", target="$2") is None


async def test_show_option_returns_none_for_output_it_cannot_parse(monkeypatch):
    _capture(monkeypatch, "mouse")
    assert await client.show_option("mouse", target="$2") is None


async def test_set_option_targets_the_session(monkeypatch):
    captured = _capture(monkeypatch)
    await client.set_option("mouse", "on", target="$2")
    argv = captured[0]
    assert argv[0] == "set-option"
    assert "-g" not in argv
    assert argv[argv.index("-t") + 1] == "$2"
    assert argv[-2:] == ["mouse", "on"]


async def test_unset_option_uses_u_so_the_global_value_applies_again(monkeypatch):
    captured = _capture(monkeypatch)
    await client.unset_option("mouse", target="$2")
    argv = captured[0]
    assert argv[0] == "set-option"
    assert "-u" in argv
    assert "-g" not in argv
    assert argv[-1] == "mouse"


async def test_show_window_option_is_window_scoped(monkeypatch):
    captured = _capture(monkeypatch, "@theater_regie 1\n")
    assert await client.show_window_option("@theater_regie", target="@4") == "1"
    argv = captured[0]
    assert argv[0] == "show-options"
    assert "-w" in argv
    assert argv[argv.index("-t") + 1] == "@4"


async def test_set_window_option_is_window_scoped(monkeypatch):
    captured = _capture(monkeypatch)
    await client.set_window_option("@theater_regie", "1", target="@4")
    argv = captured[0]
    assert argv[0] == "set-option"
    assert "-w" in argv
    assert argv[argv.index("-t") + 1] == "@4"
    assert argv[-2:] == ["@theater_regie", "1"]


# ---- key bindings --------------------------------------------------------


async def test_key_bound_reads_key_string_not_key(monkeypatch):
    """`#{key}` silently expands to empty; `#{key_string}` is the real field."""
    captured = _capture(monkeypatch, "Left\nh\nc\n")
    assert await client.key_bound("prefix", "h") is True
    argv = captured[0]
    assert argv[:3] == ["list-keys", "-T", "prefix"]
    assert argv[argv.index("-F") + 1] == "#{key_string}"


async def test_key_bound_false_when_key_absent(monkeypatch):
    _capture(monkeypatch, "Left\nc\n")
    assert await client.key_bound("prefix", "h") is False


async def test_bind_key_if_free_skips_an_existing_binding(monkeypatch):
    captured = _capture(monkeypatch, "h\n")
    installed = await client.bind_key_if_free("prefix", "h", ["select-pane", "-L"], note="x")
    assert installed is False
    assert all(argv[0] != "bind-key" for argv in captured)


async def test_bind_key_if_free_installs_and_tags_with_the_note(monkeypatch):
    captured = _capture(monkeypatch, "")
    installed = await client.bind_key_if_free("prefix", "h", ["select-pane", "-L"], note="x")
    assert installed is True
    argv = captured[-1]
    assert argv[0] == "bind-key"
    assert argv[argv.index("-N") + 1] == "x"
    assert argv[-2:] == ["select-pane", "-L"]
    assert "h" in argv


async def test_unbind_key_if_owned_removes_a_matching_note(monkeypatch):
    captured = _capture(monkeypatch, f"h{client._FORMAT_SEP}x\n")
    await client.unbind_key_if_owned("prefix", "h", note="x")
    assert ["unbind-key", "-T", "prefix", "h"] in captured


async def test_unbind_key_if_owned_leaves_someone_elses_binding_alone(monkeypatch):
    captured = _capture(monkeypatch, f"h{client._FORMAT_SEP}someone-else\n")
    await client.unbind_key_if_owned("prefix", "h", note="x")
    assert all(argv[0] != "unbind-key" for argv in captured)


async def test_bind_key_if_free_does_not_bind_when_inspection_fails(monkeypatch):
    """A failed check must not read as "nothing bound" and overwrite blindly."""

    async def failing_run(*args: str, check: bool = True) -> str:
        raise client.TmuxError("list-keys failed")

    monkeypatch.setattr(client, "run", failing_run)
    with pytest.raises(client.TmuxError):
        await client.bind_key_if_free("prefix", "h", ["select-pane", "-L"], note="x")
