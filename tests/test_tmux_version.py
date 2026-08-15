"""Tests for the tmux version probe in theater.tmux.client.

The probe must never raise, must cache, and must distinguish "not yet probed"
from "probed, got None". The "at least" comparison treats a letter suffix
(``3.7a``) as ≥ the bare version (``3.7``).
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from theater.tmux import client


@pytest.fixture(autouse=True)
def _reset_cache():
    client.reset_version_cache()
    yield
    client.reset_version_cache()


def _stub_version_output(monkeypatch, stdout: str, returncode: int = 0):
    """Make subprocess.run return the given stdout for `tmux -V`."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.returncode = returncode
    proc.stderr = ""

    def fake_run(cmd, **kw):
        assert cmd[0] == "tmux" and cmd[1] == "-V", f"unexpected call: {cmd}"
        return proc

    monkeypatch.setattr(subprocess, "run", fake_run)


class TestTmuxVersion:
    def test_parses_3_4(self, monkeypatch):
        _stub_version_output(monkeypatch, "tmux 3.4\n")
        assert client.tmux_version() == "3.4"

    def test_parses_3_7(self, monkeypatch):
        _stub_version_output(monkeypatch, "tmux 3.7\n")
        assert client.tmux_version() == "3.7"

    def test_parses_3_7a(self, monkeypatch):
        _stub_version_output(monkeypatch, "tmux 3.7a\n")
        assert client.tmux_version() == "3.7a"

    def test_parses_next_3_8(self, monkeypatch):
        _stub_version_output(monkeypatch, "tmux next-3.8\n")
        assert client.tmux_version() == "next-3.8"

    def test_returns_none_for_garbage(self, monkeypatch):
        _stub_version_output(monkeypatch, "garbage without prefix\n")
        assert client.tmux_version() is None

    def test_returns_none_when_tmux_absent(self, monkeypatch):
        monkeypatch.setattr(client, "available", lambda: False)
        assert client.tmux_version() is None

    def test_never_raises_on_subprocess_failure(self, monkeypatch):
        def boom(*a, **kw):
            raise OSError("boom")

        monkeypatch.setattr(client, "available", lambda: True)
        monkeypatch.setattr(subprocess, "run", boom)
        assert client.tmux_version() is None

    def test_caches_after_first_call(self, monkeypatch):
        call_count = 0

        def fake_run(cmd, **kw):
            nonlocal call_count
            call_count += 1
            proc = MagicMock()
            proc.stdout = "tmux 3.4\n"
            proc.returncode = 0
            proc.stderr = ""
            return proc

        monkeypatch.setattr(client, "available", lambda: True)
        monkeypatch.setattr(subprocess, "run", fake_run)

        assert client.tmux_version() == "3.4"
        assert client.tmux_version() == "3.4"
        assert call_count == 1

    def test_cache_distinguishes_unprobed_from_none(self, monkeypatch):
        # Before probing, the cache sentinel is not None.
        assert client._VERSION_CACHE[0] is client._UNPROBED
        monkeypatch.setattr(client, "available", lambda: False)
        assert client.tmux_version() is None
        # After probing None, the cache is None, not the sentinel.
        assert client._VERSION_CACHE[0] is None


class TestTmuxAtLeast:
    def test_3_7_is_at_least_3_7(self, monkeypatch):
        _stub_version_output(monkeypatch, "tmux 3.7\n")
        assert client.tmux_at_least(3, 7)

    def test_3_7a_is_at_least_3_7(self, monkeypatch):
        """A letter suffix is a patch release, so 3.7a ≥ 3.7."""
        _stub_version_output(monkeypatch, "tmux 3.7a\n")
        assert client.tmux_at_least(3, 7)

    def test_3_7b_is_at_least_3_7(self, monkeypatch):
        _stub_version_output(monkeypatch, "tmux 3.7b\n")
        assert client.tmux_at_least(3, 7)

    def test_3_4_is_not_at_least_3_7(self, monkeypatch):
        _stub_version_output(monkeypatch, "tmux 3.4\n")
        assert not client.tmux_at_least(3, 7)

    def test_3_8_is_at_least_3_7(self, monkeypatch):
        _stub_version_output(monkeypatch, "tmux 3.8\n")
        assert client.tmux_at_least(3, 7)

    def test_next_3_8_is_at_least_3_7(self, monkeypatch):
        _stub_version_output(monkeypatch, "tmux next-3.8\n")
        assert client.tmux_at_least(3, 7)

    def test_returns_false_when_tmux_absent(self, monkeypatch):
        monkeypatch.setattr(client, "available", lambda: False)
        assert not client.tmux_at_least(3, 7)

    def test_returns_false_for_garbage_version(self, monkeypatch):
        _stub_version_output(monkeypatch, "garbage\n")
        assert not client.tmux_at_least(3, 7)


class TestParseVersionTuple:
    @pytest.mark.parametrize(
        "version, expected",
        [
            ("3.4", (3, 4)),
            ("3.7", (3, 7)),
            ("3.7a", (3, 7)),
            ("3.7b", (3, 7)),
            ("3.10", (3, 10)),
            ("4", (4,)),
            ("next-3.8", (3, 8)),
        ],
    )
    def test_parses_numeric_components(self, version, expected):
        assert client._parse_version_tuple(version) == expected

    @pytest.mark.parametrize("version", ["master", "garbage", ""])
    def test_returns_none_for_non_numeric(self, version):
        assert client._parse_version_tuple(version) is None
