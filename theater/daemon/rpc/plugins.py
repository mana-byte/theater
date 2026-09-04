"""Authenticated MCP-plugin gateway RPC."""

from __future__ import annotations

from theater.daemon.plugins.dispatch import authenticate, dispatch
from theater.daemon.rpc.router import method
from theater.models import BadRequest


@method("plugin.call")
async def _plugin_call(daemon, params: dict):
    """Run one explicitly supported sidecar operation as its credential owner."""
    if not isinstance(params, dict):
        raise BadRequest("plugin.call parameters must be a JSON object")
    record = authenticate(daemon, params.get("credential"))
    return await dispatch(
        daemon,
        record,
        params.get("operation"),
        params.get("params", {}),
    )


__all__ = []
