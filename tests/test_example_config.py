"""`config.example.toml` against the code it claims to document.

An example config is documentation that looks like data, which makes it the
kind that rots without anyone noticing: change a default in `config.py` and the
file at the repo root goes on stating the old one, authoritatively. Nothing
reads it at runtime, so nothing else would ever catch that.

So the file is treated as a claim about the code, and these tests try to
falsify it — the same reason the loader refuses an unknown key rather than
shrugging at it.

The tests uncomment the file mechanically rather than duplicating its contents,
which is what makes them notice a key that was never written down.
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

#: Splits the settings from the harness declarations. Both halves are
#: uncommented, but by different rules, since only one of them is TOML the
#: user is expected to copy verbatim.
MARKER = "# Declaring a harness"

#: A commented setting: `# poll_interval = 0.25`.
SETTING = re.compile(r"^# ([a-z_]+ = .+)$")

#: A commented declaration line: a `[harness.name]` header on its own, a
#: `key = value`, or the continuation of a wrapped list. The header pattern is
#: anchored so the prose `[harness.<name>] teaches Theater…` is left alone.
DECLARATION = re.compile(r"^# (\[harness\.[a-z0-9_-]+\]$|[a-z_]+ +=|\s+[\"'])")

#: Settings whose default is None: there is no value to compare against, and
#: the example necessarily shows a real one to be useful.
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
def halves() -> tuple[str, str]:
    text = EXAMPLE.read_text()
    assert MARKER in text, f"{EXAMPLE} no longer has the {MARKER!r} section"
    settings, harnesses = text.split(MARKER, 1)
    return settings, harnesses


def uncomment(text: str, pattern: re.Pattern) -> str:
    return "\n".join(
        line[2:] if pattern.match(line) else line for line in text.splitlines()
    )


def test_as_shipped_it_parses_and_changes_nothing(load):
    """Copying it unedited must be indistinguishable from having no config."""
    got = load(EXAMPLE.read_text())
    plain = cfg.Config()
    for section in cfg._SECTIONS:
        assert getattr(got, section) == getattr(plain, section)
    assert got.harnesses == {}
    assert set(got.sources.values()) <= {"default"}


def test_every_setting_is_written_down(load, halves):
    """A key the code accepts but the example omits is undocumented."""
    settings, _ = halves
    full = load(uncomment(settings, SETTING))
    missing = [
        f"{section}.{f.name}"
        for section, klass in cfg._SECTIONS.items()
        for f in fields(klass)
        if full.source(f"{section}.{f.name}") == "default"
    ]
    assert not missing, f"absent from {EXAMPLE.name}: {', '.join(missing)}"


def test_every_documented_default_is_the_real_default(load, halves):
    """The value shown next to each key must be the one the code uses."""
    settings, _ = halves
    full = load(uncomment(settings, SETTING))
    for section, klass in cfg._SECTIONS.items():
        for f in fields(klass):
            dotted = f"{section}.{f.name}"
            if dotted in NO_DEFAULT:
                continue
            assert getattr(getattr(full, section), f.name) == f.default, dotted


def test_the_harness_examples_declare_cleanly(load, halves, tmp_path):
    """Each example must survive the loader, not just look plausible."""
    _, harnesses = halves
    declared = load(uncomment(harnesses, DECLARATION))
    assert sorted(declared.harnesses) == ["codex", "otheragent", "someagent"]
    registry.install(declared, plugin_dir=tmp_path / "none")


def test_the_harness_examples_bake_the_id_into_the_launch(load, halves, tmp_path):
    """The one thing an MCP declaration must get right.

    A declaration that renders an id nowhere produces a session Theater cannot
    talk to — and it fails silently, at spawn time, in someone else's terminal.
    Each example covers a different lever: argv, a written file, an env var.
    """
    _, harnesses = halves
    declared = load(uncomment(harnesses, DECLARATION))
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


def test_an_unset_placeholder_would_be_caught(load, halves, tmp_path):
    """Guard the guard: the id check above must be able to fail."""
    _, harnesses = halves
    body = uncomment(harnesses, DECLARATION).replace("{id}", "no-substitution-here")
    declared = load(body)
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
