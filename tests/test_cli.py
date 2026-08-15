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
    "name": "Arlequin",
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
    out = cli._format_ls(
        [ROW],
        tree=False,
        unmanaged=[
            {"pane": "%9", "command": "vibe", "cwd": "/tmp/other"},
        ],
    )
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


def test_the_name_column_appears_in_the_header_and_row():
    out = cli._format_ls([ROW], tree=False)
    lines = out.splitlines()
    assert "NAME" in lines[0]
    assert "Arlequin" in lines[1]


def test_an_unmanaged_row_shows_a_dash_in_the_name_cell():
    out = cli._format_ls(
        [ROW],
        tree=False,
        unmanaged=[{"pane": "%9", "command": "vibe", "cwd": "/tmp/other"}],
    )
    lines = out.splitlines()
    name_col = lines[0].index("NAME")
    unmanaged_line = next(ln for ln in lines if "%9" in ln)
    assert unmanaged_line[name_col] == "-"


def test_watch_and_json_cannot_be_combined():
    """One prints once and exits, the other never exits. Fail loudly."""
    with pytest.raises(SystemExit):
        parse("ls", "--watch", "--json")


# ---- version ------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--version", "-v"])
def test_version_flag_prints_the_version_and_exits_clean(flag, capsys):
    """Both spellings print `theater <version>` and exit 0, above the
    required subcommand — argparse resolves --version during parsing."""
    from theater import __version__

    with pytest.raises(SystemExit) as exc:
        parse(flag)
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"theater {__version__}"


def test_a_bare_invocation_still_requires_a_subcommand():
    """--version is optional; without it a subcommand is still mandatory."""
    with pytest.raises(SystemExit) as exc:
        parse()
    assert exc.value.code != 0


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
    line = cli._bus_line(event(kind="agent.assistant", payload={"text": "one\ntwo   three"}), 200)
    assert "one two three" in line
    assert "\n" not in line


def test_a_turn_boundary_is_visible():
    assert "(turn end)" in cli._bus_line(event(payload={"text": "done", "turn_end": True}), 200)


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


async def test_follow_from_an_empty_bus_starts_at_zero(fake_follow):
    client = fake_follow([[], [event(id=1)]])
    args = parse("bus", "-f", "--interval", "0")
    await _run_follow(args)
    assert client.cursors == [0, 0, 1]


# ---- harnesses ----------------------------------------------------------


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

    async def connect(self):
        """Reaching the daemon succeeds. Whether it replies is another matter.

        `_shutdown_running_daemon` distinguishes the two: connecting answers
        "is there a daemon", and the reply is allowed to go missing.
        """

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


# ---- command bodies ------------------------------------------------------
#
# Everything above tests rendering against dicts. These test the commands
# themselves: what they ask the daemon for, what they print, and what they
# return. `call_sync` is the seam — it is the only thing between a command and
# a socket, and the socket itself is covered in test_daemon.py.


@pytest.fixture
def answers(monkeypatch):
    """Replace call_sync with a canned-answer recorder."""
    state = {"replies": {}, "calls": []}

    def call_sync(method, **params):
        state["calls"].append((method, params))
        reply = state["replies"].get(method, [])
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(cli, "call_sync", call_sync)
    return state


def test_ls_prints_participants_and_unmanaged_panes(answers, capsys):
    answers["replies"] = {"participants.list": [ROW], "participants.unmanaged": []}
    assert cli.cmd_ls(parse("ls")) == 0
    assert "p-abc123" in capsys.readouterr().out


