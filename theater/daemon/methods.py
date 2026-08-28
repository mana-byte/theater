"""Compatibility façade for the daemon RPC handlers.

All handlers now live in ``theater.daemon.rpc.*``.  This module re-exports the
public surface (``METHODS``, ``MAX_AWAIT``, ``SEND_CLAIM_TTL``, and the private
helpers tests import directly) so existing ``from theater.daemon.methods import
X`` calls continue to work.  It does not re-import timing seams or external
modules — tests that monkeypatch those must patch the owning rpc submodule.

Importing this module has the side effect of registering every RPC handler,
via the ``rpc`` package's ``__init__``.
"""

from __future__ import annotations

from theater.daemon.rpc import METHODS
from theater.daemon.rpc.jobs import (
    _JOB_ERROR_MESSAGES,
    AWAIT_ANNOUNCE_AFTER,
    MAX_AWAIT,
    _await_announced,
    _close_await,
    _open_await,
)
from theater.daemon.rpc.params import (
    _JSON_REPLY_INSTRUCTION,
    _optional_string_param,
    _prompt_with_response_format,
    _reject_response_format_resume,
    _require,
    _serialized_response_format,
    _string_param,
    _validate_worktree_param,
)
from theater.daemon.rpc.participants import _resume_state
from theater.daemon.rpc.recall import _attach_parent_names
from theater.daemon.rpc.router import Handler, method
from theater.daemon.rpc.sending import (
    SEND_CLAIM_TTL,
    _check_approval_modal,
    _check_pane_identity,
    _check_transcript_send_preflight,
    _refuse_send,
    _transcript_identity_lost,
)
from theater.daemon.rpc.transcripts import (
    _READABLE,
    TRANSCRIPT_RECEIPT_BUS_KIND,
    TRANSCRIPT_RECEIPT_RPC,
    _candidate_owner,
    _candidate_to_dict,
    _read_transcript,
    _reject_cross_participant_receipt,
    _reject_unbound_same_cwd_receipt,
    _transcript_bind,
    _transcript_candidates,
    _transcript_receipt,
)
from theater.daemon.rpc.usage import _calendar_period_since, _retention_floor

__all__ = [
    "AWAIT_ANNOUNCE_AFTER",
    "MAX_AWAIT",
    "METHODS",
    "SEND_CLAIM_TTL",
    "TRANSCRIPT_RECEIPT_BUS_KIND",
    "TRANSCRIPT_RECEIPT_RPC",
    "_JOB_ERROR_MESSAGES",
    "_JSON_REPLY_INSTRUCTION",
    "_READABLE",
    "Handler",
    "_attach_parent_names",
    "_await_announced",
    "_calendar_period_since",
    "_candidate_owner",
    "_candidate_to_dict",
    "_check_approval_modal",
    "_check_pane_identity",
    "_check_transcript_send_preflight",
    "_close_await",
    "_open_await",
    "_optional_string_param",
    "_prompt_with_response_format",
    "_read_transcript",
    "_refuse_send",
    "_reject_cross_participant_receipt",
    "_reject_response_format_resume",
    "_reject_unbound_same_cwd_receipt",
    "_require",
    "_resume_state",
    "_retention_floor",
    "_serialized_response_format",
    "_string_param",
    "_transcript_bind",
    "_transcript_candidates",
    "_transcript_identity_lost",
    "_transcript_receipt",
    "_validate_worktree_param",
    "method",
]
