"""`theater spawn`: forwarded parameters, model, provider, worktree and wiring flags."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.cli_fakes import parse
from theater import cli


def test_spawn_passes_the_prompt_and_the_cwd(answers, capsys):
    answers["replies"] = {"spawn": {"id": "p-new", "harness": "vibe", "tmux_pane": "%4"}}
    assert cli.cmd_spawn(parse("spawn", "vibe", "say hello", "--approval", "manual")) == 0
    method, params = answers["calls"][0]
    assert method == "spawn"
    assert params["harness"] == "vibe"
    assert params["prompt"] == "say hello"
    assert "tmux_session" not in params
    assert "p-new" in capsys.readouterr().out


def test_spawn_forwards_explicit_provider(answers):
    answers["replies"] = {
        "spawn": {
            "id": "p-new",
            "harness": "vibe",
            "operation_id": "op-new",
            "operation_state": "accepted",
        }
    }

    assert (
        cli.cmd_spawn(parse("spawn", "vibe", "hi", "--approval", "manual", "--provider", "ssh-a"))
        == 0
    )
    assert answers["calls"] == [
        (
            "spawn",
            {
                "harness": "vibe",
                "prompt": "hi",
                "approval": "manual",
                "cwd": str(Path.cwd()),
                "provider": "ssh-a",
                "parent_id": None,
                "background": True,
                "worktree": False,
                "base_branch": None,
                "model": None,
                "reasoning_effort": None,
                "wiring": "auto",
            },
        )
    ]


def test_private_management_commands_use_the_shared_private_methods(answers, capsys):
    answers["replies"] = {
        "providers.list": {"items": [{"provider_id": "pr-1", "selector": "tmux"}]},
        "workspaces.get": {"workspace_id": "ws-1", "state": "active"},
        "controls.transfer": {"participants": [{"participant_id": "p-1"}]},
    }

    assert cli.cmd_providers(parse("providers", "list", "--json")) == 0
    assert cli.cmd_workspaces(parse("workspaces", "get", "ws-1", "--json")) == 0
    assert (
        cli.cmd_control_transfer(
            parse(
                "control-transfer",
                "p-1",
                "--expected-revision",
                "4",
                "--to",
                "local_operator",
                "--idempotency-key",
                "transfer-1",
                "--json",
            )
        )
        == 0
    )
    assert answers["calls"] == [
        ("providers.list", {"cursor": None, "limit": 200}),
        ("workspaces.get", {"workspace_id": "ws-1"}),
        (
            "controls.transfer",
            {
                "participants": [{"participant_id": "p-1", "expected_revision": 4}],
                "new_owner": {"kind": "local_operator", "participant_id": None},
                "idempotency_key": "transfer-1",
            },
        ),
    ]
    assert '"provider_id": "pr-1"' in capsys.readouterr().out


def test_spawn_sends_the_model_it_was_given(answers):
    answers["replies"] = {"spawn": {"id": "p-new", "harness": "vibe", "tmux_pane": "%4"}}
    cli.cmd_spawn(parse("spawn", "vibe", "hi", "--approval", "manual", "--model", "big-one"))
    assert answers["calls"][0][1]["model"] == "big-one"


def test_spawn_sends_no_model_when_none_was_named(answers):
    """None, not absent: the daemon reads it with .get either way, but a
    spawn that did not choose must be distinguishable from one that did."""
    answers["replies"] = {"spawn": {"id": "p-new", "harness": "vibe", "tmux_pane": "%4"}}
    cli.cmd_spawn(parse("spawn", "vibe", "hi", "--approval", "manual"))
    assert answers["calls"][0][1]["model"] is None


def test_spawn_json_prints_the_record_verbatim(answers, capsys):
    answers["replies"] = {"spawn": {"id": "p-new", "harness": "vibe", "tmux_pane": "%4"}}
    assert cli.cmd_spawn(parse("spawn", "vibe", "hi", "--approval", "manual", "--json")) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "p-new"


def test_spawn_rejects_an_unknown_harness_by_name(monkeypatch):
    """Not argparse `choices`: declared harnesses arrive after the parser."""
    with pytest.raises(cli.BadUsage) as exc:
        cli._spawn_harness(parse("spawn", "nosuch", "hi", "--approval", "manual"))
    assert "unknown harness" in str(exc.value)
    assert "--prompt" in str(exc.value), "the likely mistake is naming a prompt"


def test_spawn_bare_worktree_flag_sends_true(answers):
    """Bare --worktree sends True (unique isolated worktree)."""
    answers["replies"] = {"spawn": {"id": "p-new", "harness": "vibe", "tmux_pane": "%4"}}
    cli.cmd_spawn(parse("spawn", "vibe", "hi", "--approval", "manual", "--worktree"))
    assert answers["calls"][0][1]["worktree"] is True


def test_spawn_worktree_with_name_sends_string(answers):
    """--worktree NAME sends the string (named shared worktree)."""
    answers["replies"] = {"spawn": {"id": "p-new", "harness": "vibe", "tmux_pane": "%4"}}
    cli.cmd_spawn(parse("spawn", "vibe", "hi", "--approval", "manual", "--worktree", "shared"))
    assert answers["calls"][0][1]["worktree"] == "shared"


def test_spawn_without_worktree_sends_false(answers):
    """Omitting --worktree sends False."""
    answers["replies"] = {"spawn": {"id": "p-new", "harness": "vibe", "tmux_pane": "%4"}}
    cli.cmd_spawn(parse("spawn", "vibe", "hi", "--approval", "manual"))
    assert answers["calls"][0][1]["worktree"] is False


def test_spawn_defaults_wiring_to_auto(answers):
    answers["replies"] = {"spawn": {"id": "p-new", "harness": "vibe", "tmux_pane": "%4"}}
    assert cli.cmd_spawn(parse("spawn", "vibe", "hi", "--approval", "manual")) == 0
    assert answers["calls"][0][1]["wiring"] == "auto"


def test_spawn_passes_the_explicit_legacy_opt_out(answers):
    answers["replies"] = {"spawn": {"id": "p-new", "harness": "vibe", "tmux_pane": "%4"}}
    assert (
        cli.cmd_spawn(parse("spawn", "vibe", "hi", "--approval", "manual", "--wiring", "legacy"))
        == 0
    )
    assert answers["calls"][0][1]["wiring"] == "legacy"


def test_spawn_rejects_unknown_wiring_at_parse_time():
    with pytest.raises(SystemExit):
        cli._parser().parse_args(
            ["spawn", "vibe", "hi", "--approval", "manual", "--wiring", "bogus"]
        )
