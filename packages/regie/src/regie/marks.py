"""What a participant's row says about the operations aimed at it: spawn progress, send outcome.

Derived only from the public operations in a state projection, so the tree draws what the
daemon reports (a phase, a state, an error code) and never guesses a delivery.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from enum import IntEnum, StrEnum

from regie.ui_constants import (
    REGIE_MARK_ACTIVE_MAX_SECONDS,
    REGIE_MARK_OUTCOME_MAX_SECONDS,
    REGIE_MARK_SENT_SECONDS,
)
from theater.frontend import ErrorCode, Operation
from theater.frontend.dto import OperationState


class MarkKind(StrEnum):
    LAUNCHING = "launching"
    TERMINATING = "terminating"
    QUEUED = "queued"
    SENT = "sent"
    BLOCKED = "blocked"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class _Rank(IntEnum):
    """Which mark wins when several operations aim at one participant."""

    SENT = 0
    QUEUED = 1
    LAUNCHING = 2
    TERMINATING = 3
    OUTCOME = 4  # blocked, failed, uncertain: the human has to look


@dataclass(frozen=True, slots=True)
class RowMark:
    kind: MarkKind
    operation_id: str
    glyph: str
    style: str
    #: Replaces the row's third line while the mark shows.
    detail: str | None = None
    #: Wall-clock second the mark fades by itself; None until the human has seen it.
    expires_at: float | None = None

    @property
    def replaces_status(self) -> bool:
        """Whether the mark stands in for the row's status glyph, not after its name."""
        return self.kind in {MarkKind.LAUNCHING, MarkKind.TERMINATING}

    @property
    def needs_seen(self) -> bool:
        """Whether this mark stays until the human has looked at the row."""
        return self.attention

    @property
    def attention(self) -> bool:
        """Whether the human is wanted: something blocked, broke or may not have arrived."""
        return self.kind in {MarkKind.BLOCKED, MarkKind.FAILED, MarkKind.UNCERTAIN}


#: The spawn phases the daemon publishes, in order; each maps to one step of a five-step ring.
_LAUNCH_STEPS = {
    "reserved": 0,
    "launch_reserved": 0,
    "launch_preparing": 1,
    "plan_ready": 1,
    "workspace_ready": 2,
    "terminal_create_pending": 3,
    "terminal_create_dispatched": 3,
    "terminal_bound": 4,
}
_LAUNCH_GLYPHS = "○◔◑◕●"
_LAUNCH_LABELS = ("reserved", "preparing", "workspace ready", "starting terminal", "started")

_SEND_KINDS = frozenset(
    {"controls.send", "controls.steer", "controls.queue_followup", "controls.interrupt"}
)

#: Refusals that say "not now" rather than "broken": the human can simply try again.
_BLOCKED = {
    ErrorCode.HUMAN_PRESENT: "human present",
    ErrorCode.BUSY: "busy",
    ErrorCode.PROVIDER_BUSY: "provider busy",
    ErrorCode.AWAITING_DECISION: "awaiting a decision",
    ErrorCode.NOT_ADDRESSABLE: "not addressable",
}

_IN_FLIGHT = {OperationState.ACCEPTED.value, OperationState.RUNNING.value}


def _stamp(operation: Operation) -> float:
    return operation.updated_at or operation.created_at or 0.0


def _launch_mark(operation: Operation, now: float) -> RowMark | None:
    state = operation.state
    if state in _IN_FLIGHT:
        if now - _stamp(operation) > REGIE_MARK_ACTIVE_MAX_SECONDS:
            return None
        step = _LAUNCH_STEPS.get(operation.phase, 1)
        return RowMark(
            MarkKind.LAUNCHING,
            operation.operation_id,
            _LAUNCH_GLYPHS[step],
            "$primary",
            _LAUNCH_LABELS[step],
        )
    return _outcome_mark(operation, now, subject="launch")


