"""Harness name normalization.

The canonical name is what the observer needs to match a participant to an
adapter. A misreported name that passes through unchecked is a silent blind
spot — the participant registers, then is unobservable forever.
"""

from theater.harness import HARNESSES, UNKNOWN_ICON, harness_icon, normalize


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
