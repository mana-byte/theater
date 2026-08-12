"""`config.example.toml` against the code it claims to document.

An example config is documentation that looks like data, which makes it the
kind that rots without anyone noticing: change a default in `config.py` and the
file at the repo root goes on stating the old one, authoritatively. Nothing
reads it at runtime, so nothing else would ever catch that.

So the file is treated as a claim about the code, and these tests try to
falsify it — the same reason the loader refuses an unknown key rather than
shrugging at it.

The settings are loaded verbatim: the file sets every one of them to its
default, so it is valid TOML as shipped and the tests can read it the way a
user's copy would be read. The harness declarations are examples rather than
defaults and stay commented, so that half is uncommented mechanically — never
duplicated here, which is what makes these tests notice a key that was never
written down.
"""

from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

import pytest

from theater import config as cfg
from theater import harness as registry
from theater import paths

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"

#: Splits the live settings from the commented harness declarations.
MARKER = "# Declaring a harness"

#: A commented declaration line: a `[harness.name]` header on its own, a
#: `key = value`, or the continuation of a wrapped list. The header pattern is
#: anchored so the prose `[harness.<name>] teaches Theater…` is left alone.
DECLARATION = re.compile(r"^# (\[harness\.[a-z0-9_-]+\]$|[a-z_]+ +=|\s+[\"'])")

#: Settings whose default is None, which TOML cannot express: writing the key
#: down at all would change behaviour, so they are the two the example leaves
#: commented. Named here so their absence is asserted rather than tolerated.
NO_DEFAULT = {"theater.favourite", "regie.theme"}


@pytest.fixture
def load(tmp_path, monkeypatch):
    """Load an arbitrary config body as if it were the user's file."""

    def _load(text: str) -> cfg.Config:
        (tmp_path / "config.toml").write_text(text)
        monkeypatch.setattr(paths, "home", lambda: tmp_path)
        return cfg.load()

    return _load


@pytest.fixture
def declarations() -> str:
    """The harness half of the example, uncommented into loadable TOML."""
    text = EXAMPLE.read_text()
    assert MARKER in text, f"{EXAMPLE} no longer has the {MARKER!r} section"
    _, harnesses = text.split(MARKER, 1)
    return "\n".join(
        line[2:] if DECLARATION.match(line) else line for line in harnesses.splitlines()
    )


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
    assert got.harnesses == {}


def test_every_setting_is_written_down(load):
    """A key the code accepts but the example omits is undocumented."""
    full = load(EXAMPLE.read_text())
    missing = [
        dotted
        for section, klass in cfg._SECTIONS.items()
        for f in fields(klass)
        if (dotted := f"{section}.{f.name}") not in NO_DEFAULT
        and full.source(dotted) == "default"
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


def test_every_documented_default_is_the_real_default(load):
    """The value shown next to each key must be the one the code uses."""
    full = load(EXAMPLE.read_text())
    for section, klass in cfg._SECTIONS.items():
        for f in fields(klass):
            dotted = f"{section}.{f.name}"
            if dotted in NO_DEFAULT:
                continue
            assert getattr(getattr(full, section), f.name) == f.default, dotted


def test_the_harness_examples_declare_cleanly(load, declarations, tmp_path):
    """Each example must survive the loader, not just look plausible."""
    declared = load(declarations)
    assert sorted(declared.harnesses) == ["codex", "otheragent", "someagent"]
    registry.install(declared, plugin_dir=tmp_path / "none")


def test_the_harness_examples_bake_the_id_into_the_launch(load, declarations, tmp_path):
    """The one thing an MCP declaration must get right.

    A declaration that renders an id nowhere produces a session Theater cannot
    talk to — and it fails silently, at spawn time, in someone else's terminal.
    Each example covers a different lever: argv, a written file, an env var.
    """
    declared = load(declarations)
    registry.install(declared, plugin_dir=tmp_path / "none")
    try:
        for name in declared.harnesses:
            plan = registry.get(name).plan_launch(
                participant_id="abc123def456",
                prompt="hello",
                config_path=tmp_path / "mcp.json",
                approval="manual",
            )
            rendered = [*plan.argv, *plan.env.values(), *plan.files.values()]
            assert any("abc123def456" in part for part in rendered), name
            assert plan.argv[0] == registry.get(name).binary
            assert plan.argv[-1] == "hello"
    finally:
        registry.install(cfg.Config(), plugin_dir=tmp_path / "none")


def test_an_unset_placeholder_would_be_caught(load, declarations, tmp_path):
    """Guard the guard: the id check above must be able to fail."""
    declared = load(declarations.replace("{id}", "no-substitution-here"))
    registry.install(declared, plugin_dir=tmp_path / "none")
    try:
        plan = registry.get("codex").plan_launch(
            participant_id="abc123def456",
            prompt="hello",
            config_path=tmp_path / "mcp.json",
            approval="manual",
        )
        rendered = [*plan.argv, *plan.env.values(), *plan.files.values()]
        assert not any("abc123def456" in part for part in rendered)
    finally:
        registry.install(cfg.Config(), plugin_dir=tmp_path / "none")
