"""Participant commands: ls rendering, kill, name, candidates, bind, adopt and stats."""

from __future__ import annotations

import json

import pytest

from tests.support.cli_fakes import FollowClient, StopFollow, parse
from theater import cli
from theater.cli.commands import participants as participants_mod
from theater.formatting import flatten_tree

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


def test_a_participant_renders_with_its_tier_mark():
    line = cli._row_line(ROW)
    assert line.startswith("p-abc123")
    assert "S " in line  # spawned, addressable
    assert "%3" not in line, "historical pane fields are not current route evidence"
    assert line.endswith("/tmp/project")


def test_a_human_at_the_pane_marks_the_row_and_absence_does_not():
    # No human_presence key at all: the row looks exactly like before.
    assert cli._row_line(ROW).endswith("/tmp/project")
    present = cli._row_line({**ROW, "human_presence": {"state": "present", "protected": True}})
    assert present.endswith("/tmp/project  ◉ human")
    unknown = cli._row_line({**ROW, "human_presence": {"state": "unknown", "protected": True}})
    assert unknown.endswith("/tmp/project  ◌ human?")
    absent = cli._row_line({**ROW, "human_presence": {"state": "absent", "protected": False}})
    assert absent.endswith("/tmp/project")


def test_listing_has_no_legacy_unmanaged_pane_section():
    out = cli._format_ls([ROW], tree=False)
    assert "unmanaged" not in out
    assert "PANE" not in out


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


def test_watch_and_json_cannot_be_combined():
    """One prints once and exits, the other never exits. Fail loudly."""
    with pytest.raises(SystemExit):
        parse("ls", "--watch", "--json")


def test_the_harness_column_lines_up_across_header_and_rows():
    out = cli._format_ls([ROW], tree=False)
    lines = out.splitlines()
    col = lines[0].index("HARNESS")
    assert lines[1].index("vibe") == col


def test_a_participant_row_carries_its_harness_icon():
    assert cli.harness_icon("vibe") in cli._row_line(ROW)


# The tests below drive the commands themselves: what they ask the daemon for, what they
# print and what they return. `call_sync` is the seam between a command and the socket.
def test_ls_prints_participants_without_legacy_unmanaged_scan(answers, capsys):
    answers["replies"] = {"participants.list": [ROW]}
    assert cli.cmd_ls(parse("ls")) == 0
    assert "p-abc123" in capsys.readouterr().out
    assert answers["calls"] == [("participants.list", {"include_dead": False})]


