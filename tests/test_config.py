"""Config loading, and above all config *rejecting*.

The interesting half of this file is the failures. A config layer that reads
correct files is easy; the requirement that earned this module its docstring is
that a wrong file stops the daemon instead of being quietly half-applied.
"""

from __future__ import annotations

import json

import pytest

from theater import cli, paths
from theater import config as cfg
from theater.client import DaemonClient
from theater.daemon.server import Daemon
from theater.protocol import RemoteError


def write(text: str) -> None:
    paths.config_path().write_text(text, encoding="utf-8")


# ---- defaults -----------------------------------------------------------


def test_missing_file_is_not_an_error():
    loaded = cfg.load()
    assert loaded.exists is False
    assert loaded.rails.depth_cap == 3
    assert loaded.observer.poll_interval == 0.25
    assert loaded.regie.theme is None


def test_missing_file_reports_every_value_as_default():
    loaded = cfg.load()
    assert loaded.source("rails.budget") == "default"
    assert {source for _, _, source in cfg.describe(loaded)} == {"default"}


def test_empty_file_is_all_defaults_but_exists():
    write("")
    loaded = cfg.load()
    assert loaded.exists is True
    assert loaded.rails.budget == 20


def test_defaults_match_the_constants_they_replace():
    """The move to config must not have changed any shipped behaviour."""
    from theater.daemon import observer, rails
    from theater.regie import app

    loaded = cfg.load()
    assert loaded.rails.depth_cap == rails.DEFAULT_DEPTH_CAP
    assert loaded.rails.budget == rails.DEFAULT_BUDGET
    assert loaded.observer.poll_interval == observer.POLL_INTERVAL
    assert loaded.observer.relocate_timeout == observer.RELOCATE_TIMEOUT
    assert loaded.observer.awaiting_input_timeout == observer.AWAITING_INPUT_TIMEOUT
    assert loaded.observer.search_interval == observer.SEARCH_INTERVAL
    assert loaded.observer.sync_interval == observer.SYNC_INTERVAL
    assert loaded.regie.tree_interval == app.TREE_INTERVAL
    assert loaded.regie.bus_interval == app.BUS_INTERVAL
    assert loaded.regie.bus_batch == app.BUS_BATCH


# ---- reading ------------------------------------------------------------


def test_values_override_defaults():
    write("[rails]\ndepth_cap = 5\nbudget = 100\n")
    loaded = cfg.load()
    assert loaded.rails.depth_cap == 5
    assert loaded.rails.budget == 100


def test_partial_section_leaves_siblings_at_default():
    write("[rails]\ndepth_cap = 5\n")
    loaded = cfg.load()
    assert loaded.rails.depth_cap == 5
    assert loaded.rails.budget == 20
    assert loaded.source("rails.depth_cap") == "config.toml"
    assert loaded.source("rails.budget") == "default"


def test_whole_number_is_accepted_for_an_interval():
    """`poll_interval = 1` means one second, not a type error."""
    write("[observer]\npoll_interval = 1\n")
    loaded = cfg.load()
    assert loaded.observer.poll_interval == 1.0
    assert isinstance(loaded.observer.poll_interval, float)


def test_theme_and_favourite_are_plain_strings():
    write('[regie]\ntheme = "nord"\n\n[theater]\nfavourite = "vibe"\n')
    loaded = cfg.load()
    assert loaded.regie.theme == "nord"
    assert loaded.theater.favourite == "vibe"


def test_describe_reports_source_per_key():
    write("[rails]\nbudget = 7\n")
    rows = {key: (value, source) for key, value, source in cfg.describe(cfg.load())}
    assert rows["rails.budget"] == ("7", "config.toml")
    assert rows["rails.depth_cap"] == ("3", "default")
    assert rows["regie.theme"] == ("(unset)", "default")


# ---- rejecting ----------------------------------------------------------


def test_unknown_key_is_fatal():
    write("[rails]\ndepth_capp = 5\n")
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    assert "depth_capp" in str(exc.value)


def test_unknown_key_suggests_the_real_one():
    write("[rails]\ndepth_capp = 5\n")
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    assert "depth_cap" in str(exc.value)


def test_unknown_section_is_fatal_and_suggests():
    write("[railz]\ndepth_cap = 5\n")
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    message = str(exc.value)
    assert "railz" in message
    assert "rails" in message


def test_error_names_the_file():
    write("[rails]\nnope = 1\n")
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    assert str(paths.config_path()) in str(exc.value)


def test_malformed_toml_is_fatal():
    write("[rails\ndepth_cap = 5\n")
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    assert "not valid TOML" in str(exc.value)


def test_wrong_type_is_fatal():
    write('[rails]\ndepth_cap = "three"\n')
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    assert "must be an integer" in str(exc.value)


def test_float_is_rejected_for_an_integer_field():
    write("[rails]\ndepth_cap = 3.5\n")
    with pytest.raises(cfg.ConfigError):
        cfg.load()


