"""Harness normalization helpers.

The canonical name is what the observer needs to match a participant to an
adapter. A misreported name that passes through unchecked is a silent blind
spot — the participant registers, then is unobservable forever.
"""

import json

from tests.rig.tables import eq_row, is_row, run_rows
from theater.constants.trajectory import TRAJECTORY_IDENTIFIER_MAX_BYTES
from theater.harness import HARNESSES, UNKNOWN_ICON, harness_icon, normalize
from theater.harness.normalization import (
    finite_float,
    iso_epoch,
    json_container_format,
    nonnegative_int,
    safe_trajectory_text,
    stable_json,
    trajectory_detail,
    trajectory_identifier,
    trajectory_status,
)
from theater.trajectory.content import ContentFormat
from theater.trajectory.enums import TrajectoryStatus


def test_claude_code_maps_to_claude():
    assert normalize("claude_code") == "claude"


def test_unknown_name_passes_through():
    """A genuinely unknown harness is not an error — just unobservable."""
    assert normalize("cursor") == "cursor"
    assert normalize("aider") == "aider"


# ---- icons --------------------------------------------------------------


def test_icons_are_distinct_between_harnesses():
    """Two harnesses drawn with the same mark would defeat the point."""
    icons = [h.icon for h in HARNESSES.values()]
    assert len(set(icons)) == len(icons)


def test_an_alias_gets_the_canonical_glyph():
    """The icon rides on normalize, so registered-as names work too."""
    assert harness_icon("claude-code") == harness_icon("claude")
    assert harness_icon("mistral_vibe") == harness_icon("vibe")


def test_an_unknown_harness_falls_back_rather_than_raising():
    assert harness_icon("cursor") == UNKNOWN_ICON


def test_a_missing_harness_name_falls_back():
    """External participants may have no harness recorded at all."""
    assert harness_icon(None) == UNKNOWN_ICON
    assert harness_icon("") == UNKNOWN_ICON


def test_safe_trajectory_text_replaces_invalid_surrogates():
    assert safe_trajectory_text("bad\ud800text") == "bad?text"
    assert safe_trajectory_text(None) == ""


def test_trajectory_identifier_enforces_utf8_controls_and_byte_limit():
    """Identifiers keep clean UTF-8; control chars, surrogates and overflow drop."""
    limit = TRAJECTORY_IDENTIFIER_MAX_BYTES
    run_rows(
        [
            eq_row("plain", lambda: trajectory_identifier("identifier"), "identifier"),
            eq_row("empty", lambda: trajectory_identifier(""), None),
            eq_row("nul", lambda: trajectory_identifier("bad\x00"), None),
            eq_row("del", lambda: trajectory_identifier("bad\x7f"), None),
            eq_row("c1", lambda: trajectory_identifier("bad\x9f"), None),
            eq_row("surrogate", lambda: trajectory_identifier("bad\ud800"), None),
            eq_row("at_limit", lambda: trajectory_identifier("x" * limit), "x" * limit),
            eq_row("over_limit", lambda: trajectory_identifier("x" * (limit + 1)), None),
        ]
    )


def test_stable_json_is_compact_deterministic_and_safe_on_fallback():
    value = {"z": "é", "a": [2, 1]}
    assert stable_json(value) == '{"a":[2,1],"z":"é"}'
    mixed_keys = {1: "one", "a": "two"}
    assert stable_json(mixed_keys) == json.dumps(str(mixed_keys), ensure_ascii=True)


def test_trajectory_detail_uses_safe_text_or_stable_json():
    text = trajectory_detail("text", "bad\ud800", format=ContentFormat.TEXT)
    payload = trajectory_detail("data", {"b": 2, "a": 1}, format=ContentFormat.JSON)
    assert (text.preview.text, text.format) == ("bad?", ContentFormat.TEXT)
    assert (payload.preview.text, payload.format) == ('{"a":1,"b":2}', ContentFormat.JSON)


