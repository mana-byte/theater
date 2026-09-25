from pathlib import Path

import pytest
from regie.config import SettingsError, load_settings


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
