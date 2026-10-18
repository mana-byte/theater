"""Config loading and rejection, file paths, daemon wiring and the shipped example file."""

from __future__ import annotations

from dataclasses import MISSING, fields
from functools import partial

import pytest

from tests.support.config_file import write_config
from tests.support.paths import REPO_ROOT
from tests.support.tables import run_rows
from theater import config as cfg
from theater import paths
from theater.client import DaemonClient
from theater.daemon.server import Daemon
from theater.protocol import RemoteError


def test_missing_file_is_not_an_error():
    loaded = cfg.load()
    assert loaded.exists is False
    assert loaded.rails.depth_cap == 3
    assert loaded.observer.poll_interval == 0.25
    assert loaded.scratchpad.ttl_days == 7.0
    assert loaded.terminals.default_provider == "tmux"
    assert loaded.mcp.enabled == ["beads"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[mcp]\n", ["beads"]),
        ("[mcp]\nenabled = []\n", []),
        ('[mcp]\nenabled = ["acme"]\n', ["acme"]),
    ],
)
def test_mcp_defaults_preserve_explicit_plugin_lists(text, expected):
    write_config(text)
    assert cfg.load().mcp.enabled == expected


def test_missing_file_reports_every_value_as_default():
    loaded = cfg.load()
    assert loaded.source("rails.budget") == "default"
    assert {source for _, _, source in cfg.describe(loaded)} == {"default"}


def test_empty_file_is_all_defaults_but_exists():
    write_config("")
    loaded = cfg.load()
    assert loaded.exists is True
    assert loaded.rails.budget == 20


def test_absent_skills_section_has_an_empty_denylist():
    loaded = cfg.load()

    assert loaded.skills.disabled == ()


def test_values_override_defaults():
    write_config("[rails]\ndepth_cap = 5\nbudget = 100\n")
    loaded = cfg.load()
    assert loaded.rails.depth_cap == 5
    assert loaded.rails.budget == 100


def test_partial_section_leaves_siblings_at_default():
    write_config("[rails]\ndepth_cap = 5\n")
    loaded = cfg.load()
    assert loaded.rails.depth_cap == 5
    assert loaded.rails.budget == 20
    assert loaded.source("rails.depth_cap") == "config.toml"
    assert loaded.source("rails.budget") == "default"


def test_event_retention_is_independently_configurable():
    write_config("[retention]\nevents_days = 11\nbus_days = 3\n")

    loaded = cfg.load()

    assert loaded.retention.events_days == 11
    assert loaded.retention.bus_days == 3
    assert loaded.retention.jobs_days == 15
    assert loaded.source("retention.events_days") == "config.toml"


def test_scratchpad_ttl_accepts_positive_finite_days():
    write_config("[scratchpad]\nttl_days = 0.5\n")

    loaded = cfg.load()

    assert loaded.scratchpad.ttl_days == 0.5
    assert loaded.source("scratchpad.ttl_days") == "config.toml"


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_scratchpad_ttl_rejects_non_positive_or_non_finite_days(value):
    write_config(f"[scratchpad]\nttl_days = {value}\n")

    with pytest.raises(cfg.ConfigError, match=r"scratchpad\.ttl_days"):
        cfg.load()


def test_terminal_default_provider_accepts_a_nonempty_selector():
    write_config('[terminals]\ndefault_provider = "wezterm"\n')

    loaded = cfg.load()

    assert loaded.terminals.default_provider == "wezterm"
    assert loaded.source("terminals.default_provider") == "config.toml"


@pytest.mark.parametrize("value", ['""', '"   "'])
def test_terminal_default_provider_rejects_a_blank_selector(value):
    write_config(f"[terminals]\ndefault_provider = {value}\n")

    with pytest.raises(cfg.ConfigError, match=r"terminals\.default_provider"):
        cfg.load()


def test_legacy_regie_table_names_the_manual_destination_without_rewriting() -> None:
    original = '[regie]\ntheme = "nord"\n'
    write_config(original)

    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()

    message = str(exc.value)
    assert "$THEATER_HOME/regie/config.toml" in message
    assert "manually" in message
    assert "never rewrites" in message
    assert paths.config_path().read_text(encoding="utf-8") == original


