from __future__ import annotations

import json

from theater.harness.builtin.plugins.opencode.constants import MCP_CATALOG_MAX_BYTES
from theater.harness.builtin.plugins.opencode.mcp import OpenCodeMcpCatalog


def test_catalog_uses_only_verified_tool_mappings(tmp_path) -> None:
    path = tmp_path / "mcp-catalog.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": ["sentry"],
                "tools": {"sentry_find_organizations": ["sentry", "find_organizations"]},
            }
        )
    )
    catalog = OpenCodeMcpCatalog(path)

    assert catalog.identity("sentry_find_organizations") == (
        "sentry",
        "find_organizations",
    )
    assert catalog.identity("sentry_unobserved_tool") is None
    assert catalog.identity("read") is None


def test_catalog_fails_open_and_always_recognizes_theater(tmp_path) -> None:
    path = tmp_path / "mcp-catalog.json"
    path.write_text("not-json")
    catalog = OpenCodeMcpCatalog(path)

    assert catalog.identity("external_call") is None
    assert catalog.identity("theater_send") == ("theater", "send")


def test_catalog_reloads_only_valid_atomic_replacements(tmp_path) -> None:
    path = tmp_path / "mcp-catalog.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": ["first"],
                "tools": {"first_call": ["first", "call"]},
            }
        )
    )
    catalog = OpenCodeMcpCatalog(path)

    assert catalog.identity("first_call") == ("first", "call")
    generation = catalog.generation
    replacement = tmp_path / "replacement.json"
    replacement.write_text("not-json")
    replacement.replace(path)

    assert catalog.identity("first_call") == ("first", "call")
    assert catalog.generation == generation

    replacement.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": ["second"],
                "tools": {"second_call": ["second", "call"]},
            }
        )
    )
    replacement.replace(path)

    assert catalog.identity("first_call") is None
    assert catalog.identity("second_call") == ("second", "call")
    assert catalog.generation == generation + 1


def test_catalog_rejects_oversized_or_unverified_snapshots(tmp_path) -> None:
    path = tmp_path / "mcp-catalog.json"
    path.write_bytes(b"x" * (MCP_CATALOG_MAX_BYTES + 1))
    catalog = OpenCodeMcpCatalog(path)

    assert catalog.identity("external_call") is None
    assert catalog.generation == 0

    replacement = tmp_path / "replacement.json"
    replacement.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": ["known"],
                "tools": {"unknown_call": ["unknown", "call"]},
            }
        )
    )
    replacement.replace(path)

    assert catalog.identity("unknown_call") is None
    assert catalog.generation == 0
