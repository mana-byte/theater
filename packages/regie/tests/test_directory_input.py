from __future__ import annotations

import os
from pathlib import Path

import pytest
from regie.widgets.directory_input import directory_suggestion, normalize_directory


def test_directory_completion_uses_common_prefix_then_the_unique_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpine").mkdir()
    (tmp_path / "almanac.txt").write_text("not a directory")

    assert directory_suggestion("al", base_dir=tmp_path) == "alp"
    assert directory_suggestion("alph", base_dir=tmp_path) == f"alpha{os.sep}"


def test_directory_normalization_supports_relative_tilde_and_rejects_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project with spaces"
    project.mkdir()
    file_path = tmp_path / "file.txt"
    file_path.write_text("not a directory")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert normalize_directory("project with spaces", base_dir=tmp_path) == str(project)
    assert normalize_directory("~/project with spaces", base_dir=Path("/")) == str(project)
    with pytest.raises(ValueError, match="not an existing directory"):
        normalize_directory(str(file_path), base_dir=tmp_path)
