"""Harness normalization helpers.

The canonical name is what the observer needs to match a participant to an
adapter. A misreported name that passes through unchecked is a silent blind
spot — the participant registers, then is unobservable forever.
"""

import json

import pytest

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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("identifier", "identifier"),
        ("", None),
        ("bad\x00", None),
        ("bad\x7f", None),
        ("bad\x9f", None),
        ("bad\ud800", None),
        ("x" * TRAJECTORY_IDENTIFIER_MAX_BYTES, "x" * TRAJECTORY_IDENTIFIER_MAX_BYTES),
        ("x" * (TRAJECTORY_IDENTIFIER_MAX_BYTES + 1), None),
    ],
)
def test_trajectory_identifier_enforces_utf8_controls_and_byte_limit(value, expected):
    assert trajectory_identifier(value) == expected


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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"ok": True}, ContentFormat.JSON),
        ([1, 2], ContentFormat.JSON),
        ('{"ok":true}', ContentFormat.JSON),
        ("[1,2]", ContentFormat.JSON),
        ('"text"', ContentFormat.TEXT),
        ("42", ContentFormat.TEXT),
        ("plain text", ContentFormat.TEXT),
    ],
)
def test_json_container_format_rejects_json_scalars(value, expected):
    assert json_container_format(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (3, 3),
        (3.0, 3),
        (3.5, 0),
        (-1, 0),
        (-1.0, 0),
        (True, 0),
        (float("nan"), 0),
        (float("inf"), 0),
    ],
)
def test_nonnegative_int_rejects_booleans_negative_and_nonintegral_values(value, expected):
    assert nonnegative_int(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0.0),
        (3.5, 3.5),
        (True, None),
        (float("nan"), None),
        (float("inf"), None),
        (10**1000, None),
    ],
)
def test_finite_float_rejects_booleans_nonfinite_and_overflow(value, expected):
    assert finite_float(value) == expected


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        ("success", TrajectoryStatus.UNKNOWN, TrajectoryStatus.COMPLETED),
        ("in-progress", TrajectoryStatus.UNKNOWN, TrajectoryStatus.RUNNING),
        ("canceled", TrajectoryStatus.UNKNOWN, TrajectoryStatus.CANCELLED),
        ("missing", TrajectoryStatus.PARTIAL, TrajectoryStatus.PARTIAL),
        (None, TrajectoryStatus.PARTIAL, TrajectoryStatus.PARTIAL),
    ],
)
def test_trajectory_status_normalizes_aliases_and_retains_default(value, default, expected):
    assert trajectory_status(value, default) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-27T12:00:00Z", 1_787_832_000.0),
        ("2026-08-27T14:00:00+02:00", 1_787_832_000.0),
        ("not-a-time", None),
        (None, None),
    ],
)
def test_iso_epoch_parses_shared_iso_timestamps(value, expected):
    assert iso_epoch(value) == expected
