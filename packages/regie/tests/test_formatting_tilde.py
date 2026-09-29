from __future__ import annotations

from pathlib import Path

from regie.formatting import tilde


def test_tilde_only_abbreviates_paths_inside_home(monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: Path("/Users/bob"))

    assert tilde("/Users/bob") == "~"
    assert tilde("/Users/bob/repo") == "~/repo"
    assert tilde("/Users/bob2/repo") == "/Users/bob2/repo"
    assert tilde(None) == "-"
