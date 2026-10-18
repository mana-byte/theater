"""Fixtures for the MCP surface tests."""

from __future__ import annotations

import pytest

from theater.daemon.server import Daemon


@pytest.fixture
async def mcp_daemon(theater_home):
    d = Daemon()
    await d.start()
    yield d
    await d.aclose()
