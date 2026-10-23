"""Private send RPC and its retained clock/TTL compatibility seam."""

from __future__ import annotations

from theater.constants.daemon import SEND_CLAIM_TTL_SECONDS as SEND_CLAIM_TTL  # noqa: F401
from theater.daemon.controls import preflight, send_request
from theater.daemon.rpc.router import method
from theater.models import now  # noqa: F401 — composed clock compatibility seam

_transcript_identity_lost = preflight._transcript_identity_lost
_transcript_send_failure = preflight.transcript_send_failure
_check_transcript_send_preflight = preflight.check_transcript_send_preflight
_refuse_send = send_request._refuse_send
_check_pane_identity = send_request._check_pane_identity
_check_approval_modal = send_request._check_approval_modal
_send_after = send_request._send_after


def _send_claim_ttl() -> float:
    from theater.daemon import methods as _facade

    return _facade.SEND_CLAIM_TTL


@method("send")
async def _send(daemon, params: dict) -> dict:
    return await send_request.send(daemon, params)
