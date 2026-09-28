"""Codex native pending-interaction tracking: exact requests and flag evidence."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping

from theater.harness.contracts.runtime import (
    NativeHumanInteraction,
    NativeInteractionKind,
    NativeRequestId,
    validate_native_request_id,
)

from ._runtime_host import CodexRuntimeHost
from .runtime_constants import (
    _APPROVAL_METHOD_SUFFIX,
    _CLARIFICATION_METHOD_MARKERS,
    _PENDING_INTERACTION_OVERFLOW_DETAILS,
    _REQUEST_USER_INPUT_METHOD,
    _WAITING_APPROVAL_FLAG_DETAILS,
    _WAITING_INPUT_FLAG_DETAILS,
    CODEX_RUNTIME_PENDING_INTERACTIONS_MAX,
)
from .runtime_messages import _bounded_str, _clarification_details

#: The only status variants whose waiting evidence this runtime can interpret.
_RECOGNIZED_STATUS_TYPES = frozenset({"active", "idle"})
#: Known flag names; an unknown entry silences the whole list.
_KNOWN_FLAGS = frozenset({"waitingOnApproval", "waitingOnUserInput"})

_MISSING = object()


def _waiting_flags_of(status: object) -> tuple[bool, bool] | None:
    """(approval, input) evidence, or None when the payload cannot speak.

    Unknown variants and malformed or unknown-entry flag lists are silence,
    never absence; a valid idle variant may omit ``activeFlags`` entirely.
    """
    if not isinstance(status, Mapping):
        return None
    status_type = status.get("type")
    if not isinstance(status_type, str) or status_type not in _RECOGNIZED_STATUS_TYPES:
        return None
    flags = status.get("activeFlags", _MISSING)
    if flags is _MISSING:
        return (False, False) if status.get("type") == "idle" else None
    if not isinstance(flags, (list, tuple)):
        return None
    if not all(isinstance(flag, str) and flag in _KNOWN_FLAGS for flag in flags):
        return None
    if status_type == "idle" and flags:
        return None
    return ("waitingOnApproval" in flags, "waitingOnUserInput" in flags)


class CodexRuntimeInteractions(CodexRuntimeHost):
    """One runtime's unresolved human interactions and waiting evidence."""

    _pending_interactions: OrderedDict[NativeRequestId, NativeHumanInteraction]
    _waiting_approval_flag: bool
    _waiting_input_flag: bool
    _overflow_kinds: frozenset[NativeInteractionKind]

    def _on_server_request_resolved(
        self, params: Mapping[str, object], _: NativeRequestId | None
    ) -> None:
        if not self._thread_filter(params):
            return
        request_id = params.get("requestId")
        if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
            return
        self._status_revision += 1
        if request_id in self._pending_interactions:
            del self._pending_interactions[request_id]
            # Clearing a pending interaction changes the readable status
            # snapshot (no more AWAITING_INPUT) without any event landing.
            self._notify_activity()

    def _record_server_request(
        self, method: str, params: Mapping[str, object], request_id: NativeRequestId
    ) -> None:
        """Observe one native server request; never send an answer."""
        if not self._thread_filter(params):
            return
        validate_native_request_id(request_id, "server request id")
        if method == _REQUEST_USER_INPUT_METHOD and params.get("isBlocking") is False:
            return
        if method.endswith(_APPROVAL_METHOD_SUFFIX):
            kind = NativeInteractionKind.APPROVAL
        elif any(marker in method for marker in _CLARIFICATION_METHOD_MARKERS):
            kind = NativeInteractionKind.CLARIFICATION
        else:
            self._diagnostic(f"observed unclassified server request {method}")
            return
        details = params.get("reason")
        if not isinstance(details, str) or not details:
            questions = params.get("questions")
            details = (
                _clarification_details(questions)
                if isinstance(questions, (list, tuple)) and questions
                else method
            )
        interaction = NativeHumanInteraction(
            kind=kind,
            native_request_id=request_id,
            native_turn_id=_bounded_str(params.get("turnId"), limit=512),
            native_item_id=_bounded_str(params.get("itemId"), limit=512),
            details=details[:240],
        )
        self._status_revision += 1
        if request_id in self._pending_interactions:
            # A replayed unresolved request is idempotent by exact id; only its
            # view of the details refreshes.
            self._pending_interactions[request_id] = interaction
        elif len(self._pending_interactions) >= CODEX_RUNTIME_PENDING_INTERACTIONS_MAX:
            # Unresolved evidence is never evicted into a "safe" snapshot; the
            # latch keeps the observed kind visible, never a fake exact id.
            self._overflow_kinds = self._overflow_kinds | {kind}
            self._degrade("unresolved native interaction bound exceeded; waiting stays reported")
        else:
            self._pending_interactions[request_id] = interaction
        # A recorded approval/clarification flips the readable status to
        # AWAITING_INPUT with no event or fact landing; wake observation.
        self._notify_activity()

    def _pending_interaction_view(self) -> NativeHumanInteraction | None:
        """The strongest unresolved interaction, honest about unknown identity.

        Approvals outrank clarifications; exact requests outrank equivalent
        summaries; summaries never carry invented ids.
        """
        approval = None
        clarification = None
        for interaction in self._pending_interactions.values():
            if interaction.kind is NativeInteractionKind.APPROVAL:
                if approval is None:
                    approval = interaction
            elif clarification is None:
                clarification = interaction
        if approval is not None:
            return approval
        if self._waiting_approval_flag:
            return self._flag_summary(
                NativeInteractionKind.APPROVAL, _WAITING_APPROVAL_FLAG_DETAILS
            )
        if NativeInteractionKind.APPROVAL in self._overflow_kinds:
            return self._flag_summary(
                NativeInteractionKind.APPROVAL, _PENDING_INTERACTION_OVERFLOW_DETAILS
            )
        if clarification is not None:
            return clarification
        if self._waiting_input_flag:
            return self._flag_summary(
                NativeInteractionKind.CLARIFICATION, _WAITING_INPUT_FLAG_DETAILS
            )
        if NativeInteractionKind.CLARIFICATION in self._overflow_kinds:
            return self._flag_summary(
                NativeInteractionKind.CLARIFICATION, _PENDING_INTERACTION_OVERFLOW_DETAILS
            )
        return None

    def _flag_summary(self, kind: NativeInteractionKind, details: str) -> NativeHumanInteraction:
        """A waiting indication whose exact request was never observed here."""
        return NativeHumanInteraction(
            kind=kind,
            native_request_id=None,
            native_turn_id=None,
            native_item_id=None,
            details=details,
        )

    def _adopt_waiting_flags(self, status: object) -> bool:
        """Adopt authoritative waiting evidence; return whether state changed.

        Only well-formed recognized evidence ever clears; any other payload
        is silence, never manufactured absence.
        """
        waiting = _waiting_flags_of(status)
        if waiting is None:
            return False
        approval, waiting_input = waiting
        clear_latch = not (approval or waiting_input)
        changed = (approval, waiting_input) != (
            self._waiting_approval_flag,
            self._waiting_input_flag,
        ) or (clear_latch and bool(self._overflow_kinds))
        if clear_latch:
            # Authoritative no-waiting evidence ends the overflow latch: the
            # untracked requests it guarded have resolved.
            self._overflow_kinds = frozenset()
        self._waiting_approval_flag = approval
        self._waiting_input_flag = waiting_input
        return changed

    def _drop_turn_interactions(self, turn_id: str) -> None:
        """Terminal turn lifecycle ends only that turn's recorded requests."""
        stale = [
            request_id
            for request_id, interaction in self._pending_interactions.items()
            if interaction.native_turn_id == turn_id
        ]
        if not stale:
            return
        for request_id in stale:
            del self._pending_interactions[request_id]
        self._notify_activity()
