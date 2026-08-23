"""Keyed typing animation for the initial tree and agent-spawned leaves."""

from __future__ import annotations

import subprocess
import sys

from theater.constants.regie import (
    REGIE_NEW_LEAF_REVEAL_COLUMNS_PER_FRAME,
    REGIE_SPINNER_FRAMES,
    REGIE_STARTUP_REVEAL_MAX_LEAVES,
    REGIE_WORKING_HARNESS_STYLES,
)
from theater.regie.animations.pulse import advance_pulse_frame, working_harness_style
from theater.regie.animations.retirement import LeafRetirementController
from theater.regie.animations.reveal import LeafRevealController, clip_parts
from theater.regie.animations.spinner import advance_spinner_frame, spinner_frame


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

    frame = controller.observe({first: 5, later: 20}, animate_new={later}, now=0.1)
    assert frame.widths == {first: 1, later: 0}
    frame = controller.tick({first: 5, later: 20}, now=0.2)
    assert frame.widths[later] == REGIE_NEW_LEAF_REVEAL_COLUMNS_PER_FRAME


def test_each_late_leaf_gets_its_own_deadline():
    key = ("empty", "")
    later = ("p", "later")
    controller = LeafRevealController()
    assert controller.observe({key: 200}, now=0.0).active
    assert not controller.tick({key: 200}, now=10.0).active

    frame = controller.observe({later: 200}, animate_new={later}, now=100.0)
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


def test_ineligible_new_leaf_is_seen_without_being_animated():
    initial = ("p", "initial")
    user_spawn = ("p", "user-spawn")
    controller = LeafRevealController()
    controller.observe({initial: 1}, now=0.0)
    assert not controller.tick({initial: 1}, now=0.1).active

    frame = controller.observe({initial: 1, user_spawn: 20}, now=0.2)
    assert not frame.active
    assert frame.widths == {}

    controller.observe({initial: 1}, now=0.3)
    frame = controller.observe({initial: 1, user_spawn: 20}, now=0.4)
    assert not frame.active


def test_retirement_controller_preserves_agent_spawn_provenance_and_can_restore():
    root = ("p", "root")
    child = ("p", "child")
    controller = LeafRetirementController()

    assert controller.observe({root: False, child: True}).retire == set()
    change = controller.observe({root: False})
    assert change.retire == {child}

    frame = controller.begin({child: 10})
    assert frame.widths == {child: 10}
    frame = controller.tick()
    assert frame.widths == {child: 5}

    change = controller.observe({root: False, child: False})
    assert change.restore == {child}
    assert not controller.active


def test_retirement_controller_drops_candidate_without_a_mounted_width():
    child = ("p", "child")
    controller = LeafRetirementController()

    controller.observe({child: True})
    change = controller.observe({})
    frame = controller.begin({}, candidates=change.retire)

    assert not frame.active
    assert controller.observe({child: False}).restore == set()
    assert controller.observe({}).retire == set()


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

    frame = controller.observe(
        {**initial, first_new: 100, overflow: 100},
        animate_new={first_new, overflow},
        now=0.1,
    )
    assert first_new in frame.widths
    assert overflow not in frame.widths


def test_animations_package_does_not_eagerly_import_back_into_render_glyphs():
    """A cold import of render.glyphs must not re-enter itself via the package.

    Regression guard for the cycle where ``render.glyphs`` imports
    ``animations.pulse``, Python runs ``animations/__init__.py``, and any eager
    reexport there pulls ``animations.reveal`` → ``render.layout`` →
    ``render.glyphs`` (partially initialized).  The package init must stay
    dependency-free; a subprocess import is the only way to catch this in CI.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "; ".join(
                f"import {m}"
                for m in (
                    "theater.regie.render.glyphs",
                    "theater.regie.animations.pulse",
                    "theater.regie.animations.reveal",
                    "theater.regie.animations.retirement",
                    "theater.regie.animations.routes",
                    "theater.regie.animations.footer",
                    "theater.regie.animations.spinner",
                    "theater.regie.animations.cycling_text",
                    "theater.regie.controllers.animation",
                    "theater.regie.controllers.reveal",
                    "theater.regie.render.reveal",
                )
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_advance_spinner_frame_wraps_at_cycle_length():
    """Advancing past the last frame wraps to zero, matching the braille cycle."""
    cycle = len(REGIE_SPINNER_FRAMES)
    assert advance_spinner_frame(0) == 1
    assert advance_spinner_frame(cycle - 1) == 0
    frame = 0
    for _ in range(cycle):
        frame = advance_spinner_frame(frame)
    assert frame == 0
    assert spinner_frame(0) == spinner_frame(cycle)


def test_advance_pulse_frame_wraps_at_cycle_length():
    """Advancing past the last pulse frame wraps to zero, matching the grayscale cycle."""
    cycle = len(REGIE_WORKING_HARNESS_STYLES)
    assert advance_pulse_frame(0) == 1
    assert advance_pulse_frame(cycle - 1) == 0
    frame = 0
    for _ in range(cycle):
        frame = advance_pulse_frame(frame)
    assert frame == 0
    assert working_harness_style(0, 0) == working_harness_style(cycle, 0)
