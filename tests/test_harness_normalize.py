"""Harness name normalization.

The canonical name is what the observer needs to match a participant to an
adapter. A misreported name that passes through unchecked is a silent blind
spot — the participant registers, then is unobservable forever.
"""

from theater.harness import normalize


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
