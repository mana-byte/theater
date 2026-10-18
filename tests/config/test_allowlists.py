"""The `[models]` and `[reasoning]` per-harness allowlists: parsing, rejection and enforcement."""

from __future__ import annotations

import pytest

from tests.support.config_file import write_config
from theater import config as cfg
from theater.client import DaemonClient
from theater.daemon.server import Daemon
from theater.protocol import RemoteError

ALLOWLISTS = pytest.mark.parametrize(
    ("section", "harness", "accessor", "sample"),
    [("models", "vibe", "models_for", "big"), ("reasoning", "codex", "reasoning_for", "high")],
    ids=["models", "reasoning"],
)


def refused(text: str) -> str:
    write_config(text)
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    return str(exc.value)


@ALLOWLISTS
def test_an_allowlist_is_read_per_harness(section, harness, accessor, sample):
    write_config(f'[{section}]\n{harness} = ["small", "{sample}"]\n')
    assert getattr(cfg.load(), accessor)(harness) == ["small", sample]


@ALLOWLISTS
def test_a_harness_with_no_entry_allows_nothing(section, harness, accessor, sample):
    write_config(f'[{section}]\n{harness} = ["{sample}"]\n')
    assert getattr(cfg.load(), accessor)("claude") == []


@ALLOWLISTS
def test_no_allowlist_section_at_all_is_not_an_error(section, harness, accessor, sample):
    """The default install. Every harness is simply unlisted."""
    write_config("[rails]\ndepth_cap = 2\n")
    assert getattr(cfg.load(), accessor)(harness) == []


@ALLOWLISTS
def test_an_allowlist_section_must_be_a_table(section, harness, accessor, sample):
    assert "must be a table" in refused(f"{section} = 3\n")


@ALLOWLISTS
def test_an_allowlist_entry_must_be_a_list_of_strings(section, harness, accessor, sample):
    assert "must be a list of strings" in refused(f"[{section}]\n{harness} = 3\n")


@ALLOWLISTS
def test_a_value_listed_twice_is_refused(section, harness, accessor, sample):
    """A copy-paste artefact, not a second entry."""
    message = refused(f'[{section}]\n{harness} = ["{sample}", "{sample}"]\n')
    assert "more than once" in message


@ALLOWLISTS
def test_an_illegal_harness_name_is_refused(section, harness, accessor, sample):
    assert "legal harness name" in refused(f'[{section}]\n"Not A Harness" = ["{sample}"]\n')


@pytest.mark.parametrize(
    ("typo", "section", "harness"),
    [("modles", "models", "vibe"), ("reasonig", "reasoning", "codex")],
    ids=["models", "reasoning"],
)
def test_a_typo_of_an_allowlist_section_name_suggests_it(typo, section, harness):
    assert section in refused(f'[{typo}]\n{harness} = ["big"]\n')


@ALLOWLISTS
def test_describe_reports_a_configured_allowlist(section, harness, accessor, sample):
    write_config(f'[{section}]\n{harness} = ["{sample}"]\n')
    rows = cfg.describe(cfg.load())
    assert any(key == f"{section}.{harness}" for key, _, _ in rows)


def test_a_list_of_non_strings_is_refused():
    refused("[models]\nvibe = [1, 2]\n")


def test_a_harness_that_is_not_installed_is_not_a_parse_error():
    """This module knows nothing of the registry; the daemon checks that."""
    write_config('[models]\nnotinstalled = ["big"]\n')
    assert cfg.load().models_for("notinstalled") == ["big"]


def test_an_empty_list_is_legal_and_means_nothing_is_allowed():
    """Distinct from absent only in intent, and both refuse every --model."""
    write_config("[models]\nvibe = []\n")
    assert cfg.load().models_for("vibe") == []


def test_describe_says_nothing_about_unlisted_harnesses():
    """`[models]` has no fields to enumerate, so there is no default row."""
    rows = cfg.describe(cfg.load())
    assert not [key for key, _, _ in rows if key.startswith("models.")]


def test_reasoning_section_is_recognized_as_legal():
    """The section the parser builds must also be in the legal set."""
    write_config('[reasoning]\ncodex = ["high"]\n')
    loaded = cfg.load()
    assert loaded.exists is True
    assert loaded.reasoning_for("codex") == ["high"]


def test_reasoning_and_models_coexist():
    write_config('[models]\nvibe = ["small"]\n[reasoning]\ncodex = ["high"]\n')
    loaded = cfg.load()
    assert loaded.models_for("vibe") == ["small"]
    assert loaded.reasoning_for("codex") == ["high"]


async def test_a_model_outside_the_allowlist_actually_stops_a_spawn(available_harness_binaries):
    """The end-to-end claim, as for the depth cap: the file changes behaviour."""
    write_config('[models]\nvibe = ["small"]\n')
    daemon = Daemon(harnesses={})
    await daemon.start()
    client = DaemonClient(autostart=False)
    await client.connect()
    try:
        with pytest.raises(RemoteError) as exc:
            await client.call(
                "spawn",
                harness="vibe",
                prompt="hi",
                approval="manual",
                cwd="/tmp",
                model="enormous",
            )
        assert exc.value.code == "model_not_allowed"
        with pytest.raises(RemoteError) as allowed:
            await client.call(
                "spawn",
                harness="vibe",
                prompt="hi",
                approval="manual",
                cwd="/tmp",
                model="small",
            )
        assert allowed.value.code == "provider_unavailable"
    finally:
        await client.aclose()
        await daemon.aclose()


async def test_an_unlisted_harness_reaches_provider_selection_without_a_model(
    available_harness_binaries,
):
    """An absent bridge, rather than model policy, refuses the default launch."""
    daemon = Daemon(harnesses={})
    await daemon.start()
    client = DaemonClient(autostart=False)
    await client.connect()
    try:
        with pytest.raises(RemoteError) as exc:
            await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
        assert exc.value.code == "provider_unavailable"
    finally:
        await client.aclose()
        await daemon.aclose()
