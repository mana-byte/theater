"""The OpenCode plugin's stdout endpoint parser (plugin-owned discovery half)."""

from __future__ import annotations

import pytest

from theater.harness.builtin.plugins.opencode.server_discovery import (
    parse_server_stdout_endpoint,
)


def test_banner_yields_loopback_origin() -> None:
    assert (
        parse_server_stdout_endpoint("opencode server listening on http://127.0.0.1:4096\n")
        == "http://127.0.0.1:4096"
    )


def test_non_banner_lines_are_silence() -> None:
    assert parse_server_stdout_endpoint("some other log line") is None
    assert parse_server_stdout_endpoint("") is None


@pytest.mark.parametrize(
    "line",
    [
        "opencode server listening on ",
        "opencode server listening on http://localhost:1",
        "opencode server listening on http://0.0.0.0:1",
        "opencode server listening on https://127.0.0.1:1",
        "opencode server listening on http://127.0.0.1:1/p",
        "opencode server listening on http://127.0.0.1:notaport",
    ],
)
def test_malformed_announcements_fail_closed(line: str) -> None:
    with pytest.raises(ValueError):
        parse_server_stdout_endpoint(line)
