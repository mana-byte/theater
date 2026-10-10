from __future__ import annotations

from pathlib import Path

from regie.formatting import tilde


def test_tilde_only_abbreviates_paths_inside_home(monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: Path("/Users/bob"))

    assert tilde("/Users/bob") == "~"
    assert tilde("/Users/bob/repo") == "~/repo"
    assert tilde("/Users/bob2/repo") == "/Users/bob2/repo"
    assert tilde(None) == "-"


def test_read_at_is_omitted_from_event_display():
    from regie.formatting import event_summary

    assert event_summary({"ts": None, "index": 4, "observed_at": 1.0, "read_at": 2.0}) == ""
