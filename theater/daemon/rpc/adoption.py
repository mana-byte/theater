"""Participant adoption RPC: an agent takes a live sibling as its own child."""

from __future__ import annotations

from theater.daemon.adoption import AdoptionService
from theater.daemon.rpc.params import _require
from theater.daemon.rpc.participants import _with_presence
from theater.daemon.rpc.router import method
from theater.models import BadRequest


@method("participant.adopt")
async def _adopt(daemon, params: dict) -> dict:
    """Reparent a sibling under the caller and reown its controls to the caller."""
    caller_id = _require(params, "caller_id")
    if not isinstance(caller_id, str):
        raise BadRequest(
            "caller_id must be a participant id; there is no CLI adoption path — "
            "the caller is always the adopting agent itself"
        )
    target = await AdoptionService(daemon).adopt(_require(params, "id"), caller_id)
    return _with_presence(daemon, target.to_dict())
