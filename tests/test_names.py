"""Invariant tests for the mask list and the pick function."""

from __future__ import annotations

import re

from tests.rig.tables import eq_row, run_rows
from theater.names import MASKS, is_valid_name, pick

_NAME_RE = re.compile(r"^[A-Z][a-z]+$")
_HEX = set("0123456789abcdef")


def test_every_mask_matches_the_capitalised_ascii_pattern():
    for m in MASKS:
        assert _NAME_RE.match(m), f"{m!r} fails ^[A-Z][a-z]+$"


def test_no_duplicates_exact_or_casefold():
    exact = list(MASKS)
    assert len(exact) == len(set(exact))
    folded = [m.casefold() for m in MASKS]
    assert len(folded) == len(set(folded))


def test_no_mask_is_pure_hex_when_casefolded():
    for m in MASKS:
        assert not all(c in "0123456789abcdef" for c in m.casefold()), f"{m!r} is pure hex"


def test_every_mask_length_is_between_4_and_12():
    for m in MASKS:
        assert 4 <= len(m) <= 12, f"{m!r} has length {len(m)}"


def test_masks_has_roughly_100_entries():
    assert len(MASKS) >= 90


def test_pick_never_returns_a_taken_name():
    taken = {"Arlequin", "Pierrot", "Colombine"}
    for _ in range(50):
        name = pick(taken)
        assert name.casefold() not in {t.casefold() for t in taken}


def test_pick_returns_a_suffixed_name_when_everything_is_taken():
    taken = set(MASKS)
    name = pick(taken)
    # A suffixed name looks like ``Base-2``.
    assert "-" in name
    base, suffix = name.rsplit("-", 1)
    assert base in MASKS
    assert suffix.isdigit()
    assert name.casefold() not in {t.casefold() for t in taken}


def test_pick_is_case_insensitive_about_taken():
    taken = {"arlequin", "PIERROT"}
    for _ in range(50):
        name = pick(taken)
        assert name.casefold() not in {"arlequin", "pierrot"}


def test_name_validation_rejects_trailing_newline_and_other_control_chars():
    """Control characters never reach tmux through a participant name."""
    run_rows(
        [
            eq_row("newline", lambda: is_valid_name("Arlequin\n"), False),
            eq_row("tab", lambda: is_valid_name("Arlequin\t"), False),
            eq_row("nul", lambda: is_valid_name("Arlequin\x00"), False),
            eq_row("carriage_return", lambda: is_valid_name("Arlequin\r"), False),
        ]
    )


def test_name_validation_accepts_valid_boundaries_and_characters():
    """Length boundaries and underscore or digit suffixes are valid names."""
    run_rows(
        [
            eq_row("shortest", lambda: is_valid_name("A"), True),
            eq_row("longest", lambda: is_valid_name("a" * 24), True),
            eq_row("underscore_and_digits", lambda: is_valid_name("A_name-2"), True),
            eq_row("digit", lambda: is_valid_name("Z9"), True),
        ]
    )


def test_name_validation_rejects_invalid_names():
    """Overlong, leading-dash and spaced names are refused."""
    run_rows(
        [
            eq_row("overlong", lambda: is_valid_name("a" * 25), False),
            eq_row("leading_dash", lambda: is_valid_name("-Arlequin"), False),
            eq_row("space", lambda: is_valid_name("Arlequin space"), False),
        ]
    )
