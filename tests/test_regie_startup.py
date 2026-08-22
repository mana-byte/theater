"""Keyed typing animation for initial and newly discovered régie leaves."""

from __future__ import annotations

from theater.constants.regie import (
    REGIE_NEW_LEAF_REVEAL_COLUMNS_PER_FRAME,
    REGIE_STARTUP_REVEAL_MAX_LEAVES,
)
from theater.regie.controllers.reveal import LeafRevealController
from theater.regie.render.reveal import clip_parts


def test_clip_parts_preserves_styles_at_the_cut():
    assert clip_parts([("Ctrl+P", "accent"), (" to start", "dim")], 8) == [
        ("Ctrl+P", "accent"),
        (" t", "dim"),
    ]
    assert clip_parts([("Ctrl+P", "accent")], 0) == []


def test_controller_reveals_later_keys_at_the_faster_rate():
    first = ("p", "first")
    later = ("p", "later")
    controller = LeafRevealController()

    frame = controller.observe({first: 5}, now=0.0)
    assert frame.active and frame.widths == {first: 0}
    frame = controller.tick({first: 5}, now=0.1)
    assert frame.widths == {first: 1}

    frame = controller.observe({first: 5, later: 20}, now=0.1)
    assert frame.widths == {first: 1, later: 0}
    frame = controller.tick({first: 5, later: 20}, now=0.2)
    assert frame.widths[later] == REGIE_NEW_LEAF_REVEAL_COLUMNS_PER_FRAME


def test_each_late_leaf_gets_its_own_deadline():
    key = ("empty", "")
    later = ("p", "later")
    controller = LeafRevealController()
    assert controller.observe({key: 200}, now=0.0).active
    assert not controller.tick({key: 200}, now=10.0).active

    frame = controller.observe({later: 200}, now=100.0)
    assert frame.active and frame.widths == {later: 0}
    assert controller.tick({later: 200}, now=100.1).widths == {
        later: REGIE_NEW_LEAF_REVEAL_COLUMNS_PER_FRAME
    }


def test_empty_placeholder_is_only_eligible_on_the_initial_snapshot():
    participant = ("p", "first")
    empty = ("empty", "")
    controller = LeafRevealController()
    controller.observe({participant: 1}, now=0.0)
    assert not controller.tick({participant: 1}, now=0.1).active

    frame = controller.observe({empty: 20}, now=1.0)
    assert not frame.active
    assert frame.widths == {}


def test_controller_skips_unusually_large_trees():
    required = {("p", str(index)): 10 for index in range(REGIE_STARTUP_REVEAL_MAX_LEAVES + 1)}
    frame = LeafRevealController().observe(required, now=0.0)
    assert not frame.active
    assert frame.widths == {}


def test_controller_caps_total_pending_reveals():
    initial = {
        ("p", f"initial-{index}"): 100 for index in range(REGIE_STARTUP_REVEAL_MAX_LEAVES - 1)
    }
    first_new = ("p", "new-1")
    overflow = ("p", "new-2")
    controller = LeafRevealController()
    controller.observe(initial, now=0.0)

    frame = controller.observe({**initial, first_new: 100, overflow: 100}, now=0.1)
    assert first_new in frame.widths
    assert overflow not in frame.widths
