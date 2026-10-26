"""Private send RPC and its retained clock/TTL compatibility seam."""

from __future__ import annotations

from theater.constants.daemon import SEND_CLAIM_TTL_SECONDS as SEND_CLAIM_TTL  # noqa: F401
from theater.daemon.controls import send_request
from theater.daemon.rpc.router import method
from theater.models import now  # noqa: F401 — composed clock compatibility seam


def _send_claim_ttl() -> float:
    from theater.daemon import methods as _facade

    return _facade.SEND_CLAIM_TTL


@method("send")
async def _send(daemon, params: dict) -> dict:
    return await send_request.send(daemon, params)
