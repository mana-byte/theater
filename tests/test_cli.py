"""The human-facing commands.

Rendering is tested against plain dicts rather than a live daemon: the socket
round trip is already covered in test_daemon.py, and what can actually be wrong
here is the formatting and the follow cursor.
"""

from __future__ import annotations

import json

import pytest

from theater import cli, paths
from theater.formatting import event_summary, flatten_tree
from theater.protocol import RemoteError

ROW = {
    "id": "p-abc123",
    "tier": "spawned",
    "addressable": True,
    "harness": "vibe",
    "status": "working",
    "tmux_pane": "%3",
    "cwd": "/tmp/project",
}


def parse(*argv):
    return cli._parser().parse_args(list(argv))


# ---- ls -----------------------------------------------------------------


def test_a_participant_renders_with_its_tier_mark():
    line = cli._row_line(ROW)
    assert line.startswith("p-abc123")
    assert "S " in line  # spawned, addressable
    assert "%3" in line
    assert line.endswith("/tmp/project")


def test_unmanaged_panes_append_to_ls_output():
    """Unmanaged panes show below participants, not instead of them."""
    out = cli._format_ls([ROW], tree=False, unmanaged=[
        {"pane": "%9", "command": "vibe", "cwd": "/tmp/other"},
    ])
    assert "unmanaged" in out
    assert "%9" in out
    assert "/tmp/other" in out
    # The participant row is still there
    assert "p-abc123" in out


def test_unmanaged_none_does_not_add_section():
    out = cli._format_ls([ROW], tree=False, unmanaged=None)
    assert "unmanaged" not in out


def test_an_unaddressable_participant_is_marked():
    assert "E*" in cli._row_line({**ROW, "tier": "external", "addressable": False})


def test_a_long_harness_name_does_not_shear_the_columns():
    """`hello` takes whatever name a participant claims. Columns still line up."""
    normal = cli._row_line(ROW)
    weird = cli._row_line({**ROW, "harness": "some-very-long-harness"})
    assert weird.index("working") == normal.index("working")


def test_the_home_directory_is_abbreviated(monkeypatch):
    monkeypatch.setenv("HOME", "/Users/someone")
    assert cli._row_line({**ROW, "cwd": "/Users/someone/code"}).endswith("~/code")


def test_children_are_indented_under_their_parent():
    tree = [{**ROW, "children": [{**ROW, "id": "p-child0", "children": []}]}]
    lines = flatten_tree(tree, cli._row_line)
    assert len(lines) == 2
    assert lines[1].rstrip().endswith("  /tmp/project")
    assert lines[1].index("/tmp/project") > lines[0].index("/tmp/project")


def test_an_empty_registry_says_so_rather_than_printing_a_bare_header():
    assert cli._format_ls([], tree=False) == "no participants"


def test_the_legend_explains_the_marks():
    out = cli._format_ls([ROW], tree=False)
    assert "S spawned" in out and "not addressable" in out


def test_watch_and_json_cannot_be_combined():
    """One prints once and exits, the other never exits. Fail loudly."""
    with pytest.raises(SystemExit):
        parse("ls", "--watch", "--json")


# ---- bus ----------------------------------------------------------------


def event(**kw):
    row = {"id": 1, "ts": 0, "from_id": None, "to_id": None, "kind": "x", "payload": None}
    row.update(kw)
    return row


def test_a_tool_call_shows_the_tool_name():
    line = cli._bus_line(
        event(kind="agent.tool_call", from_id="p-abc123", payload={"tool": "bash"}),
        200,
    )
    assert "agent.tool_call" in line
    assert "p-abc123" in line
    assert "[bash]" in line


def test_assistant_text_is_flattened_to_one_line():
    line = cli._bus_line(
        event(kind="agent.assistant", payload={"text": "one\ntwo   three"}), 200
    )
    assert "one two three" in line
    assert "\n" not in line


def test_a_turn_boundary_is_visible():
    assert "(turn end)" in cli._bus_line(
        event(payload={"text": "done", "turn_end": True}), 200
    )


def test_an_unrecognised_payload_is_shown_rather_than_dropped():
    line = cli._bus_line(event(kind="participant.joined", payload={"pane": "%9"}), 200)
    assert "%9" in line


def test_bookkeeping_fields_alone_do_not_produce_noise():
    """ts/index/turn_end are chrome; with nothing else there is nothing to say."""
    assert event_summary({"ts": None, "index": 4, "turn_end": False}) == ""


def test_a_long_line_is_clipped_to_the_terminal():
    line = cli._bus_line(event(payload={"text": "x" * 500}), 80)
    assert len(line) < 80