def test_whole_number_is_accepted_for_an_interval():
    """`poll_interval = 1` means one second, not a type error."""
    write_config("[observer]\npoll_interval = 1\n")
    loaded = cfg.load()
    assert loaded.observer.poll_interval == 1.0
    assert isinstance(loaded.observer.poll_interval, float)


def test_favourite_is_a_plain_string():
    write_config('[theater]\nfavourite = "vibe"\n')
    loaded = cfg.load()
    assert loaded.theater.favourite == "vibe"


def test_skills_disabled_parses_as_an_immutable_permissive_denylist():
    write_config('[skills]\ndisabled = ["theater-debate", "not-installed"]\n')

    loaded = cfg.load()

    assert loaded.skills.disabled == ("theater-debate", "not-installed")
    assert loaded.source("skills.disabled") == "config.toml"


def test_describe_reports_source_per_key():
    write_config("[rails]\nbudget = 7\n")
    rows = {key: (value, source) for key, value, source in cfg.describe(cfg.load())}
    assert rows["rails.budget"] == ("7", "config.toml")
    assert rows["rails.depth_cap"] == ("3", "default")
    assert "regie.theme" not in rows


def test_unknown_key_suggests_the_real_one():
    write_config("[rails]\ndepth_capp = 5\n")
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    assert "depth_cap" in str(exc.value)


def test_unknown_skills_key_is_fatal_and_suggests_disabled():
    write_config("[skills]\ndisable = []\n")

    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()

    assert "skills.disable" in str(exc.value)
    assert "disabled" in str(exc.value)


def test_unknown_section_is_fatal_and_suggests():
    write_config("[railz]\ndepth_cap = 5\n")
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    message = str(exc.value)
    assert "railz" in message
    assert "rails" in message


def test_error_names_the_file():
    write_config("[rails]\nnope = 1\n")
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    assert str(paths.config_path()) in str(exc.value)


def test_malformed_toml_is_fatal():
    write_config("[rails\ndepth_cap = 5\n")
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    assert "not valid TOML" in str(exc.value)


def test_wrong_type_is_fatal():
    write_config('[rails]\ndepth_cap = "three"\n')
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    assert "must be an integer" in str(exc.value)


@pytest.mark.parametrize(
    "text",
    [
        "[rails]\ndepth_cap = 3.5\n",
        "[rails]\ndepth_cap = true\n",  # True is an int in Python, not a depth cap
        "[observer]\npoll_interval = true\n",
    ],
    ids=["float-for-integer", "bool-for-integer", "bool-for-float"],
)
def test_a_wrongly_typed_number_is_rejected(text):
    write_config(text)
    with pytest.raises(cfg.ConfigError):
        cfg.load()


def test_section_must_be_a_table():
    write_config('rails = "yes"\n')
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    assert "must be a table" in str(exc.value)


def test_out_of_range_is_fatal():
    """A zero poll interval spins a core; a zero budget spawns nothing ever."""

    def rejects(body: str) -> None:
        write_config(body)
        with pytest.raises(cfg.ConfigError) as exc:
            cfg.load()
        assert "must be >=" in str(exc.value)

    run_rows(
        (body.strip().splitlines()[-1], partial(rejects, body))
        for body in [
            "[observer]\npoll_interval = 0.0001\n",
            "[rails]\nbudget = 0\n",
            "[rails]\ndepth_cap = -1\n",
        ]
    )


def test_infinite_interval_is_fatal():
    write_config("[observer]\npoll_interval = inf\n")
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    assert "finite" in str(exc.value)


def test_depth_cap_zero_is_legal():
    """Zero is a meaningful cap: roots may not spawn at all."""
    write_config("[rails]\ndepth_cap = 0\n")
    assert cfg.load().rails.depth_cap == 0


def test_config_path_is_under_theater_home(theater_home):
    assert paths.config_path() == theater_home / "config.toml"


def test_explicit_path_overrides_the_default(tmp_path):
    other = tmp_path / "elsewhere.toml"
    other.write_text("[rails]\nbudget = 99\n", encoding="utf-8")
    loaded = cfg.load(other)
    assert loaded.rails.budget == 99
    assert loaded.path == other


def test_daemon_reads_the_file_at_construction():
    write_config("[rails]\ndepth_cap = 7\nbudget = 42\n")
    daemon = Daemon(harnesses={})
    try:
        assert daemon.config.rails.depth_cap == 7
        assert daemon.config.rails.budget == 42
    finally:
        daemon.store.close()


