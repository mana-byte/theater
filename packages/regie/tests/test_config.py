from pathlib import Path

import pytest
from regie.config import SettingsError, load_settings


def test_load_settings_reads_only_the_regie_table(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[theater]
favourite = "ignored"

[regie]
theme = "ansi-dark"
tree_interval = 0.5
participant_detail = "description"
dashboard_sentences = ["one", "two"]
"""
    )

    settings = load_settings(path)

    assert settings.theme == "ansi-dark"
    assert settings.tree_interval == 0.5
    assert settings.participant_detail == "description"
    assert settings.dashboard_sentences == ["one", "two"]
    assert settings.sidebar_width == 52


def test_load_settings_retains_validation_and_defaults(tmp_path: Path) -> None:
    missing = load_settings(tmp_path / "missing.toml")
    invalid = tmp_path / "invalid.toml"
    invalid.write_text("[regie]\nsidebar_width = 12\n")

    assert missing.bus_batch == 50
    with pytest.raises(SettingsError, match="sidebar_width"):
        load_settings(invalid)
