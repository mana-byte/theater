"""What operations aimed at a participant make its row show."""

from __future__ import annotations

import pytest
from regie.marks import MarkKind, next_expiry, row_marks
from regie.ui_constants import (
    REGIE_MARK_ACTIVE_MAX_SECONDS,
    REGIE_MARK_OUTCOME_MAX_SECONDS,
    REGIE_MARK_SENT_SECONDS,
)

from packages.regie.tests.support.app_fakes import make_operation
from theater.frontend import Operation

NOW = 10_000.0
ALL = {"a", "b"}


def operation(
    kind: str, state: str, phase: str, *, age: float = 1.0, target: str = "a", **kw
) -> Operation:
    return make_operation(kind, state, phase, target=target, updated_at=NOW - age, **kw)


def marks(*operations: Operation, seen=()):
    return row_marks(operations, ALL, now=NOW, seen=seen)


# ---- a spawn ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("phase", "glyph", "label"),
    [
        ("launch_reserved", "○", "reserved"),
        ("launch_preparing", "◔", "preparing"),
        ("workspace_ready", "◑", "workspace ready"),
        ("terminal_create_dispatched", "◕", "starting terminal"),
        ("terminal_bound", "●", "started"),
    ],
)
def test_a_spawn_in_flight_fills_a_ring_one_published_phase_at_a_time(phase, glyph, label):
    mark = marks(operation("spawn", "running", phase))["a"]
    assert (mark.kind, mark.glyph, mark.detail) == (MarkKind.LAUNCHING, glyph, label)


def test_an_unknown_phase_still_reads_as_launching():
    assert marks(operation("spawn", "running", "something_new"))["a"].kind is MarkKind.LAUNCHING


def test_a_finished_spawn_leaves_the_row_alone():
    assert marks(operation("spawn", "succeeded", "terminal_bound")) == {}


def test_a_failed_spawn_wears_its_failure_until_seen():
    mark = marks(operation("spawn", "failed", "launch_refused", error_code="provider_unavailable"))
    assert mark["a"].kind is MarkKind.FAILED
    assert mark["a"].detail == "failed · launch provider_unavailable"
    assert marks(operation("spawn", "failed", "x", error_code="internal"), seen={"op-1"}) == {}


# ---- a send ----------------------------------------------------------------


def test_a_queued_send_is_marked_until_it_settles():
    mark = marks(operation("controls.send", "accepted", "control_reserved"))["a"]
    assert (mark.kind, mark.glyph) == (MarkKind.QUEUED, "⋯")


def test_a_delivered_send_shows_a_tick_that_fades_by_itself():
    delivered = operation("controls.send", "succeeded", "delivery_acknowledged", age=1.0)
    mark = marks(delivered)["a"]
    assert (mark.kind, mark.glyph) == (MarkKind.SENT, "✓")
    assert mark.expires_at == NOW - 1.0 + REGIE_MARK_SENT_SECONDS
    assert next_expiry([mark]) == mark.expires_at
    stale = operation("controls.send", "succeeded", "delivery_acknowledged", age=5.0)
    assert marks(stale) == {}


def test_interrupting_an_idle_agent_is_not_news():
    assert marks(operation("controls.interrupt", "succeeded", "already_idle")) == {}


@pytest.mark.parametrize(
    ("code", "detail"),
    [
        ("human_present", "blocked · human present"),
        ("busy", "blocked · busy"),
        ("awaiting_decision", "blocked · awaiting a decision"),
    ],
)
def test_a_refusal_that_says_not_now_is_blocked_not_broken(code, detail):
    mark = marks(operation("controls.send", "failed", "delivery_rejected", error_code=code))["a"]
    assert (mark.kind, mark.glyph, mark.detail) == (MarkKind.BLOCKED, "⊘", detail)
    assert mark.attention and mark.needs_seen


def test_any_other_refusal_is_a_failure():
    mark = marks(operation("controls.send", "failed", "delivery_rejected", error_code="internal"))
    assert (mark["a"].kind, mark["a"].glyph) == (MarkKind.FAILED, "✗")


def test_an_uncertain_delivery_is_never_shown_as_a_failure():
    mark = marks(operation("controls.send", "uncertain", "delivery_unknown", error_code="busy"))[
        "a"
    ]
    assert (mark.kind, mark.glyph) == (MarkKind.UNCERTAIN, "?")
    assert mark.detail == "uncertain · delivery unconfirmed"


# ---- choosing and ageing ---------------------------------------------------


def test_what_needs_the_human_outranks_what_is_merely_happening():
    result = marks(
        operation("controls.send", "failed", "x", operation_id="old", age=30, error_code="busy"),
        operation("controls.send", "accepted", "control_reserved", operation_id="new", age=1),
        operation(
            "controls.send", "succeeded", "delivery_acknowledged", operation_id="ok", age=0.5
        ),
    )
    assert result["a"].kind is MarkKind.BLOCKED


def test_the_latest_of_two_equal_ranks_wins():
    result = marks(
        operation("controls.send", "accepted", "control_reserved", operation_id="old", age=20),
        operation("controls.send", "accepted", "control_reserved", operation_id="new", age=2),
    )
    assert result["a"].operation_id == "new"


def test_operations_are_attached_only_to_participants_on_screen():
    assert marks(operation("controls.send", "accepted", "x", target="gone")) == {}


def test_other_operations_make_no_mark():
    assert marks(operation("participants.terminate", "running", "accepted")) == {}


def test_old_operations_stop_marking_the_row():
    assert marks(operation("spawn", "running", "x", age=REGIE_MARK_ACTIVE_MAX_SECONDS + 1)) == {}
    assert (
        marks(
            operation(
                "controls.send",
                "failed",
                "x",
                error_code="busy",
                age=REGIE_MARK_OUTCOME_MAX_SECONDS + 1,
            )
        )
        == {}
    )