def test_ls_json_carries_participants_only(answers, capsys):
    answers["replies"] = {"participants.list": [ROW]}
    assert cli.cmd_ls(parse("ls", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"participants": [ROW]}


def test_ls_tree_does_not_ask_for_unmanaged_panes(answers):
    """The tree is lineage; a pane with no participant has no place in it."""
    answers["replies"] = {"participants.tree": [ROW]}
    assert cli.cmd_ls(parse("ls", "--tree")) == 0
    assert [m for m, _ in answers["calls"]] == ["participants.tree"]


def test_ls_all_asks_for_the_dead_too(answers):
    answers["replies"] = {"participants.list": [], "participants.unmanaged": []}
    cli.cmd_ls(parse("ls", "--all"))
    assert answers["calls"][0] == ("participants.list", {"include_dead": True})


def test_a_dead_participant_renders_with_a_dash_for_its_name():
    """Dead rows have name=None; clip_name renders that as '-', not 'None'."""
    dead = {**ROW, "name": None, "status": "dead", "addressable": False}
    out = cli._format_ls([dead], tree=False)
    lines = out.splitlines()
    name_idx = lines[0].index("NAME")
    assert lines[1][name_idx] == "-"


def test_ls_all_json_preserves_null_name_for_dead_participants(answers, capsys):
    """A dead row's name is JSON null, not the CLI dash — the dash is rendering."""
    dead = {**ROW, "name": None, "status": "dead", "addressable": False}
    answers["replies"] = {"participants.list": [dead], "participants.unmanaged": []}
    assert cli.cmd_ls(parse("ls", "--all", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["participants"][0]["name"] is None


@pytest.mark.parametrize("killed", [True, False])
def test_kill_names_what_it_killed(answers, capsys, killed):
    answers["replies"] = {"participant.kill": {"id": "p-abc123", "killed": killed}}
    assert cli.cmd_kill(parse("kill", "p-abc123")) == 0
    assert answers["calls"] == [("participant.kill", {"id": "p-abc123"})]
    assert ("killed p-abc123" if killed else "already dead: p-abc123") in capsys.readouterr().out


def test_kill_forget_forwards_the_flag(answers, capsys):
    answers["replies"] = {"participant.kill": {"id": "p-1", "killed": True, "forgotten": True}}
    assert cli.cmd_kill(parse("kill", "--forget", "p-1")) == 0
    assert answers["calls"] == [("participant.kill", {"id": "p-1", "forget": True})]
    assert "forgot p-1" in capsys.readouterr().out


def test_name_command_calls_rename_and_prints_the_result(answers, capsys):
    answers["replies"] = {"participant.rename": {**ROW, "name": "Pierrot"}}
    assert cli.cmd_name(parse("name", "Arlequin", "Pierrot")) == 0
    assert answers["calls"] == [("participant.rename", {"id": "Arlequin", "name": "Pierrot"})]
    assert "Pierrot" in capsys.readouterr().out


def test_candidates_prints_metadata_without_content(answers, capsys):
    answers["replies"] = {
        "transcript.candidates": {
            "id": "p-abc123",
            "candidates": [
                {
                    "location": "/tmp/session/messages.jsonl",
                    "session_id": "sid-1",
                    "mtime": 0,
                    "size": 120,
                    "provenance": "unattributed",
                    "rejection_reason": None,
                    "owner": None,
                    "tombstone": None,
                }
            ],
        }
    }
    assert cli.cmd_candidates(parse("candidates", "p-abc123")) == 0
    out = capsys.readouterr().out
    assert "/tmp/session/messages.jsonl" in out
    assert "sid-1" in out
    assert "content" not in out.lower()
    assert answers["calls"] == [("transcript.candidates", {"id": "p-abc123"})]


def test_bind_forwards_confirmation_and_transfer_flags(answers, capsys):
    answers["replies"] = {
        "transcript.bind": {
            "id": "p-target",
            "location": "/tmp/session/messages.jsonl",
            "session_id": "sid-1",
            "prior_owner": "p-old",
        }
    }
    assert (
        cli.cmd_bind(
            parse(
                "bind",
                "p-target",
                "/tmp/session/messages.jsonl",
                "--confirm-id",
                "p-target",
                "--transfer-from",
                "p-old",
                "--transfer-confirm-id",
                "p-old",
            )
        )
        == 0
    )
    assert answers["calls"] == [
        (
            "transcript.bind",
            {
                "id": "p-target",
                "candidate": "/tmp/session/messages.jsonl",
                "confirm_id": "p-target",
                "transfer_from": "p-old",
                "transfer_confirm_id": "p-old",
            },
        )
    ]
    assert "transferred from p-old" in capsys.readouterr().out


def test_adopt_explains_the_provider_aware_replacement() -> None:
    with pytest.raises(cli.BadUsage, match="provider-aware frontend"):
        cli.cmd_adopt(parse("adopt"))


def test_stats_json_is_the_daemon_answer(answers, capsys):
    data = {"since": 0, "harnesses": [], "refusals": []}
    answers["replies"] = {"stats": data}
    assert cli.cmd_stats(parse("stats", "--json")) == 0
    assert answers["calls"] == [("stats", {})]
    assert json.loads(capsys.readouterr().out) == data


def test_ls_watch_redraws_a_whole_frame_each_time(monkeypatch, capsys):
    """A partial redraw would leave the previous frame's rows on screen."""
    client = FollowClient([[ROW], [], [ROW], []])
    monkeypatch.setattr(participants_mod, "DaemonClient", lambda: client)
    with pytest.raises(StopFollow):
        cli.cmd_ls(parse("ls", "--watch", "--interval", "0"))
    out = capsys.readouterr().out
    assert out.count(cli._CLEAR) == 4, "one screen clear per participant-list frame"
    assert out.count("p-abc123") == 2


def test_ls_watch_in_tree_mode_never_asks_for_unmanaged_panes(monkeypatch):
    """Unmanaged panes have no place in a lineage tree — no parent, no children."""
    client = FollowClient([[ROW]])
    monkeypatch.setattr(participants_mod, "DaemonClient", lambda: client)
    with pytest.raises(StopFollow):
        cli.cmd_ls(parse("ls", "--watch", "--tree", "--interval", "0"))
    assert [m for m, _ in client.calls] == ["participants.tree", "participants.tree"]
