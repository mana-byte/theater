"""Fixtures for the MCP-server plugin tests."""

from __future__ import annotations

import pytest

from theater.mcp_plugins import registry


@pytest.fixture
def clean_mcp_registry():
    """Start and end each test with an empty plugin registry."""
    registry.MCP_SERVERS.clear()
    registry._PLUGINS.clear()
    registry._DIAGNOSTICS.clear()
    yield
    registry.MCP_SERVERS.clear()
    registry._PLUGINS.clear()
    registry._DIAGNOSTICS.clear()
