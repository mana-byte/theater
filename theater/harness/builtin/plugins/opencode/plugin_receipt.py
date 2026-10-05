"""Fail-closed proof that the generated OpenCode 1.x plugin actually loaded.

OpenCode 1.x swallows a plugin load failure and keeps serving, so manual/edits
cannot be assumed: the plugin writes a launch-pinned receipt from its `config`
hook, and a session is refused unless that exact launch's receipt is on disk.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from theater import paths

from .constants import (
    ENFORCED_APPROVALS,
    PLUGIN_LOAD_RECEIPT_FILENAME,
    PLUGIN_RECEIPT_POLL_SECONDS,
    PLUGIN_RECEIPT_TIMEOUT_SECONDS,
)
from .mcp import plugin_path

_RECEIPT_MAX_BYTES = 4096
_PLUGIN_MAX_BYTES = 256 * 1024
#: Rendered by ``render_native_plugin``; pinning the receipt to one plugin
#: build defeats a stale receipt left behind by an earlier backend generation.
_PROOF_RE = re.compile(rb'const loadProof = "([0-9a-f]{8,128})"')


class PluginNotLoaded(RuntimeError):
    """The generated Theater plugin did not prove it loaded on this launch."""


def load_receipt_path(participant_id: str) -> Path:
    """The Theater-owned artifact the plugin's config hook writes."""
    return (
        paths.participant_observation_dir(participant_id, "opencode") / PLUGIN_LOAD_RECEIPT_FILENAME
    )


def approval_requires_plugin(approval: str | None) -> bool:
    """Only manual/edits ride on the plugin; yolo enforces nothing."""
    return approval in ENFORCED_APPROVALS


def _expected_proof(participant_id: str) -> str | None:
    """The nonce baked into the plugin this launch rendered, or ``None``."""
    try:
        raw = plugin_path(paths.mcp_config_path(participant_id)).read_bytes()
    except OSError:
        return None
    if len(raw) > _PLUGIN_MAX_BYTES:
        return None
    match = _PROOF_RE.search(raw)
    return match.group(1).decode("ascii") if match else None


def _receipt_matches(participant_id: str, proof: str) -> bool:
    try:
        raw = load_receipt_path(participant_id).read_bytes()
    except OSError:
        return False
    if len(raw) > _RECEIPT_MAX_BYTES:
        return False
    try:
        document = json.loads(raw)
    except ValueError:
        return False
    return isinstance(document, dict) and document.get("proof") == proof


def _refusal(participant_id: str, detail: str) -> str:
    return (
        f"the OpenCode approval plugin for {participant_id!r} is not proven loaded ({detail}); "
        "OpenCode 1.x keeps serving with a broken plugin and a manual/edits session would run "
        "with the agent's own permissions, so Theater refuses to open one — inspect the "
        "backend's stderr log for the plugin import failure and relaunch, or spawn with yolo "
        "approval"
    )


async def require_plugin_loaded(participant_id: str) -> None:
    """Wait for this launch's load receipt; refuse when it never lands.

    The backend loads its config (and so the plugin) lazily — the session
    create that precedes this call triggers it — so a missing or stale receipt
    is polled, but an unreadable plugin build is refused immediately.
    """
    proof = _expected_proof(participant_id)
    if proof is None:
        raise PluginNotLoaded(_refusal(participant_id, "the generated plugin build is unreadable"))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PLUGIN_RECEIPT_TIMEOUT_SECONDS
    while not _receipt_matches(participant_id, proof):
        if loop.time() >= deadline:
            raise PluginNotLoaded(_refusal(participant_id, "no load receipt appeared in time"))
        await asyncio.sleep(PLUGIN_RECEIPT_POLL_SECONDS)


__all__ = [
    "PluginNotLoaded",
    "approval_requires_plugin",
    "load_receipt_path",
    "require_plugin_loaded",
]
