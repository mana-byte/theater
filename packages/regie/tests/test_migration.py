"""An old flat Régie home moves itself into var/{state,run,logs}, Theater's tree left alone."""

from __future__ import annotations

from pathlib import Path

from regie.migration import migrate_flat_layout
from regie.paths import RegiePaths


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