def test_bool_is_rejected_for_an_integer_field():
    """True is an int in Python. It is not a depth cap."""
    write("[rails]\ndepth_cap = true\n")
    with pytest.raises(cfg.ConfigError):
        cfg.load()


def test_bool_is_rejected_for_a_float_field():
    write("[observer]\npoll_interval = true\n")
    with pytest.raises(cfg.ConfigError):
        cfg.load()


def test_non_string_theme_is_fatal():
    write("[regie]\ntheme = 3\n")
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    assert "must be a string" in str(exc.value)


def test_section_must_be_a_table():
    write('rails = "yes"\n')
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    assert "must be a table" in str(exc.value)


@pytest.mark.parametrize(
    "body",
    [
        "[observer]\npoll_interval = 0\n",
        "[observer]\npoll_interval = -1.0\n",
        "[observer]\npoll_interval = 0.0001\n",
        "[rails]\nbudget = 0\n",
        "[rails]\ndepth_cap = -1\n",
        "[regie]\nbus_batch = 0\n",
    ],
)
def test_out_of_range_is_fatal(body):
    """A zero poll interval spins a core; a zero budget spawns nothing ever."""
    write(body)
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    assert "must be >=" in str(exc.value)


def test_infinite_interval_is_fatal():
    write("[observer]\npoll_interval = inf\n")
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    assert "finite" in str(exc.value)


def test_depth_cap_zero_is_legal():
    """Zero is a meaningful cap: roots may not spawn at all."""
    write("[rails]\ndepth_cap = 0\n")
    assert cfg.load().rails.depth_cap == 0


# ---- location -----------------------------------------------------------


def test_config_path_is_under_theater_home(theater_home):
    assert paths.config_path() == theater_home / "config.toml"


def test_explicit_path_overrides_the_default(tmp_path):
    other = tmp_path / "elsewhere.toml"
    other.write_text("[rails]\nbudget = 99\n", encoding="utf-8")
    loaded = cfg.load(other)
    assert loaded.rails.budget == 99
    assert loaded.path == other


# ---- reaching the daemon ------------------------------------------------
#
# A config layer nothing reads is the failure this section exists to catch.
# Loading correctly is not the same as being obeyed.


def test_daemon_reads_the_file_at_construction():
    write("[rails]\ndepth_cap = 7\nbudget = 42\n")
    daemon = Daemon(harnesses={})
    try:
        assert daemon.config.rails.depth_cap == 7
        assert daemon.config.rails.budget == 42
    finally:
        daemon.store.close()


def test_observer_intervals_come_from_the_file():
    write("[observer]\npoll_interval = 3\nawaiting_input_timeout = 30\n")
    daemon = Daemon(harnesses={})
    try:
        assert daemon.observer.poll == 3.0
        assert daemon.observer.awaiting == 30.0
        # untouched keys keep their defaults
        assert daemon.observer.sync == 1.0
    finally:
        daemon.store.close()


def test_an_injected_config_wins_over_the_file():
    """Tests must be able to configure a daemon without writing to disk."""
    write("[rails]\ndepth_cap = 7\n")
    daemon = Daemon(harnesses={}, config=cfg.Config())
    try:
        assert daemon.config.rails.depth_cap == 3
    finally:
        daemon.store.close()


async def test_configured_depth_cap_actually_rejects_a_spawn(fake_tmux):
    """The end-to-end claim: a number in the file changes what the daemon does."""
    write("[rails]\ndepth_cap = 0\n")
    daemon = Daemon(harnesses={})
    await daemon.start()
    client = DaemonClient(autostart=False)
    await client.connect()
    try:
        root = await client.call(
            "spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp"
        )
        with pytest.raises(RemoteError) as exc:
            await client.call(
                "spawn",
                harness="vibe",
                prompt="hi",
                approval="manual",
                cwd="/tmp",
                parent_id=root["id"],
            )
        assert exc.value.code == "depth_exceeded"
    finally:
        await client.aclose()
        await daemon.aclose()


# ---- the CLI ------------------------------------------------------------


def run_cli(*argv) -> int:
    return cli.main(list(argv))


def test_config_path_prints_the_file(capsys):
    assert run_cli("config", "path") == 0
    assert capsys.readouterr().out.strip() == str(paths.config_path())


def test_config_path_works_when_no_file_exists(capsys):
    assert not paths.config_path().exists()
    assert run_cli("config", "path") == 0
    assert str(paths.config_path()) in capsys.readouterr().out


def test_config_says_when_there_is_no_file(capsys):
    assert run_cli("config") == 0
    assert "no file yet" in capsys.readouterr().out


def test_config_marks_values_that_came_from_the_file(capsys):
    write("[rails]\nbudget = 42\n")
    assert run_cli("config") == 0
    out = capsys.readouterr().out
    assert "rails.budget" in out
    assert "42" in out
    assert "<- config.toml" in out


