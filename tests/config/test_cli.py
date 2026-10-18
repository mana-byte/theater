"""`theater config`, the favourite harness and the spawn prompt flags."""

from __future__ import annotations

import json

import pytest

from tests.support.config_file import write_config
from theater import cli, paths
from theater.cli.commands import participants as participants_mod


def run_cli(*argv) -> int:
    return cli.main(list(argv))


def test_config_path_prints_the_file(capsys):
    assert run_cli("config", "path") == 0
    assert capsys.readouterr().out.strip() == str(paths.config_path())


def test_config_says_when_there_is_no_file(capsys):
    assert run_cli("config") == 0
    assert "no file yet" in capsys.readouterr().out


def test_config_marks_values_that_came_from_the_file(capsys):
    write_config("[rails]\nbudget = 42\n")
    assert run_cli("config") == 0
    out = capsys.readouterr().out
    assert "rails.budget" in out
    assert "42" in out
    assert "<- config.toml" in out


def test_config_reports_a_bad_file_instead_of_crashing(capsys):
    write_config("[rails]\nnonsense = 1\n")
    assert run_cli("config") == 1
    assert "nonsense" in capsys.readouterr().err


def test_config_json_carries_the_source(capsys):
    write_config("[rails]\nbudget = 42\n")
    assert run_cli("config", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["exists"] is True
    by_key = {row["key"]: row for row in payload["settings"]}
    assert by_key["rails.budget"]["source"] == "config.toml"
    assert by_key["rails.depth_cap"]["source"] == "default"


def spawn_args(*argv):
    return cli._parser().parse_args(["spawn", *argv, "--approval", "manual"])


def test_the_favourite_is_used_when_no_harness_is_named():
    write_config('[theater]\nfavourite = "claude"\n')
    assert cli._spawn_harness(spawn_args()) == "claude"


def test_no_harness_and_no_favourite_says_how_to_fix_it():
    with pytest.raises(cli.BadUsage) as exc:
        cli._spawn_harness(spawn_args())
    message = str(exc.value)
    assert "theater.favourite" in message
    assert str(paths.config_path()) in message


def test_a_favourite_that_is_not_a_harness_is_refused():
    """Loudly: the alternative is spawning something the user did not ask for."""
    write_config('[theater]\nfavourite = "emacs"\n')
    with pytest.raises(cli.BadUsage) as exc:
        cli._spawn_harness(spawn_args())
    assert "emacs" in str(exc.value)


def test_a_named_harness_survives_a_nonsense_favourite():
    write_config('[theater]\nfavourite = "emacs"\n')
    assert cli._spawn_harness(spawn_args("vibe")) == "vibe"


def spawned_params(monkeypatch, *argv) -> dict:
    seen: dict = {}

    def fake_call(method, **params):
        seen.update(params)
        return {"id": "abc", "harness": params["harness"], "tmux_pane": "%1"}

    monkeypatch.setattr(participants_mod, "call_sync", fake_call)
    assert cli.main(["spawn", *argv, "--approval", "manual"]) == 0
    return seen


def test_the_positional_prompt_still_works(monkeypatch, capsys):
    seen = spawned_params(monkeypatch, "vibe", "do the thing")
    assert seen["prompt"] == "do the thing"


def test_the_prompt_flag_carries_the_prompt_without_a_harness(monkeypatch, capsys):
    """Two optional positionals would bind a lone prompt to the harness slot."""
    write_config('[theater]\nfavourite = "vibe"\n')
    seen = spawned_params(monkeypatch, "--prompt", "do the thing")
    assert seen["harness"] == "vibe"
    assert seen["prompt"] == "do the thing"


def test_the_prompt_flag_wins_over_the_positional(monkeypatch, capsys):
    seen = spawned_params(monkeypatch, "vibe", "positional", "--prompt", "flag")
    assert seen["prompt"] == "flag"


def test_spawning_with_no_harness_and_no_favourite_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(participants_mod, "call_sync", lambda *a, **k: {})
    assert cli.main(["spawn", "--approval", "manual"]) == 1
    assert "favourite" in capsys.readouterr().err