def _terminate_mark(operation: Operation, now: float) -> RowMark | None:
    """A kill in flight, then its failure or doubt; a verified exit takes the row with it."""
    if operation.state in _IN_FLIGHT:
        if now - _stamp(operation) > REGIE_MARK_ACTIVE_MAX_SECONDS:
            return None
        return RowMark(MarkKind.TERMINATING, operation.operation_id, "✕", "$error", "terminating")
    return _outcome_mark(operation, now, subject="termination")


def _send_mark(operation: Operation, now: float) -> RowMark | None:
    state = operation.state
    if state in _IN_FLIGHT:
        if now - _stamp(operation) > REGIE_MARK_ACTIVE_MAX_SECONDS:
            return None
        return RowMark(MarkKind.QUEUED, operation.operation_id, "⋯", "$text-muted")
    if state == OperationState.SUCCEEDED.value:
        if operation.phase == "already_idle":
            return None
        expires_at = _stamp(operation) + REGIE_MARK_SENT_SECONDS
        if now >= expires_at:
            return None
        return RowMark(MarkKind.SENT, operation.operation_id, "✓", "$success", None, expires_at)
    return _outcome_mark(operation, now, subject="delivery")


def _outcome_mark(operation: Operation, now: float, *, subject: str) -> RowMark | None:
    """A failed or uncertain operation, until the human has seen it or it grows stale."""
    if now - _stamp(operation) > REGIE_MARK_OUTCOME_MAX_SECONDS:
        return None
    error = operation.error
    if operation.state == OperationState.UNCERTAIN.value:
        return RowMark(
            MarkKind.UNCERTAIN,
            operation.operation_id,
            "?",
            "$warning",
            f"uncertain · {subject} unconfirmed",
        )
    if operation.state != OperationState.FAILED.value:
        return None
    code = error.known_code if error is not None else None
    if code in _BLOCKED:
        return RowMark(
            MarkKind.BLOCKED,
            operation.operation_id,
            "⊘",
            "$warning",
            f"blocked · {_BLOCKED[code]}",
        )
    reason = error.code if error is not None else "unknown"
    return RowMark(
        MarkKind.FAILED, operation.operation_id, "✗", "$error", f"failed · {subject} {reason}"
    )


def _rank(mark: RowMark) -> _Rank:
    if mark.attention:
        return _Rank.OUTCOME
    return {
        MarkKind.LAUNCHING: _Rank.LAUNCHING,
        MarkKind.TERMINATING: _Rank.TERMINATING,
        MarkKind.QUEUED: _Rank.QUEUED,
        MarkKind.SENT: _Rank.SENT,
    }[mark.kind]


def row_marks(
    operations: Iterable[Operation],
    participants: Collection[str],
    *,
    now: float,
    seen: Collection[str] = (),
) -> Mapping[str, RowMark]:
    """The mark each participant row wears, by participant id.

    *seen* holds operation ids the human has looked at: their failures stop asking for attention.
    """
    chosen: dict[str, tuple[tuple[_Rank, float], RowMark]] = {}
    for operation in operations:
        if operation.kind == "spawn":
            mark = _launch_mark(operation, now)
        elif operation.kind == "participants.terminate":
            mark = _terminate_mark(operation, now)
        elif operation.kind in _SEND_KINDS:
            mark = _send_mark(operation, now)
        else:
            continue
        if mark is None or (mark.needs_seen and operation.operation_id in seen):
            continue
        order = (_rank(mark), _stamp(operation))
        for target in operation.target_ids:
            if target in participants and (target not in chosen or order > chosen[target][0]):
                chosen[target] = (order, mark)
    return {target: mark for target, (_, mark) in chosen.items()}


def next_expiry(marks: Iterable[RowMark]) -> float | None:
    """The soonest wall-clock second a mark fades by itself, if any will."""
    return min((mark.expires_at for mark in marks if mark.expires_at is not None), default=None)


__all__ = ["MarkKind", "RowMark", "next_expiry", "row_marks"]
