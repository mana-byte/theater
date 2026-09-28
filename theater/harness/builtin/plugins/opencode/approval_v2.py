"""Fail-closed approval enforcement over the 2.x HTTP API.

OpenCode core swallows a plugin setup/import failure and keeps running
(core/src/plugin.ts), so enforcement must be proven, not assumed: the exact
generated plugin is verified active on the very server that will run the
session, and the session ruleset is written and read back. Session rules alone
do not protect a resumed session with a saved "always" allow — native merges
saved allows after session rules — so the plugin's `permission.evaluate`
hook stays registered to tighten those allows into asks.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence

from .constants import (
    PLUGIN_ACTIVE_POLL_SECONDS,
    PLUGIN_ACTIVE_TIMEOUT_SECONDS,
    _APPROVAL_RULES_V2,
)
from .http_v2 import OpenCodeV2Client


class PluginNotActive(RuntimeError):
    """The generated Theater plugin is not proven active on the serving instance."""

    def __init__(self, state: str, detail: str) -> None:
        self.state = state
        super().__init__(detail)


class PolicyNotEnforced(RuntimeError):
    """The session ruleset readback does not match the enforced policy."""


def approval_ruleset(approval: str) -> tuple[dict[str, str], ...]:
    """The 2.x rules enforcing one approval choice; empty for yolo (no enforcement)."""
    return _APPROVAL_RULES_V2.get(approval, ())


def protected_ruleset(
    existing: Sequence[Mapping[str, object]], rules: Sequence[Mapping[str, str]]
) -> list[dict[str, str]]:
    """Existing denies survive a policy overwrite; native denies win before any hook.

    Approval rules append after them: within a ruleset the last matching rule
    decides, and dedupe keeps a re-applied policy idempotent.
    """
    merged: list[dict[str, str]] = []
    for rule in (*existing, *rules):
        normalized = {
            "action": str(rule.get("action", "")),
            "resource": str(rule.get("resource", "")),
            "effect": str(rule.get("effect", "")),
        }
        if normalized not in merged:
            merged.append(normalized)
    return merged


def classify_plugin_state(
    entries: Sequence[Mapping[str, object]], *, plugin_id: str, source_path: str
) -> None:
    """Refuse unless exactly the generated plugin, at its exact path, is active.

    Native reports the resolved `server.js` as the local source path, so the
    comparison is over realpaths; a missing, failed, or ambiguous entry all
    refuse — never an assumption that the hook is registered.
    """
    matches = [entry for entry in entries if entry.get("id") == plugin_id]
    if not matches:
        raise PluginNotActive(
            "missing",
            f"plugin {plugin_id} is not loaded on this server; refusing to open a "
            "session whose approval policy cannot be enforced",
        )
    if len(matches) > 1:
        raise PluginNotActive(
            "ambiguous",
            f"plugin {plugin_id} is registered {len(matches)} times; refusing to open a "
            "session whose approval policy cannot be pinned to one instance",
        )
    entry = matches[0]
    state = entry.get("state")
    status = state.get("status") if isinstance(state, Mapping) else None
    if status != "active":
        error = state.get("error") if isinstance(state, Mapping) else None
        raise PluginNotActive(
            "failed",
            f"plugin {plugin_id} did not load on this server"
            + (f" ({error})" if isinstance(error, str) else "")
            + "; refusing to open a session whose approval policy cannot be enforced",
        )
    source = entry.get("source")
    reported = source.get("path") if isinstance(source, Mapping) else None
    if not isinstance(source, Mapping) or source.get("type") != "local" or not isinstance(
        reported, str
    ):
        raise PluginNotActive(
            "mismatch",
            f"plugin {plugin_id} is not the launch-local package; refusing to open a "
            "session whose approval policy cannot be enforced",
        )
    if os.path.realpath(reported) != os.path.realpath(source_path):
        raise PluginNotActive(
            "mismatch",
            f"plugin {plugin_id} loaded from {reported!r}, not the generated {source_path!r}; "
            "refusing to open a session whose approval policy cannot be enforced",
        )


async def require_plugin_active(
    client: OpenCodeV2Client,
    *,
    plugin_id: str,
    source_path: str,
    directory: str | None = None,
    timeout: float = PLUGIN_ACTIVE_TIMEOUT_SECONDS,
    interval: float = PLUGIN_ACTIVE_POLL_SECONDS,
) -> None:
    """Poll `GET /api/plugin` until the exact plugin is active or the deadline passes.

    A freshly booted location registers plugins asynchronously, so a missing
    entry is retried; a reported failure is terminal and refuses immediately.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            entries = await client.list_plugins(directory=directory)
            classify_plugin_state(entries, plugin_id=plugin_id, source_path=source_path)
            return
        except PluginNotActive as exc:
            if exc.state != "missing" or loop.time() >= deadline:
                raise
            await asyncio.sleep(interval)


async def apply_session_policy(
    client: OpenCodeV2Client, session_id: str, *, approval: str
) -> list[dict[str, str]] | None:
    """Write the approval ruleset onto the session and verify the stored readback.

    Yolo renders no rules and changes nothing. Any other approval preserves the
    session's existing denies, and a readback that does not match exactly is a
    refusal, not a hope.
    """
    rules = approval_ruleset(approval)
    if not rules:
        return None
    readback = await client.read_session(session_id)
    existing = readback.get("permissions")
    merged = protected_ruleset(existing if isinstance(existing, list) else [], rules)
    await client.update_session_permissions(session_id, merged)
    verified = await client.read_session(session_id)
    stored = verified.get("permissions")
    if stored != merged:
        raise PolicyNotEnforced(
            "the session permission readback does not match the enforced policy; refusing "
            "to open a session whose approval policy cannot be proven"
        )
    return merged


__all__ = [
    "PluginNotActive",
    "PolicyNotEnforced",
    "apply_session_policy",
    "approval_ruleset",
    "classify_plugin_state",
    "protected_ruleset",
    "require_plugin_active",
]
