"""Model discovery, per adapter.

Discovery is an authoring aid: `theater models --discover` prints a config
block a human edits and pastes. Nothing here is on the spawn path, which is
what makes the failure mode acceptable — every one of these adapters is
reading something it does not own (another tool's subcommand output, another
tool's config file), and any of it can change under us.

So the property under test is mostly negative: when the source is missing,
unreadable, or shaped differently than expected, the adapter raises
`NotImplementedError` and the command says "cannot be asked". It never
invents a model name, and it never lets an exception out that the CLI would
print as a crash.

The two empty answers are deliberately distinct and both are asserted:
  - `NotImplementedError` — there is no way to ask this CLI at all;
  - `[]`                  — it was asked and genuinely has none.
"""

from __future__ import annotations

import subprocess

import pytest

from theater.harness import get as get_harness

# ---- the adapters that cannot be asked ----------------------------------


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_an_adapter_with_no_source_says_so(name):
    """Neither CLI can list models: no subcommand, and no file that holds a set.

    Asserted rather than left implicit because the honest refusal is the
    feature — the alternative is `theater models --discover claude` printing
    an empty block that looks like an answer.
    """
    with pytest.raises(NotImplementedError) as exc:
        get_harness(name).discover_models()
    assert name in str(exc.value)


def test_the_base_class_refuses_by_default():
    """A third-party plugin costs nothing by not implementing this.

    `plan_launch` is the only abstract method, so this is the whole of a
    minimal plugin — and it discovers nothing without saying a word about
    models.
    """
    from theater.harness.base import Harness, LaunchPlan

    class Nova(Harness):
        name = "nova"
        binary = "nova"

        def plan_launch(self, **kwargs) -> LaunchPlan:
            return LaunchPlan(argv=["nova"])

    with pytest.raises(NotImplementedError):
        Nova().discover_models()


# ---- opencode: a subcommand ---------------------------------------------


def fake_output(monkeypatch, text: str) -> None:
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: text)


def fake_raise(monkeypatch, exc: Exception) -> None:
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(subprocess, "check_output", boom)


def test_opencode_ignores_blank_lines(monkeypatch):
    fake_output(monkeypatch, "\nanthropic/claude-x\n\n  \nopenai/gpt-y\n")
    assert get_harness("opencode").discover_models() == [
        "anthropic/claude-x",
        "openai/gpt-y",
    ]


def test_opencode_with_no_output_reports_none_found(monkeypatch):
    """Asked and answered: none. Not the same as cannot be asked."""
    fake_output(monkeypatch, "")
    assert get_harness("opencode").discover_models() == []


def test_opencode_that_is_not_installed_cannot_be_asked(monkeypatch):
    fake_raise(monkeypatch, FileNotFoundError("opencode"))
    with pytest.raises(NotImplementedError):
        get_harness("opencode").discover_models()


def test_opencode_that_hangs_cannot_be_asked(monkeypatch):
    """The call reaches the network; a human is waiting on it."""
    fake_raise(monkeypatch, subprocess.TimeoutExpired("opencode", 20))
    with pytest.raises(NotImplementedError):
        get_harness("opencode").discover_models()


def test_opencode_that_exits_nonzero_cannot_be_asked(monkeypatch):
    fake_raise(monkeypatch, subprocess.CalledProcessError(1, "opencode"))
    with pytest.raises(NotImplementedError):
        get_harness("opencode").discover_models()


# ---- vibe: another tool's config file -----------------------------------


def write_vibe_config(monkeypatch, tmp_path, text: str):
    monkeypatch.setenv("VIBE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(text, encoding="utf-8")


def test_vibe_returns_both_spellings(monkeypatch, tmp_path):
    """`VIBE_ACTIVE_MODEL` takes either, so both are worth offering."""
    write_vibe_config(
        monkeypatch,
        tmp_path,
        '[[models]]\nname = "claude-opus-5"\nalias = "opus-5"\n',
    )
    assert get_harness("vibe").discover_models() == ["claude-opus-5", "opus-5"]


def test_vibe_does_not_repeat_a_name_used_as_its_own_alias(monkeypatch, tmp_path):
    write_vibe_config(monkeypatch, tmp_path, '[[models]]\nname = "solo"\nalias = "solo"\n')
    assert get_harness("vibe").discover_models() == ["solo"]


def test_vibe_keeps_an_entry_that_has_only_a_name(monkeypatch, tmp_path):
    write_vibe_config(monkeypatch, tmp_path, '[[models]]\nname = "solo"\n')
    assert get_harness("vibe").discover_models() == ["solo"]


def test_vibe_with_no_config_file_cannot_be_asked(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_HOME", str(tmp_path))
    with pytest.raises(NotImplementedError):
        get_harness("vibe").discover_models()


def test_vibe_with_a_broken_config_cannot_be_asked(monkeypatch, tmp_path):
    """A vibe release could ship anything here; it must not crash Theater."""
    write_vibe_config(monkeypatch, tmp_path, "[[models\n")
    with pytest.raises(NotImplementedError):
        get_harness("vibe").discover_models()


def test_vibe_with_no_models_table_cannot_be_asked(monkeypatch, tmp_path):
    write_vibe_config(monkeypatch, tmp_path, '[ui]\ntheme = "dark"\n')
    with pytest.raises(NotImplementedError):
        get_harness("vibe").discover_models()


def test_vibe_skips_an_entry_that_names_nothing(monkeypatch, tmp_path):
    """Shape drift degrades to "found nothing", never to a wrong name."""
    write_vibe_config(monkeypatch, tmp_path, '[[models]]\nprovider = "acme"\n')
    assert get_harness("vibe").discover_models() == []
