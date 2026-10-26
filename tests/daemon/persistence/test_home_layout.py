"""Theater home layout: owned directories, permissions and Alembic bootstrap."""

from __future__ import annotations

import stat

import pytest
from alembic import command
from alembic.config import Config

from theater import paths
from theater.daemon.store import MIGRATIONS, Store


def test_runtime_owners_are_private_and_created_lazily(theater_home):
    managed = (
        paths.state_dir(),
        paths.run_dir(),
        paths.logs_dir(),
        paths.participants_dir(),
    )
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in managed)
    assert not paths.participant_dir("abcdef123456").exists()
    assert not paths.plugin_state_dir("alerts").exists()

    store = Store(paths.db_path())
    store.close()

    assert paths.db_path().parent == paths.state_dir()
    assert stat.S_IMODE(paths.db_path().stat().st_mode) == 0o600


def test_alembic_bootstraps_an_empty_home(tmp_path, monkeypatch):
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "fresh"))
    migration = Config()
    migration.set_main_option("script_location", str(MIGRATIONS))

    command.upgrade(migration, "head")

    assert paths.db_path().is_file()
    assert stat.S_IMODE(paths.db_path().stat().st_mode) == 0o600


@pytest.mark.parametrize("value", ("", ".", "..", "a/b", "a\\b", "/tmp/x", "C:\\x"))
def test_owned_path_helpers_reject_non_components(value):
    with pytest.raises(ValueError):
        paths.participant_dir(value)
    with pytest.raises(ValueError):
        paths.plugin_state_dir(value)
