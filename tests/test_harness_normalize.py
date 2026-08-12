"""Harness name normalization.

The canonical name is what the observer needs to match a participant to an
adapter. A misreported name that passes through unchecked is a silent blind
spot — the participant registers, then is unobservable forever.
"""

from theater.harness import HARNESSES, UNKNOWN_ICON, harness_icon, normalize


def test_claude_code_maps_to_claude():
    assert normalize("claude_code") == "claude"


def test_claude_code_with_dash_maps_to_claude():
    assert normalize("claude-code") == "claude"


def test_capitalized_claude_maps_to_claude():
    assert normalize("Claude") == "claude"
    assert normalize("ClaudeCode") == "claude"


def test_vibe_is_canonical():
    assert normalize("vibe") == "vibe"


def test_capitalized_vibe_maps_to_vibe():
    assert normalize("Vibe") == "vibe"


def test_mistral_vibe_maps_to_vibe():
    assert normalize("mistral-vibe") == "vibe"
    assert normalize("mistral_vibe") == "vibe"


def test_unknown_name_passes_through():
    """A genuinely unknown harness is not an error — just unobservable."""
    assert normalize("cursor") == "cursor"
    assert normalize("aider") == "aider"


def test_empty_string_passes_through():
    assert normalize("") == ""


# ---- icons --------------------------------------------------------------


def test_a_known_harness_has_its_own_glyph():
    assert harness_icon("vibe") == HARNESSES["vibe"].icon
    assert harness_icon("claude") == HARNESSES["claude"].icon


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


def test_every_icon_is_one_column_wide():
    """The listing pads to a fixed column; a two-cell glyph would shear it."""
    for harness in HARNESSES.values():
        assert len(harness.icon) == 1
    assert len(UNKNOWN_ICON) == 1
