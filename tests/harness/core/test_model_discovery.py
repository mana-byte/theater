"""Per-harness model discovery: listing, aliases and refusal when nothing can be asked."""

from __future__ import annotations

import subprocess

import pytest

from theater.harness import get as get_harness


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


def fake_output(monkeypatch, text: str) -> None:
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: text)


def fake_raise(monkeypatch, exc: Exception) -> None:
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(subprocess, "check_output", boom)


@pytest.mark.parametrize(
    ("text", "models"),
    [
        pytest.param(
            "\nanthropic/claude-x\n\n  \nopenai/gpt-y\n",
            ["anthropic/claude-x", "openai/gpt-y"],
            id="ignores_blank_lines",
        ),
        # Asked and answered: none. Not the same as cannot be asked.
        pytest.param("", [], id="no_output_reports_none_found"),
    ],
)
def test_opencode_lists_the_models_it_prints(monkeypatch, text, models):
    fake_output(monkeypatch, text)
    assert get_harness("opencode").discover_models() == models


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("opencode"),
        # The call reaches the network; a human is waiting on it.
        subprocess.TimeoutExpired("opencode", 20),
        subprocess.CalledProcessError(1, "opencode"),
    ],
    ids=["not_installed", "hangs", "exits_nonzero"],
)
def test_opencode_that_cannot_answer_cannot_be_asked(monkeypatch, failure):
    fake_raise(monkeypatch, failure)
    with pytest.raises(NotImplementedError):
        get_harness("opencode").discover_models()


def write_vibe_config(monkeypatch, tmp_path, text: str):
    monkeypatch.setenv("VIBE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("config", "models"),
    [
        # `VIBE_ACTIVE_MODEL` takes either spelling, so both are worth offering.
        pytest.param(
            '[[models]]\nname = "claude-opus-5"\nalias = "opus-5"\n',
            ["claude-opus-5", "opus-5"],
            id="returns_both_spellings",
        ),
        pytest.param(
            '[[models]]\nname = "solo"\nalias = "solo"\n',
            ["solo"],
            id="does_not_repeat_a_name_used_as_its_own_alias",
        ),
        pytest.param('[[models]]\nname = "solo"\n', ["solo"], id="keeps_an_entry_with_only_a_name"),
        # Shape drift degrades to "found nothing", never to a wrong name.
        pytest.param('[[models]]\nprovider = "acme"\n', [], id="skips_an_entry_that_names_nothing"),
    ],
)
def test_vibe_lists_the_models_in_its_config(monkeypatch, tmp_path, config, models):
    write_vibe_config(monkeypatch, tmp_path, config)
    assert get_harness("vibe").discover_models() == models


@pytest.mark.parametrize(
    "config",
    [
        None,
        # A vibe release could ship anything here; it must not crash Theater.
        "[[models\n",
        '[ui]\ntheme = "dark"\n',
    ],
    ids=["no_config_file", "broken_config", "no_models_table"],
)
def test_vibe_without_a_usable_config_cannot_be_asked(monkeypatch, tmp_path, config):
    if config is None:
        monkeypatch.setenv("VIBE_HOME", str(tmp_path))
    else:
        write_vibe_config(monkeypatch, tmp_path, config)
    with pytest.raises(NotImplementedError):
        get_harness("vibe").discover_models()
