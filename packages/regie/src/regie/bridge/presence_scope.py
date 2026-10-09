"""Narrow a changed-pane set to terminals the provider could be tracking."""

from __future__ import annotations

from regie.tmux.command import TmuxError
from regie.tmux.identity import pane_inventory


async def reportable_terminals(
    changed: frozenset[str], *, provider_id: str, expected_server_identity: str
) -> list[str]:
    """Drop only panes tmux proves belong to another owner; vanished panes stay in scope."""
    snapshots = await pane_inventory()
    if snapshots and snapshots[0].server_identity != expected_server_identity:
        raise TmuxError("tmux server identity changed")
    foreign = {snapshot.pane_id for snapshot in snapshots if snapshot.provider_id != provider_id}
    return sorted(changed - foreign)