def test_observer_intervals_come_from_the_file():
    write_config("[observer]\npoll_interval = 3\nawaiting_input_timeout = 30\n")
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
    write_config("[rails]\ndepth_cap = 7\n")
    daemon = Daemon(harnesses={}, config=cfg.Config())
    try:
        assert daemon.config.rails.depth_cap == 3
    finally:
        daemon.store.close()


async def test_configured_depth_cap_actually_rejects_a_spawn():
    """The end-to-end claim: a number in the file changes what the daemon does."""
    write_config("[rails]\ndepth_cap = 0\n")
    daemon = Daemon(harnesses={})
    await daemon.start()
    client = DaemonClient(autostart=False)
    await client.connect()
    try:
        root = daemon.registry.register(harness="vibe", pane=None, cwd="/tmp")
        with pytest.raises(RemoteError) as exc:
            await client.call(
                "spawn",
                harness="vibe",
                prompt="hi",
                approval="manual",
                cwd="/tmp",
                parent_id=root.id,
            )
        assert exc.value.code == "depth_exceeded"
    finally:
        await client.aclose()
        await daemon.aclose()


EXAMPLE = REPO_ROOT / "config.example.toml"


#: Settings whose default is None, which TOML cannot express: writing the key
#: down at all would change behaviour, so these are the ones the example leaves
#: commented. Named here so their absence is asserted rather than tolerated.
NO_DEFAULT = {
    "theater.favourite",
    "observability.otlp_endpoint",
}


def default_of(f) -> object:
    """The declared default of a field, however it was declared.

    A mutable default has to come from a factory, so `f.default` is MISSING for
    exactly the settings whose value is a list — which is not a difference the
    example file is allowed to have an opinion about.
    """
    return f.default_factory() if f.default is MISSING else f.default


@pytest.fixture
def load(tmp_path, monkeypatch):
    """Load an arbitrary config body as if it were the user's file."""

    def _load(text: str) -> cfg.Config:
        (tmp_path / "config.toml").write_text(text)
        monkeypatch.setattr(paths, "home", lambda: tmp_path)
        return cfg.load()

    return _load


def test_as_shipped_it_parses_and_changes_nothing(load):
    """Copying it unedited must behave exactly like having no config at all.

    Not the same as being *sourced* like one: every key is written down, so
    `theater config` reports the file rather than the code. That is the price
    of a copyable example, and the next test is what pins it.
    """
    got = load(EXAMPLE.read_text())
    plain = cfg.Config()
    for section in cfg._SECTIONS:
        assert getattr(got, section) == getattr(plain, section)
    # Per-key: a mismatch names the offending setting, not just the section.
    for section, klass in cfg._SECTIONS.items():
        for f in fields(klass):
            dotted = f"{section}.{f.name}"
            if dotted in NO_DEFAULT:
                continue
            assert getattr(getattr(got, section), f.name) == default_of(f), dotted


def test_every_setting_is_written_down(load):
    """A key the code accepts but the example omits is undocumented."""
    full = load(EXAMPLE.read_text())
    missing = [
        dotted
        for section, klass in cfg._SECTIONS.items()
        for f in fields(klass)
        if (dotted := f"{section}.{f.name}") not in NO_DEFAULT and full.source(dotted) == "default"
    ]
    assert not missing, f"absent from {EXAMPLE.name}: {', '.join(missing)}"


def test_unset_settings_stay_unwritten(load):
    """Guard the exemption: NO_DEFAULT must not quietly grow stale.

    These mean "unset", and TOML has no word for that. If a default ever becomes
    expressible, this fails and the key should move out of the exemption.
    """
    full = load(EXAMPLE.read_text())
    for dotted in NO_DEFAULT:
        section, name = dotted.split(".")
        assert full.source(dotted) == "default", dotted
        assert getattr(getattr(full, section), name) is None, dotted


def test_the_model_allowlist_ships_empty(load):
    """`[models]` is documented but grants nothing.

    The one section with no default to write out: every name in it is a model
    an agent that can spawn may then spend, so a copied example that came with
    entries would hand out that permission to everyone who ran `cp`. The header
    is live because an empty table means exactly what absence means, and having
    it there is what makes the prose above it findable.
    """
    full = load(EXAMPLE.read_text())
    assert full.models == {}
    assert full.models_for("claude") == []
