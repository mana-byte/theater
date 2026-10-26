"""Compatibility façade re-exporting the RPC handlers from ``theater.daemon.rpc.*``.

Importing registers every handler; monkeypatch timing seams on the owning rpc submodule, not here.
"""

from __future__ import annotations

from theater.daemon.rpc import METHODS
from theater.daemon.rpc.hooks import HARNESS_EVENT_RPC, _harness_event
from theater.daemon.rpc.jobs import (
    _JOB_ERROR_MESSAGES,
    AWAIT_ANNOUNCE_AFTER,
    MAX_AWAIT,
)
from theater.daemon.rpc.params import (
    _optional_string_param,
    _prompt_with_response_format,
    _require,
    _string_param,
    _validate_worktree_param,
)
from theater.daemon.rpc.participants import _resume_state
from theater.daemon.rpc.recall import _attach_parent_names
from theater.daemon.rpc.router import Handler, method
from theater.daemon.rpc.sending import SEND_CLAIM_TTL
from theater.daemon.rpc.transcripts import (
    _read_transcript,
    _reject_cross_participant_receipt,
    _transcript_bind,
    _transcript_candidates,
    _transcript_receipt,
)

__all__ = [
    "AWAIT_ANNOUNCE_AFTER",
    "HARNESS_EVENT_RPC",
    "MAX_AWAIT",
    "METHODS",
    "SEND_CLAIM_TTL",
    "_JOB_ERROR_MESSAGES",
    "Handler",
    "_attach_parent_names",
    "_harness_event",
    "_optional_string_param",
    "_prompt_with_response_format",
    "_read_transcript",
    "_reject_cross_participant_receipt",
    "_require",
    "_resume_state",
    "_string_param",
    "_transcript_bind",
    "_transcript_candidates",
    "_transcript_receipt",
    "_validate_worktree_param",
    "method",
]
