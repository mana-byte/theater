"""Fail-closed approval enforcement over the 2.x HTTP API.

Core swallows a plugin setup/import failure and keeps running, so enforcement is
proven, not assumed: the exact generated plugin is verified active on the very
server that will run the session. Session and agent rules stay native.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence

from theater.constants.harness import HARNESS_APPROVAL_POLICIES
from theater.models import BadRequest

from .constants import (
    _APPROVAL_RULES_V2,
    PLUGIN_ACTIVE_POLL_SECONDS,
    PLUGIN_ACTIVE_TIMEOUT_SECONDS,
)
from .http_v2 import OpenCodeV2Client


class PluginNotActive(RuntimeError):
    """The generated Theater plugin is not proven active on the serving instance."""

    def __init__(self, state: str, detail: str) -> None:
        self.state = state
        super().__init__(detail)


def approval_ruleset(approval: str) -> tuple[dict[str, str], ...]:
    """The rules the plugin's evaluate hook applies; empty for yolo."""
    if approval not in HARNESS_APPROVAL_POLICIES:
        raise BadRequest(
            f"unknown approval {approval!r}; expected one of {', '.join(HARNESS_APPROVAL_POLICIES)}"
        )
    return _APPROVAL_RULES_V2.get(approval, ())


def classify_plugin_state(
    entries: Sequence[Mapping[str, object]], *, plugin_id: str, source_path: str
) -> None:
    """Refuse unless exactly the generated plugin, at its exact path, is active.

    Native reports the resolved `server.js` as the local source path, so the
    comparison is over realpaths; missing/failed/ambiguous all refuse.
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
    if (
        not isinstance(source, Mapping)
        or source.get("type") != "local"
        or not isinstance(reported, str)
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
        except PluginNotActive as exc:
            if exc.state != "missing" or loop.time() >= deadline:
                raise
            await asyncio.sleep(interval)
        else:
            return


__all__ = [
    "PluginNotActive",
    "approval_ruleset",
    "classify_plugin_state",
    "require_plugin_active",
]
