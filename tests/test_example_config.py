"""`config.example.toml` against the code it claims to document.

An example config is documentation that looks like data, which makes it the
kind that rots without anyone noticing: change a default in `config.py` and the
file at the repo root goes on stating the old one, authoritatively. Nothing
reads it at runtime, so nothing else would ever catch that.

So the file is treated as a claim about the code, and these tests try to
falsify it — the same reason the loader refuses an unknown key rather than
shrugging at it.

The file is loaded verbatim, the way a user's copy would be. Nothing here
restates a default: every expected value is read back out of the dataclasses,
which is what makes these tests notice a setting that was added to the code and
never written down.
"""

from __future__ import annotations

from dataclasses import MISSING, fields
from pathlib import Path

import pytest

from theater import config as cfg
from theater import paths

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"

#: Settings whose default is None, which TOML cannot express: writing the key
#: down at all would change behaviour, so they are the two the example leaves
#: commented. Named here so their absence is asserted rather than tolerated.
NO_DEFAULT = {"theater.favourite", "regie.theme"}


def default_of(f) -> object:
    """The declared default of a field, however it was declared.

    A mutable default has to come from a factory, so `f.default` is MISSING for
    exactly the settings whose value is a list — which is not a difference the
    example file is allowed to have an opinion about.
    """
    return f.default_factory() if f.default is MISSING else f.default


@pytest.fixture
def load(tmp_path, monkeypatch):
    """Load an arbitrary config body as if it were the user's file."""

    def _load(text: str) -> cfg.Config:
        (tmp_path / "config.toml").write_text(text)
        monkeypatch.setattr(paths, "home", lambda: tmp_path)
        return cfg.load()

    return _load


def test_as_shipped_it_parses_and_changes_nothing(load):
    """Copying it unedited must behave exactly like having no config at all.

    Not the same as being *sourced* like one: every key is written down, so
    `theater config` reports the file rather than the code. That is the price
    of a copyable example, and the next test is what pins it.
    """
    got = load(EXAMPLE.read_text())
    plain = cfg.Config()
    for section in cfg._SECTIONS:
        assert getattr(got, section) == getattr(plain, section)
    # Per-key: a mismatch names the offending setting, not just the section.
    for section, klass in cfg._SECTIONS.items():
        for f in fields(klass):
            dotted = f"{section}.{f.name}"
            if dotted in NO_DEFAULT:
                continue
            assert getattr(getattr(got, section), f.name) == default_of(f), dotted


def test_every_setting_is_written_down(load):
    """A key the code accepts but the example omits is undocumented."""
    full = load(EXAMPLE.read_text())
    missing = [
        dotted
        for section, klass in cfg._SECTIONS.items()
        for f in fields(klass)
        if (dotted := f"{section}.{f.name}") not in NO_DEFAULT and full.source(dotted) == "default"
    ]
    assert not missing, f"absent from {EXAMPLE.name}: {', '.join(missing)}"


def test_the_two_unwritable_settings_stay_unwritten(load):
    """Guard the exemption: NO_DEFAULT must not quietly grow stale.

    Both of these mean "unset", and TOML has no word for that — writing either
    one down would set a favourite or a theme for everybody who copies the
    file. If a default ever becomes expressible, this fails and the key should
    move out of the exemption rather than the exemption widening.
    """
    full = load(EXAMPLE.read_text())
    for dotted in NO_DEFAULT:
        section, name = dotted.split(".")
        assert full.source(dotted) == "default", dotted
        assert getattr(getattr(full, section), name) is None, dotted


def test_the_model_allowlist_ships_empty(load):
    """`[models]` is documented but grants nothing.

    The one section with no default to write out: every name in it is a model
    an agent that can spawn may then spend, so a copied example that came with
    entries would hand out that permission to everyone who ran `cp`. The header
    is live because an empty table means exactly what absence means, and having
    it there is what makes the prose above it findable.
    """
    full = load(EXAMPLE.read_text())
    assert full.models == {}
    assert full.models_for("claude") == []
