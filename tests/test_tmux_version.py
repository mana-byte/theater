"""Tests for the tmux version probe in theater.tmux.client.

The probe must never raise, must cache, and must distinguish "not yet probed"
from "probed, got None". The "at least" comparison treats a letter suffix
(``3.7a``) as ≥ the bare version (``3.7``).
"""

from __future__ import annotations

import subprocess
from functools import partial
from unittest.mock import MagicMock

import pytest

from tests.rig.tables import eq_row, is_row, run_rows
from theater.tmux import client


@pytest.fixture(autouse=True)
def _reset_cache():
    # Pin None (tmux-absent) so no test falls through to a real `tmux -V`.
    # Tests that need a specific version stub subprocess.run or override the
    # cache directly. See test_tmux_client.py for the same rationale.
    client._VERSION_CACHE[0] = None
    yield
    client.reset_version_cache()


def _stub_version_output(monkeypatch, stdout: str, returncode: int = 0):
    """Make subprocess.run return the given stdout for `tmux -V`."""
    # Reset to unprobed so tmux_version() actually calls the stubbed run.
    client.reset_version_cache()
    proc = MagicMock()
    proc.stdout = stdout
    proc.returncode = returncode
    proc.stderr = ""

    def fake_run(cmd, **kw):
        assert cmd[0] == "tmux" and cmd[1] == "-V", f"unexpected call: {cmd}"
        return proc

    monkeypatch.setattr(subprocess, "run", fake_run)


class TestTmuxVersion:
    def test_parses_ordinary_probe_outputs(self, monkeypatch):
        """Ordinary probe outputs parse to their version; junk reports None."""

        def probes(stdout: str, expected: str | None) -> None:
            _stub_version_output(monkeypatch, stdout)
            assert client.tmux_version() == expected

        run_rows(
            (stdout.strip(), partial(probes, stdout, expected))
            for stdout, expected in [
                ("tmux 3.4\n", "3.4"),
                ("tmux 3.7\n", "3.7"),
                ("tmux 3.7a\n", "3.7a"),
                ("tmux next-3.8\n", "next-3.8"),
                ("garbage without prefix\n", None),
            ]
        )

    def test_returns_none_when_tmux_absent(self, monkeypatch):
        monkeypatch.setattr(client, "available", lambda: False)
        assert client.tmux_version() is None

    def test_never_raises_on_subprocess_failure(self, monkeypatch):
        def boom(*a, **kw):
            raise OSError("boom")

        monkeypatch.setattr(client, "available", lambda: True)
        monkeypatch.setattr(subprocess, "run", boom)
        client.reset_version_cache()
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
        client.reset_version_cache()

        assert client.tmux_version() == "3.4"
        assert client.tmux_version() == "3.4"
        assert call_count == 1

    def test_cache_distinguishes_unprobed_from_none(self, monkeypatch):
        # Explicitly reset to unprobed — the autouse fixture pins None, but
        # this test checks the sentinel before probing.
        client.reset_version_cache()
        # Before probing, the cache sentinel is not None.
        assert client._VERSION_CACHE[0] is client._UNPROBED
        monkeypatch.setattr(client, "available", lambda: False)
        assert client.tmux_version() is None
        # After probing None, the cache is None, not the sentinel.
        assert client._VERSION_CACHE[0] is None


class TestTmuxAtLeast:
    def test_ordinary_at_least_cells(self, monkeypatch):
        """Letter suffixes are patch releases, so 3.7a ≥ 3.7; junk is not."""

        def checks(stdout: str, expected: bool) -> None:
            _stub_version_output(monkeypatch, stdout)
            assert client.tmux_at_least(3, 7) == expected

        run_rows(
            (stdout.strip(), partial(checks, stdout, expected))
            for stdout, expected in [
                ("tmux 3.7\n", True),
                ("tmux 3.7a\n", True),
                ("tmux 3.7b\n", True),
                ("tmux 3.4\n", False),
                ("tmux 3.8\n", True),
                ("tmux next-3.8\n", True),
                ("garbage\n", False),
            ]
        )

    def test_returns_false_when_tmux_absent(self, monkeypatch):
        monkeypatch.setattr(client, "available", lambda: False)
        assert not client.tmux_at_least(3, 7)


class TestParseVersionTuple:
    def test_parses_numeric_components(self):
        """Version strings reduce to their numeric component tuples."""
        run_rows(
            [
                eq_row("3.4", lambda: client._parse_version_tuple("3.4"), (3, 4)),
                eq_row("3.7", lambda: client._parse_version_tuple("3.7"), (3, 7)),
                eq_row("3.7a", lambda: client._parse_version_tuple("3.7a"), (3, 7)),
                eq_row("3.7b", lambda: client._parse_version_tuple("3.7b"), (3, 7)),
                eq_row("3.10", lambda: client._parse_version_tuple("3.10"), (3, 10)),
                eq_row("4", lambda: client._parse_version_tuple("4"), (4,)),
                eq_row("next-3.8", lambda: client._parse_version_tuple("next-3.8"), (3, 8)),
                eq_row("1.2.3", lambda: client._parse_version_tuple("1.2.3"), (1, 2, 3)),
                eq_row("3.7.1", lambda: client._parse_version_tuple("3.7.1"), (3, 7, 1)),
            ]
        )

    def test_returns_none_for_non_numeric(self):
        """Bare names, junk and the empty string report no version."""
        run_rows(
            [
                is_row("master", lambda: client._parse_version_tuple("master"), None),
                is_row("garbage", lambda: client._parse_version_tuple("garbage"), None),
                is_row("empty", lambda: client._parse_version_tuple(""), None),
            ]
        )


class TestTmuxAtLeastBareAndThreeComponent:
    def test_bare_3_is_at_least_3_0(self, monkeypatch):
        _stub_version_output(monkeypatch, "tmux 3\n")
        assert client.tmux_at_least(3, 0)

    def test_bare_3_is_at_least_3(self, monkeypatch):
        _stub_version_output(monkeypatch, "tmux 3\n")
        assert client.tmux_at_least(3)

    def test_bare_3_is_not_at_least_3_1(self, monkeypatch):
        _stub_version_output(monkeypatch, "tmux 3\n")
        assert not client.tmux_at_least(3, 1)

    def test_three_component_is_at_least(self, monkeypatch):
        _stub_version_output(monkeypatch, "tmux 1.2.3\n")
        assert client.tmux_at_least(1, 2)

    def test_three_component_is_not_at_least(self, monkeypatch):
        _stub_version_output(monkeypatch, "tmux 1.2.3\n")
        assert not client.tmux_at_least(1, 3)
