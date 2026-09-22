"""Prove a recorded terminal disappeared on its original tmux server."""

from __future__ import annotations

from collections.abc import Mapping

from regie.tmux.command import TmuxError
from regie.tmux.identity import current_server_identity, pane_inventory


async def missing_terminal_identity(
    expected: Mapping[str, object] | None,
    *,
    provider_id: str,
    generation: int,
    terminal_id: str,
    terminal_incarnation: str,
    server_identity: str,
) -> dict[str, object]:
    """An absent pane is exit evidence only on the recorded, still-live server."""
    occupant = None if expected is None else expected.get("occupant")
    if (
        expected is None
        or expected.get("provider_id") != provider_id
        or expected.get("provider_generation") != generation
        or expected.get("terminal_id") != terminal_id
        or expected.get("terminal_incarnation") != terminal_incarnation
        or not isinstance(occupant, Mapping)
        or occupant.get("provider_kind") != "tmux"
        or occupant.get("tmux_server_identity") != server_identity
        or occupant.get("terminal_incarnation") != terminal_incarnation
        or not isinstance(occupant.get("occupant_id"), str)
        or not occupant["occupant_id"]
    ):
        raise TmuxError("missing terminal has no matching recorded identity on this server")
    inventory = await pane_inventory()
    if (
        any(pane.server_identity != server_identity for pane in inventory)
        or await current_server_identity() != server_identity
    ):
        raise TmuxError("tmux server identity changed while verifying terminal exit")
    if any(pane.pane_id == terminal_id for pane in inventory):
        raise TmuxError("terminal still exists; missing-pane exit cannot be established")
    return dict(expected)
