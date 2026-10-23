"""Launch-local OpenCode plugin generation."""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

from theater.harness.base import theater_binary

from ._assets import read_asset
from .constants import (
    MCP_CATALOG_MAX_BYTES,
    MCP_CATALOG_MAX_NON_MCP_TOOLS,
    MCP_CATALOG_MAX_SERVERS,
    MCP_CATALOG_MAX_TOOLS,
    MCP_CATALOG_NAME_MAX_BYTES,
    MCP_CATALOG_VERSION,
    RECEIPT_RETRY_DELAYS_MS,
)
from .mcp import catalog_path
from .plugin_receipt import load_receipt_path


def render_native_plugin(participant_id: str, token_path: Path, permission_rules=()) -> str:
    """Render the launch-local plugin.

    Session rules merge after the agent's own, with existing denies re-appended so approval never
    weakens them; empty (yolo) enforces nothing.
    """
    participant = json.dumps(participant_id)
    token = json.dumps(str(token_path))
    command = json.dumps(theater_binary())
    retry_delays = json.dumps(RECEIPT_RETRY_DELAYS_MS)
    mcp_catalog = json.dumps(str(catalog_path(participant_id)))
    load_proof = json.dumps(secrets.token_hex(16))
    load_receipt = json.dumps(str(load_receipt_path(participant_id)))
    rules = json.dumps([dict(rule) for rule in permission_rules])
    values = {
        "PARTICIPANT": participant,
        "TOKEN": token,
        "COMMAND": command,
        "RETRY_DELAYS": retry_delays,
        "MCP_CATALOG": mcp_catalog,
        "MCP_CATALOG_VERSION": str(MCP_CATALOG_VERSION),
        "MCP_CATALOG_MAX_BYTES": str(MCP_CATALOG_MAX_BYTES),
        "MCP_CATALOG_MAX_SERVERS": str(MCP_CATALOG_MAX_SERVERS),
        "MCP_CATALOG_MAX_TOOLS": str(MCP_CATALOG_MAX_TOOLS),
        "MCP_CATALOG_MAX_NON_MCP_TOOLS": str(MCP_CATALOG_MAX_NON_MCP_TOOLS),
        "MCP_CATALOG_NAME_MAX_BYTES": str(MCP_CATALOG_NAME_MAX_BYTES),
        "LOAD_PROOF": load_proof,
        "LOAD_RECEIPT": load_receipt,
        "RULES": rules,
        # The historical f-string emitted literal codepoints in this JS regex.
        "CONTROL_RANGE": "\u0000-\u001f\u007f-\u009f",
    }
    return re.sub(
        r"__THEATER_([A-Z_]+)__",
        lambda match: values[match.group(1)],
        read_asset("native_plugin.js"),
    )


__all__ = ["render_native_plugin"]