def test_ls_json_carries_both_lists(answers, capsys):
    answers["replies"] = {
        "participants.list": [ROW],
        "participants.unmanaged": [{"pane": "%9"}],
    }
    assert cli.cmd_ls(parse("ls", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["participants"] == [ROW]
    assert payload["unmanaged"] == [{"pane": "%9"}]


def test_ls_tree_does_not_ask_for_unmanaged_panes(answers):
    """The tree is lineage; a pane with no participant has no place in it."""
    answers["replies"] = {"participants.tree": [ROW]}
    assert cli.cmd_ls(parse("ls", "--tree")) == 0
    assert [m for m, _ in answers["calls"]] == ["participants.tree"]


def test_ls_all_asks_for_the_dead_too(answers):
    answers["replies"] = {"participants.list": [], "participants.unmanaged": []}
    cli.cmd_ls(parse("ls", "--all"))
    assert answers["calls"][0] == ("participants.list", {"include_dead": True})


def test_kill_names_what_it_killed(answers, capsys):
    assert cli.cmd_kill(parse("kill", "p-abc123")) == 0
    assert answers["calls"] == [("participant.kill", {"id": "p-abc123"})]
    assert "killed p-abc123" in capsys.readouterr().out


def test_name_command_calls_rename_and_prints_the_result(answers, capsys):
    answers["replies"] = {"participant.rename": {**ROW, "name": "Pierrot"}}
    assert cli.cmd_name(parse("name", "Arlequin", "Pierrot")) == 0
    assert answers["calls"] == [
        ("participant.rename", {"id": "Arlequin", "name": "Pierrot"})
    ]
    assert "Pierrot" in capsys.readouterr().out


def test_bus_with_no_events_says_so(answers, capsys):
    answers["replies"] = {"bus.tail": []}
    assert cli.cmd_bus(parse("bus")) == 0
    assert "no events" in capsys.readouterr().out


def test_bus_filters_by_kind_prefix(answers, capsys):
    answers["replies"] = {
        "bus.tail": [
            {
                "id": 1,
                "ts": 0,
                "kind": "agent.assistant",
                "from_id": "a",
                "to_id": None,
                "payload": {"text": "hi"},
            },
            {"id": 2, "ts": 0, "kind": "job.created", "from_id": "a", "to_id": "b", "payload": {}},
        ]
    }
    assert cli.cmd_bus(parse("bus", "--kind", "agent")) == 0
    out = capsys.readouterr().out
    assert "agent.assistant" in out
    assert "job.created" not in out


def test_adopt_without_a_pane_explains_itself(monkeypatch, capsys):
    monkeypatch.setattr(cli.tmux, "current_pane", lambda: None)
    assert cli.cmd_adopt(parse("adopt")) == 1
    assert "$TMUX_PANE" in capsys.readouterr().err


def test_adopt_reports_the_record_it_got_back(answers, monkeypatch, capsys):
    monkeypatch.setattr(cli.tmux, "current_pane", lambda: "%7")
    answers["replies"] = {
        "adopt": {"id": "p-xyz", "tier": "adopted", "harness": "vibe", "tmux_pane": "%7"}
    }
    assert cli.cmd_adopt(parse("adopt")) == 0
    out = capsys.readouterr().out
    assert "p-xyz" in out
    assert "%7" in out
    assert answers["calls"][0][1]["pane"] == "%7"


def test_adopt_json_prints_the_record_verbatim(answers, monkeypatch, capsys):
    monkeypatch.setattr(cli.tmux, "current_pane", lambda: "%7")
    record = {"id": "p-xyz", "tier": "adopted", "harness": "vibe", "tmux_pane": "%7"}
    answers["replies"] = {"adopt": record}
    assert cli.cmd_adopt(parse("adopt", "--json")) == 0
    assert json.loads(capsys.readouterr().out) == record


def test_stats_json_is_the_daemon_answer(answers, capsys):
    data = {"since": 0, "harnesses": [], "refusals": []}
    answers["replies"] = {"stats": data}
    assert cli.cmd_stats(parse("stats", "--json")) == 0
    assert json.loads(capsys.readouterr().out) == data


# ---- spawn ---------------------------------------------------------------


def test_spawn_passes_the_prompt_and_the_cwd(answers, monkeypatch, capsys):
    monkeypatch.setattr(cli.tmux, "current_session_sync", lambda: "main")
    answers["replies"] = {"spawn": {"id": "p-new", "harness": "vibe", "tmux_pane": "%4"}}
    assert cli.cmd_spawn(parse("spawn", "vibe", "say hello", "--approval", "manual")) == 0
    method, params = answers["calls"][0]
    assert method == "spawn"
    assert params["harness"] == "vibe"
    assert params["prompt"] == "say hello"
    assert params["tmux_session"] == "main"
    assert "p-new" in capsys.readouterr().out


def test_spawn_sends_the_model_it_was_given(answers, monkeypatch):
    monkeypatch.setattr(cli.tmux, "current_session_sync", lambda: "main")
    answers["replies"] = {"spawn": {"id": "p-new", "harness": "vibe", "tmux_pane": "%4"}}
    cli.cmd_spawn(parse("spawn", "vibe", "hi", "--approval", "manual", "--model", "big-one"))
    assert answers["calls"][0][1]["model"] == "big-one"


def test_spawn_sends_no_model_when_none_was_named(answers, monkeypatch):
    """None, not absent: the daemon reads it with .get either way, but a
    spawn that did not choose must be distinguishable from one that did."""
    monkeypatch.setattr(cli.tmux, "current_session_sync", lambda: "main")
    answers["replies"] = {"spawn": {"id": "p-new", "harness": "vibe", "tmux_pane": "%4"}}
    cli.cmd_spawn(parse("spawn", "vibe", "hi", "--approval", "manual"))
    assert answers["calls"][0][1]["model"] is None


def test_spawn_json_prints_the_record_verbatim(answers, monkeypatch, capsys):
    monkeypatch.setattr(cli.tmux, "current_session_sync", lambda: "main")
    answers["replies"] = {"spawn": {"id": "p-new", "harness": "vibe", "tmux_pane": "%4"}}
    assert cli.cmd_spawn(parse("spawn", "vibe", "hi", "--approval", "manual", "--json")) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "p-new"


def test_spawn_rejects_an_unknown_harness_by_name(monkeypatch):
    """Not argparse `choices`: declared harnesses arrive after the parser."""
    with pytest.raises(cli.BadUsage) as exc:
        cli._spawn_harness(parse("spawn", "nosuch", "hi", "--approval", "manual"))
    assert "unknown harness" in str(exc.value)
    assert "--prompt" in str(exc.value), "the likely mistake is naming a prompt"


# ---- models --------------------------------------------------------------


def write_config(text: str) -> None:
    paths.config_path().write_text(text, encoding="utf-8")


def discover(monkeypatch, harness: str, result, *extra):
    """Run `models --discover <harness>` with that adapter answering `result`.

    An exception as `result` is raised instead of returned, which is how the
    "cannot be asked" path is reached.

    Called directly rather than through `main`, because `main` rebuilds the
    registry first and `plugins.scan` re-imports the plugin files — so neither
    the instance nor the class present at patch time is the one the command
    would end up calling. The real adapters have to be kept out of this: `vibe`
    reads ~/.vibe/config.toml, so unpatched these tests would assert against
    whatever models the developer running the suite happens to have.
    """

    def answer():
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(cli.HARNESSES[harness], "discover_models", answer)
    return cli.cmd_models(parse("models", "--discover", harness, *extra))


def test_models_lists_every_harness_not_only_the_configured_ones(capsys):
    """The unlisted ones are the point: those are what `--model` refuses."""
    write_config('[models]\nvibe = ["big"]\n')
    assert cli.main(["models"]) == 0
    out = capsys.readouterr().out
    assert "vibe" in out
    assert "big" in out
    assert "claude" in out
    assert "--model refused" in out


def test_models_says_how_to_get_started(capsys):
    """An empty allowlist refuses every --model, so the on-ramp must be visible."""
    assert cli.main(["models"]) == 0
    assert "--discover" in capsys.readouterr().out


def test_models_json_is_the_configured_mapping(capsys):
    write_config('[models]\nvibe = ["big", "small"]\n')
    assert cli.main(["models", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"vibe": ["big", "small"]}


def test_discover_prints_a_block_naming_the_harness(monkeypatch, capsys):
    assert discover(monkeypatch, "vibe", ["big", "small"]) == 0
    out = capsys.readouterr().out
    assert "[models]" in out
    assert "vibe = [" in out
    assert '"big",' in out


def pasted(capsys) -> str:
    """What a human would copy: the block, without the explanatory comments."""
    return "\n".join(
        line for line in capsys.readouterr().out.splitlines() if not line.startswith("#")
    )


def test_a_quoted_model_name_survives_the_round_trip(monkeypatch, capsys):
    """Names are quoted by a real encoder, not by string concatenation."""
    assert discover(monkeypatch, "vibe", ['weird"name']) == 0
    write_config(pasted(capsys))
    assert cli.main(["models", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"vibe": ['weird"name']}


def test_discover_json_is_the_found_list(monkeypatch, capsys):
    assert discover(monkeypatch, "vibe", ["big"], "--json") == 0
    assert json.loads(capsys.readouterr().out) == {
        "harness": "vibe",
        "models": ["big"],
    }


def test_discover_on_a_cli_that_cannot_be_asked_fails_helpfully(monkeypatch, capsys):
    problem = NotImplementedError("claude has no list")
    assert discover(monkeypatch, "claude", problem) == 1
    err = capsys.readouterr().err
    assert "claude has no list" in err
    # The manual route still works, and is the whole of the fix.
    assert "by hand" in err


def test_discover_that_finds_nothing_is_not_the_same_failure(monkeypatch, capsys):
    """Asked and answered: none. Usually an unauthenticated provider."""
    assert discover(monkeypatch, "vibe", []) == 1
    err = capsys.readouterr().err
    assert "no models" in err
    assert "by hand" not in err


def test_discover_never_prints_a_block_it_could_not_fill(monkeypatch, capsys):
    """An empty [models] block would read as an answer."""
    discover(monkeypatch, "vibe", [])
    assert capsys.readouterr().out == ""


def test_discover_rejects_an_unknown_harness(capsys):
    assert cli.main(["models", "--discover", "nope"]) == 1
    assert "unknown harness" in capsys.readouterr().err


def test_discover_accepts_an_alias_for_the_harness_name(monkeypatch, capsys):
    """Same spelling rules as `spawn`; the name came from the same head."""
    monkeypatch.setattr(cli.HARNESSES["claude"], "discover_models", lambda: ["big"])
    assert cli.cmd_models(parse("models", "--discover", "claude-code")) == 0
    assert "claude = [" in capsys.readouterr().out


# ---- long-running commands ----------------------------------------------


class _Stop(Exception):
    """Ends a follow loop the way ctrl-c would, without the signal."""


class _FollowClient:
    """A daemon that answers a fixed script, then ends the loop.

    `_watch_ls` and `_follow_bus` never return on their own, so the script
    running out is how the test gets control back.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call(self, method, **params):
        self.calls.append((method, params))
        if not self.script:
            raise _Stop
        return self.script.pop(0)


def test_ls_watch_redraws_a_whole_frame_each_time(monkeypatch, capsys):
    """A partial redraw would leave the previous frame's rows on screen."""
    client = _FollowClient([[ROW], [], [ROW], []])
    monkeypatch.setattr(cli, "DaemonClient", lambda: client)
    with pytest.raises(_Stop):
        cli.cmd_ls(parse("ls", "--watch", "--interval", "0"))
    out = capsys.readouterr().out
    assert out.count(cli._CLEAR) == 2, "one screen clear per frame"
    assert out.count("p-abc123") == 2


def test_ls_watch_in_tree_mode_never_asks_for_unmanaged_panes(monkeypatch):
    """Unmanaged panes have no place in a lineage tree — no parent, no children."""
    client = _FollowClient([[ROW]])
    monkeypatch.setattr(cli, "DaemonClient", lambda: client)
    with pytest.raises(_Stop):
        cli.cmd_ls(parse("ls", "--watch", "--tree", "--interval", "0"))
    assert [m for m, _ in client.calls] == ["participants.tree", "participants.tree"]


def test_follow_says_when_the_feed_fell_behind(monkeypatch, capsys):
    """A burst larger than one batch drops the middle; silence would look complete."""
    client = _FollowClient(
        [
            [{"id": 1, "ts": 0, "kind": "agent.user", "actor_id": "p-a", "payload": {}}],
            [{"id": 5, "ts": 0, "kind": "agent.user", "actor_id": "p-a", "payload": {}}],
        ]
    )
    monkeypatch.setattr(cli, "DaemonClient", lambda: client)
    with pytest.raises(_Stop):
        cli.cmd_bus(parse("bus", "-f", "--interval", "0"))
    assert "3 events dropped" in capsys.readouterr().out


def test_follow_holds_its_cursor_across_an_empty_poll(monkeypatch, capsys):
    """Advancing on nothing would skip whatever the daemon writes next."""
    client = _FollowClient(
        [
            [{"id": 7, "ts": 0, "kind": "agent.user", "actor_id": "p-a", "payload": {}}],
            [],
        ]
    )
    monkeypatch.setattr(cli, "DaemonClient", lambda: client)
    with pytest.raises(_Stop):
        cli.cmd_bus(parse("bus", "-f", "--interval", "0"))
    assert [p.get("after_id") for _, p in client.calls] == [None, 7, 7]


def test_regie_refuses_to_run_outside_tmux(monkeypatch, capsys):
    monkeypatch.setattr(cli.tmux, "inside_tmux", lambda: False)
    assert cli.cmd_regie(parse("regie")) == 1
    assert "inside tmux" in capsys.readouterr().err


def test_regie_hands_the_config_to_the_app(monkeypatch):
    import theater.regie.app as app_mod

    monkeypatch.setattr(cli.tmux, "inside_tmux", lambda: True)
    seen: list = []
    monkeypatch.setattr(app_mod, "run_regie", seen.append)
    assert cli.cmd_regie(parse("regie")) == 0
    assert seen and seen[0].regie is not None


def test_daemon_reports_a_refusal_to_start(monkeypatch, capsys):
    import theater.daemon.server as server_mod

    async def refuse():
        raise RuntimeError("another daemon holds the lock")

    monkeypatch.setattr(server_mod, "run", refuse)
    assert cli.cmd_daemon(parse("daemon")) == 1
    assert "another daemon" in capsys.readouterr().err


def test_daemon_exits_quietly_on_ctrl_c(monkeypatch):
    import theater.daemon.server as server_mod

    async def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(server_mod, "run", interrupted)
    assert cli.cmd_daemon(parse("daemon")) == 0


def test_mcp_serves_the_id_it_was_given(monkeypatch):
    import theater.mcp.server as server_mod

    seen: list = []
    monkeypatch.setattr(server_mod, "main", lambda pid, harness: seen.append((pid, harness)))
    assert cli.cmd_mcp(parse("mcp", "--id", "p-abc", "--harness", "vibe")) == 0
    assert seen == [("p-abc", "vibe")]


# ---- main ----------------------------------------------------------------


def test_main_dispatches_to_the_named_command(monkeypatch, capsys):
    monkeypatch.setattr(cli, "call_sync", lambda m, **p: [])
    assert cli.main(["ls"]) == 0


def test_main_turns_a_remote_error_into_one_line(monkeypatch, capsys):
    def fail(method, **params):
        raise RemoteError("busy", "target is mid-turn")

    monkeypatch.setattr(cli, "call_sync", fail)
    assert cli.main(["kill", "p-abc"]) == 1
    assert "busy: target is mid-turn" in capsys.readouterr().err


def test_main_turns_an_unreachable_daemon_into_one_line(monkeypatch, capsys):
    def fail(method, **params):
        raise ConnectionError("no daemon at /tmp/theater.sock")

    monkeypatch.setattr(cli, "call_sync", fail)
    assert cli.main(["kill", "p-abc"]) == 1
    assert "no daemon" in capsys.readouterr().err


def test_ctrl_c_out_of_a_follow_is_not_a_crash(monkeypatch):
    """`bus -f` and `ls --watch` are ended this way; 130 is what a shell expects."""

    def interrupt(method, **params):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "call_sync", interrupt)
    assert cli.main(["kill", "p-abc"]) == 130
