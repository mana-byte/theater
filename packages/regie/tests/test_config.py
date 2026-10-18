"""Régie settings, the shipped example config, public contracts and the home-layout migration."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from regie import BridgeConfig, PresentationTarget, RegieSettings
from regie.ambience.registry import NO_AMBIENCE, load_catalog
from regie.config import SettingsError, load_settings
from regie.migration import migrate_flat_layout
from regie.paths import RegiePaths


def test_load_settings_reads_regie_values_and_shared_favourite(tmp_path: Path) -> None:
    config_root = tmp_path / "regie"
    config_root.mkdir()
    path = config_root / "config.toml"
    path.write_text(
        """
[regie]
theme = "ansi-dark"
tree_interval = 0.5
participant_detail = "description"
dashboard_sentences = ["one", "two"]
"""
    )
    (tmp_path / "config.toml").write_text('[theater]\nfavourite = "claude"\n')

    settings = load_settings(path)

    assert settings.theme == "ansi-dark"
    assert settings.tree_interval == 0.5
    assert settings.participant_detail == "description"
    assert settings.dashboard_sentences == ["one", "two"]
    assert settings.sidebar_width == 52
    assert settings.favourite == "claude"


def test_load_settings_retains_validation_and_defaults(tmp_path: Path) -> None:
    missing = load_settings(tmp_path / "missing.toml")
    invalid = tmp_path / "invalid.toml"
    invalid.write_text("[regie]\nsidebar_width = 12\n")

    assert missing.bus_batch == 50
    assert missing.theme == "ansi-dark"
    with pytest.raises(SettingsError, match="sidebar_width"):
        load_settings(invalid)


def test_usage_footer_is_hidden_unless_configured_visible(tmp_path: Path) -> None:
    shown = tmp_path / "shown.toml"
    shown.write_text("[regie]\nusage_visible = true\n")
    invalid = tmp_path / "invalid-usage.toml"
    invalid.write_text('[regie]\nusage_visible = "yes"\n')

    assert load_settings(tmp_path / "missing.toml").usage_visible is False
    assert load_settings(shown).usage_visible is True
    with pytest.raises(SettingsError, match="usage_visible"):
        load_settings(invalid)


EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"


def test_the_example_loads_with_the_defaults_and_lists_every_shipped_ambience() -> None:
    assert load_settings(EXAMPLE).tree_ambience == RegieSettings().tree_ambience
    listed = set(re.findall(r'^#\s+"([a-z-]+)"\s', EXAMPLE.read_text(), re.MULTILINE))
    assert listed == {*load_catalog(Path("/nonexistent")).scenes, NO_AMBIENCE}


def test_shared_contract_defaults_are_stable() -> None:
    config = BridgeConfig(theater_socket=Path("/tmp/theater.sock"), state_dir=Path("/tmp/regie"))
    target = PresentationTarget(
        provider_id="provider-a",
        provider_kind="tmux",
        terminal_id="%1",
        terminal_incarnation="incarnation-a",
    )

    assert config.selector == "tmux"
    assert RegieSettings().sidebar_width == 52
    assert (target.provider_kind, target.terminal_id) == ("tmux", "%1")


def test_a_flat_home_moves_into_var_once_and_leaves_theaters_logs(tmp_path: Path) -> None:
    paths = RegiePaths(tmp_path)
    old, theater_logs = tmp_path / "regie", tmp_path / "var" / "logs"
    (old / "bridge" / "receipts").mkdir(parents=True)
    (old / "bridge" / "bridge-state.json").write_text("{}")
    (old / "tree-layout.json").write_text("[]")
    (old / "bridge.log").write_text("old bridge\n")
    (old / "daemon-start.log").write_text("started\n")
    (old / "bridge.pid").write_text("999999 stale-token\n")  # a bridge long gone
    (old / "bridge-start.lock").touch()
    (old / "config.toml").write_text("[regie]\n")
    (theater_logs / "regie").mkdir(parents=True)
    (theater_logs / "regie" / "pane-7.log").write_text("ui\n")
    (theater_logs / "daemon").mkdir()

    assert migrate_flat_layout(paths, tmp_path / "daemon.sock")

    assert paths.tree_layout_path.read_text() == "[]"
    assert (paths.bridge_state_dir / "bridge-state.json").read_text() == "{}"
    assert paths.bridge_log_path.read_text() == "old bridge\n"
    assert paths.daemon_start_log_path.read_text() == "started\n"
    assert (paths.ui_logs_dir / "pane-7.log").read_text() == "ui\n"
    assert paths.config_path.read_text() == "[regie]\n"  # what people edit stays on top
    assert sorted(p.name for p in old.iterdir()) == ["config.toml", "var"]
    assert sorted(p.name for p in theater_logs.iterdir()) == ["daemon"]
    assert not migrate_flat_layout(paths, tmp_path / "daemon.sock")  # once only