def test_config_reports_a_bad_file_instead_of_crashing(capsys):
    write("[rails]\nnonsense = 1\n")
    assert run_cli("config") == 1
    assert "nonsense" in capsys.readouterr().err


def test_config_json_carries_the_source(capsys):
    write("[rails]\nbudget = 42\n")
    assert run_cli("config", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["exists"] is True
    by_key = {row["key"]: row for row in payload["settings"]}
    assert by_key["rails.budget"]["source"] == "config.toml"
    assert by_key["rails.depth_cap"]["source"] == "default"


# ---- the theme reaches the régie ----------------------------------------


def make_app(theme: str | None):
    from theater.regie.app import RegieApp

    return RegieApp(cfg.Config(regie=cfg.RegieSection(theme=theme)))


def test_a_known_theme_is_applied():
    app = make_app("nord")
    app._apply_theme()
    assert app.theme == "nord"


def test_no_theme_leaves_textuals_default():
    app = make_app(None)
    before = app.theme
    app._apply_theme()
    assert app.theme == before


def test_an_unknown_theme_is_reported_not_fatal(monkeypatch):
    """Cosmetic mistake. Crashing would hide every agent on the machine."""
    app = make_app("bogus")
    said: list[str] = []
    monkeypatch.setattr(app, "notify", lambda msg, **k: said.append(msg))
    before = app.theme
    app._apply_theme()
    assert app.theme == before
    assert said and "bogus" in said[0]


def test_an_unknown_theme_lists_the_real_ones(monkeypatch):
    app = make_app("bogus")
    said: list[str] = []
    monkeypatch.setattr(app, "notify", lambda msg, **k: said.append(msg))
    app._apply_theme()
    assert "nord" in said[0]


def test_the_theme_is_applied_from_the_file_on_disk():
    """End to end: the régie the CLI builds carries what the file says."""
    from theater.regie.app import RegieApp

    write('[regie]\ntheme = "nord"\n')
    app = RegieApp(cfg.load())
    app._apply_theme()
    assert app.theme == "nord"


# ---- the favourite reaches spawn ----------------------------------------


def spawn_args(*argv):
    return cli._parser().parse_args(["spawn", *argv, "--approval", "manual"])


def test_a_named_harness_wins_over_the_favourite():
    write('[theater]\nfavourite = "claude"\n')
    assert cli._spawn_harness(spawn_args("vibe")) == "vibe"


def test_the_favourite_is_used_when_no_harness_is_named():
    write('[theater]\nfavourite = "claude"\n')
    assert cli._spawn_harness(spawn_args()) == "claude"


def test_no_harness_and_no_favourite_says_how_to_fix_it():
    with pytest.raises(cli.BadUsage) as exc:
        cli._spawn_harness(spawn_args())
    message = str(exc.value)
    assert "theater.favourite" in message
    assert str(paths.config_path()) in message


def test_a_favourite_that_is_not_a_harness_is_refused():
    """Loudly: the alternative is spawning something the user did not ask for."""
    write('[theater]\nfavourite = "emacs"\n')
    with pytest.raises(cli.BadUsage) as exc:
        cli._spawn_harness(spawn_args())
    assert "emacs" in str(exc.value)


def test_a_named_harness_survives_a_nonsense_favourite():
    write('[theater]\nfavourite = "emacs"\n')
    assert cli._spawn_harness(spawn_args("vibe")) == "vibe"


# ---- the prompt flag ----------------------------------------------------


def spawned_params(monkeypatch, *argv) -> dict:
    seen: dict = {}

    def fake_call(method, **params):
        seen.update(params)
        return {"id": "abc", "harness": params["harness"], "tmux_pane": "%1"}

    monkeypatch.setattr(cli, "call_sync", fake_call)
    monkeypatch.setattr(cli.tmux, "current_session_sync", lambda: "main")
    assert cli.main(["spawn", *argv, "--approval", "manual"]) == 0
    return seen


def test_the_positional_prompt_still_works(monkeypatch, capsys):
    seen = spawned_params(monkeypatch, "vibe", "do the thing")
    assert seen["prompt"] == "do the thing"


def test_the_prompt_flag_carries_the_prompt_without_a_harness(monkeypatch, capsys):
    """Two optional positionals would bind a lone prompt to the harness slot."""
    write('[theater]\nfavourite = "vibe"\n')
    seen = spawned_params(monkeypatch, "--prompt", "do the thing")
    assert seen["harness"] == "vibe"
    assert seen["prompt"] == "do the thing"


def test_the_prompt_flag_wins_over_the_positional(monkeypatch, capsys):
    seen = spawned_params(monkeypatch, "vibe", "positional", "--prompt", "flag")
    assert seen["prompt"] == "flag"


def test_spawning_with_no_harness_and_no_favourite_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(cli, "call_sync", lambda *a, **k: {})
    assert cli.main(["spawn", "--approval", "manual"]) == 1
    assert "favourite" in capsys.readouterr().err