def test_a_directed_event_shows_both_ends():
    line = cli._bus_line(event(from_id="p-aaa", to_id="p-bbb"), 200)
    assert "p-aaa -> p-bbb" in line


def test_the_kind_filter_is_a_prefix():
    rows = [event(kind="agent.user"), event(kind="participant.joined")]
    assert [r["kind"] for r in cli._matching(rows, "agent.")] == ["agent.user"]
    assert cli._matching(rows, None) == rows


def test_json_output_is_one_object_per_line(capsys):
    args = parse("bus", "--json")
    cli._emit_bus([event(id=1), event(id=2)], args)
    lines = capsys.readouterr().out.strip().split("\n")
    assert [json.loads(line)["id"] for line in lines] == [1, 2]


# ---- follow -------------------------------------------------------------


class FakeClient:
    """Replays canned bus.tail responses, recording the cursors asked for."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.cursors: list[int] = []

    async def call(self, method, **params):
        assert method == "bus.tail"
        self.cursors.append(params.get("after_id", 0))
        if not self.pages:
            raise KeyboardInterrupt
        return self.pages.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


@pytest.fixture
def fake_follow(monkeypatch):
    def install(pages):
        client = FakeClient(pages)
        monkeypatch.setattr(cli, "DaemonClient", lambda *a, **k: client)
        return client

    return install


async def _run_follow(args):
    with pytest.raises(KeyboardInterrupt):
        await cli._follow_bus(args)


async def test_follow_advances_the_cursor_past_what_it_printed(fake_follow, capsys):
    client = fake_follow([[event(id=7, kind="agent.user")], [event(id=9, kind="agent.user")]])
    args = parse("bus", "-f", "--interval", "0")
    await _run_follow(args)
    assert client.cursors == [0, 7, 9]
    assert capsys.readouterr().out.count("agent.user") == 2


async def test_follow_reports_a_gap_instead_of_hiding_it(fake_follow, capsys):
    """bus.tail keeps the newest rows, so a burst drops the middle. Say so."""
    fake_follow([[event(id=10)], [event(id=25)]])
    args = parse("bus", "-f", "--interval", "0")
    await _run_follow(args)
    assert "14 events dropped" in capsys.readouterr().out


async def test_follow_from_an_empty_bus_starts_at_zero(fake_follow):
    client = fake_follow([[], [event(id=1)]])
    args = parse("bus", "-f", "--interval", "0")
    await _run_follow(args)
    assert client.cursors == [0, 0, 1]


# ---- harnesses ----------------------------------------------------------


def test_harnesses_is_a_command():
    assert parse("harnesses").command == "harnesses"


def test_harnesses_lists_every_registered_adapter(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    assert cli.cmd_harnesses(parse("harnesses")) == 0
    out = capsys.readouterr().out
    for name in cli.HARNESSES:
        assert name in out
    assert "not on PATH" not in out


def test_harnesses_says_which_ones_are_missing(monkeypatch, capsys):
    """Naming an uninstalled harness is the whole point: spawn will refuse it."""
    monkeypatch.setattr(cli.shutil, "which", lambda binary: None)
    cli.cmd_harnesses(parse("harnesses"))
    out = capsys.readouterr().out
    assert "not on PATH" in out
    for name in cli.HARNESSES:
        assert name in out


def test_harnesses_json_reports_install_state(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda binary: None)
    cli.cmd_harnesses(parse("harnesses", "--json"))
    rows = json.loads(capsys.readouterr().out)
    assert {r["name"] for r in rows} == set(cli.HARNESSES)
    assert all(r["installed"] is False and r["path"] is None for r in rows)
    assert all(r["icon"] for r in rows)


def test_harnesses_never_starts_a_daemon(monkeypatch, capsys):
    """It answers 'what can I spawn', so it must work before anything is up.

    It does ask a *running* daemon — that one is authoritative, since it holds
    the config as of its own start. But autostart is off, so the question is
    never the thing that launches one.
    """
    def explode(*a, **k):
        raise AssertionError("cmd_harnesses started a daemon")

    monkeypatch.setattr(cli, "call_sync", explode)
    monkeypatch.setattr(cli.DaemonClient, "_start_daemon", explode)
    assert cli.cmd_harnesses(parse("harnesses")) == 0
    assert "no daemon running" in capsys.readouterr().out


def test_harnesses_prefers_the_running_daemons_answer(monkeypatch, capsys):
    """The daemon may know harnesses this process cannot import."""
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call(self, method, **params):
            assert method == "harnesses"
            return [
                {
                    "name": "codex",
                    "icon": "◇",
                    "binary": "codex",
                    "installed": True,
                    "path": "/usr/bin/codex",
                }
            ]

    monkeypatch.setattr(cli, "DaemonClient", FakeClient)
    assert cli.cmd_harnesses(parse("harnesses")) == 0
    out = capsys.readouterr().out
    assert "codex" in out
    assert "no daemon running" not in out
    assert "vibe" not in out


def test_the_harness_column_lines_up_across_header_rows_and_unmanaged():
    """The icon added a column; every row type has to shift by the same amount."""
    out = cli._format_ls(
        [ROW],
        tree=False,
        unmanaged=[{"pane": "%9", "command": "vibe", "cwd": "/tmp/other"}],
    )
    lines = out.splitlines()
    col = lines[0].index("HARNESS")
    assert lines[1].index("vibe") == col
    assert next(ln for ln in lines if "%9" in ln).index("vibe") == col


def test_a_participant_row_carries_its_harness_icon():
    assert cli.harness_icon("vibe") in cli._row_line(ROW)


def test_harnesses_falls_back_when_the_daemon_predates_the_method(monkeypatch, capsys):
    """Version skew: a daemon that old necessarily has the built-in registry."""
    class OldDaemon:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call(self, method, **params):
            raise RemoteError("unknown_method", f"no method {method!r}")

    monkeypatch.setattr(cli, "DaemonClient", OldDaemon)
    assert cli.cmd_harnesses(parse("harnesses")) == 0
    out = capsys.readouterr().out
    assert "predates this command" in out
    for name in cli.HARNESSES:
        assert name in out


def test_a_real_daemon_error_is_not_papered_over(monkeypatch):
    """Any other failure must surface, not become a plausible-looking list."""
    class Broken:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call(self, method, **params):
            raise RemoteError("internal", "the registry blew up")

    monkeypatch.setattr(cli, "DaemonClient", Broken)
    with pytest.raises(RemoteError):
        cli.cmd_harnesses(parse("harnesses"))


# ---- stop and restart ---------------------------------------------------


class StoppableDaemon:
    """A daemon that answers shutdown, then takes its socket away."""

    stopped = False

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call(self, method, **params):
        if method == "shutdown":
            type(self).stopped = True
            paths.socket_path().unlink(missing_ok=True)
            return {"stopping": True}
        return {"pong": True}


class NoDaemon:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        raise FileNotFoundError(paths.socket_path())

    async def __aexit__(self, *exc):
        return False


def test_restart_is_a_command():
    assert parse("restart").command == "restart"


def test_stop_does_not_start_a_daemon_just_to_stop_it(monkeypatch, capsys):
    monkeypatch.setattr(cli, "DaemonClient", NoDaemon)
    assert cli.cmd_stop(parse("stop")) == 0
    assert "no daemon running" in capsys.readouterr().out


def test_stop_shuts_down_a_running_daemon(monkeypatch, capsys):
    StoppableDaemon.stopped = False
    monkeypatch.setattr(cli, "DaemonClient", StoppableDaemon)
    assert cli.cmd_stop(parse("stop")) == 0
    assert StoppableDaemon.stopped
    assert "stopping" in capsys.readouterr().out


def test_restart_stops_the_old_daemon_before_starting_one(monkeypatch, capsys):
    StoppableDaemon.stopped = False
    order: list[str] = []
    monkeypatch.setattr(cli, "DaemonClient", StoppableDaemon)
    monkeypatch.setattr(cli, "call_sync", lambda method, **p: order.append(method))
    assert cli.cmd_restart(parse("restart")) == 0
    assert StoppableDaemon.stopped
    assert order == ["ping"], "the new daemon has to be proved up, not assumed"
    assert "restarted" in capsys.readouterr().out


def test_restart_with_no_daemon_just_starts_one(monkeypatch, capsys):
    started: list[str] = []
    monkeypatch.setattr(cli, "DaemonClient", NoDaemon)
    monkeypatch.setattr(cli, "call_sync", lambda method, **p: started.append(method))
    assert cli.cmd_restart(parse("restart")) == 0
    assert started == ["ping"]


def test_restart_refuses_to_start_a_second_daemon(monkeypatch, capsys):
    """A socket still held means the old daemon is alive; two would race."""
    paths.socket_path().touch()

    class Deaf(StoppableDaemon):
        async def call(self, method, **params):
            return {"stopping": True}  # says yes, never lets go

    monkeypatch.setattr(cli, "DaemonClient", Deaf)
    monkeypatch.setattr(cli, "STOP_TIMEOUT", 0.1)
    monkeypatch.setattr(cli, "call_sync", _explode_on_call)
    assert cli.cmd_restart(parse("restart")) == 1
    assert "still holding" in capsys.readouterr().err


def _explode_on_call(*a, **k):
    raise AssertionError("started a daemon while the old one was still up")