def test_json_container_format_rejects_json_scalars():
    """Containers and JSON text parse as JSON; scalars and prose stay TEXT."""
    run_rows(
        [
            is_row("dict", lambda: json_container_format({"ok": True}), ContentFormat.JSON),
            is_row("list", lambda: json_container_format([1, 2]), ContentFormat.JSON),
            is_row("object_text", lambda: json_container_format('{"ok":true}'), ContentFormat.JSON),
            is_row("array_text", lambda: json_container_format("[1,2]"), ContentFormat.JSON),
            is_row("scalar_text", lambda: json_container_format('"text"'), ContentFormat.TEXT),
            is_row("number_text", lambda: json_container_format("42"), ContentFormat.TEXT),
            is_row("plain_text", lambda: json_container_format("plain text"), ContentFormat.TEXT),
        ]
    )


def test_nonnegative_int_rejects_booleans_negative_and_nonintegral_values():
    """Only nonnegative integral values survive; bools and junk clamp to zero."""
    run_rows(
        [
            eq_row("zero", lambda: nonnegative_int(0), 0),
            eq_row("int", lambda: nonnegative_int(3), 3),
            eq_row("whole_float", lambda: nonnegative_int(3.0), 3),
            eq_row("fractional", lambda: nonnegative_int(3.5), 0),
            eq_row("negative_int", lambda: nonnegative_int(-1), 0),
            eq_row("negative_float", lambda: nonnegative_int(-1.0), 0),
            eq_row("boolean", lambda: nonnegative_int(True), 0),
            eq_row("nan", lambda: nonnegative_int(float("nan")), 0),
            eq_row("inf", lambda: nonnegative_int(float("inf")), 0),
        ]
    )


def test_finite_float_rejects_booleans_nonfinite_and_overflow():
    """Finite numbers pass; bools, nan, inf and overflowing ints drop to None."""
    run_rows(
        [
            eq_row("zero", lambda: finite_float(0), 0.0),
            eq_row("float", lambda: finite_float(3.5), 3.5),
            eq_row("boolean", lambda: finite_float(True), None),
            eq_row("nan", lambda: finite_float(float("nan")), None),
            eq_row("inf", lambda: finite_float(float("inf")), None),
            eq_row("overflow", lambda: finite_float(10**1000), None),
        ]
    )


def test_trajectory_status_normalizes_aliases_and_retains_default():
    """Aliases map to canonical statuses; unknowns and None retain the default."""
    run_rows(
        [
            is_row(
                "success",
                lambda: trajectory_status("success", TrajectoryStatus.UNKNOWN),
                TrajectoryStatus.COMPLETED,
            ),
            is_row(
                "in_progress",
                lambda: trajectory_status("in-progress", TrajectoryStatus.UNKNOWN),
                TrajectoryStatus.RUNNING,
            ),
            is_row(
                "canceled",
                lambda: trajectory_status("canceled", TrajectoryStatus.UNKNOWN),
                TrajectoryStatus.CANCELLED,
            ),
            is_row(
                "unknown_keeps_default",
                lambda: trajectory_status("missing", TrajectoryStatus.PARTIAL),
                TrajectoryStatus.PARTIAL,
            ),
            is_row(
                "none_keeps_default",
                lambda: trajectory_status(None, TrajectoryStatus.PARTIAL),
                TrajectoryStatus.PARTIAL,
            ),
        ]
    )


def test_iso_epoch_parses_shared_iso_timestamps():
    """Z and offset forms parse to the same epoch; junk and None drop to None."""
    run_rows(
        [
            eq_row("zulu", lambda: iso_epoch("2026-08-27T12:00:00Z"), 1_787_832_000.0),
            eq_row("offset", lambda: iso_epoch("2026-08-27T14:00:00+02:00"), 1_787_832_000.0),
            eq_row("not_a_time", lambda: iso_epoch("not-a-time"), None),
            eq_row("none", lambda: iso_epoch(None), None),
        ]
    )
