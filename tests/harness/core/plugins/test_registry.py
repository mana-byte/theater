"""The plugin registry: install, override, aliases, binary claims and disabled harnesses."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.plugin_catalog_rig import local_dir as shared_local_dir
from tests.support.plugin_packages import install, write_plugin
from theater import cli, paths
from theater import config as cfg
from theater import harness as harness_registry
from theater.harness.loading.discovery import MANIFEST_FILENAME
from theater.harness.loading.models import PluginError

# Two directories, one loader: the adapters Theater ships and `$THEATER_HOME/plugins/`. The
# refusals, precedence and collision guards matter most.

local_dir = shared_local_dir  # the shared fixture


@pytest.fixture
def shipped_dir(tmp_path) -> Path:
    """Stand-in for the directory of adapters Theater ships."""
    d = tmp_path / "shipped"
    d.mkdir()
    return d


def disabling(*names: str) -> cfg.Config:
    return cfg.Config(harness=cfg.HarnessSection(disabled=list(names)))


def test_a_plugin_joins_the_registry(local_dir):
    write_plugin(local_dir)
    assert install(local_dir) == ["acme", "claude", "codex", "opencode", "pi", "vibe"]
    assert harness_registry.get("acme").binary == "acme"
    assert harness_registry.harness_icon("acme") == "@"


def test_the_shiped_adapters_are_plugins_too(local_dir):
    """No built-in tier: every adapter comes through the same loader."""
    assert install(local_dir) == ["claude", "codex", "opencode", "pi", "vibe"]
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["vibe"]["source"] == "shipped"
    assert rows["claude"]["source"] == "shipped"
    assert set(rows["claude"]["binaries"]) == {".claude-wrapped", "claude-wrapped"}
    assert rows["codex"]["source"] == "shipped"
    assert rows["opencode"]["source"] == "shipped"
    assert rows["pi"]["source"] == "shipped"


def test_a_plugin_plans_its_own_launch(local_dir):
    write_plugin(local_dir)
    install(local_dir)
    plan = harness_registry.plan_launch(
        "acme",
        participant_id="abc123",
        prompt="hello",
        config_path=Path("/tmp/x.json"),
        approval="manual",
    )
    assert plan.argv == ["acme", "abc123"]
    assert plan.env == {"ID": "abc123"}


def test_installing_twice_is_the_same_as_once(local_dir):
    write_plugin(local_dir)
    install(local_dir)
    assert install(local_dir) == ["acme", "claude", "codex", "opencode", "pi", "vibe"]


def test_installing_an_empty_directory_drops_the_plugin(local_dir, tmp_path):
    write_plugin(local_dir)
    install(local_dir)
    install(tmp_path / "gone")
    assert "acme" not in harness_registry.HARNESSES


@pytest.mark.parametrize(
    "body", ["raise ValueError('boom')", "import sys; sys.exit(1)"], ids=["raises", "sys_exit"]
)
def test_a_broken_local_plugin_does_not_stop_start_up(local_dir, body):
    pkg = local_dir / "broken"
    pkg.mkdir()
    (pkg / MANIFEST_FILENAME).write_text(body)
    write_plugin(local_dir)
    assert install(local_dir) == ["acme", "claude", "codex", "opencode", "pi", "vibe"]


@pytest.mark.parametrize(
    "fragment", ["acme", 'disabled = ["acme"]'], ids=["names_it", "names_the_escape_hatch"]
)
def test_a_broken_shipped_plugin_is_fatal(local_dir, shipped_dir, fragment):
    pkg = shipped_dir / "acme"
    pkg.mkdir()
    (pkg / MANIFEST_FILENAME).write_text("raise ValueError('boom')")
    with pytest.raises(PluginError) as exc:
        install(local_dir, shipped_dir=shipped_dir)
    assert fragment in str(exc.value)


def test_disabling_a_plugin_stops_it_being_imported(local_dir, shipped_dir):
    """The escape hatch has to work when importing is exactly what breaks."""
    pkg = shipped_dir / "acme"
    pkg.mkdir()
    (pkg / MANIFEST_FILENAME).write_text("raise ValueError('boom')")
    write_plugin(shipped_dir, name="vibe", binary="vibe")
    assert install(local_dir, disabling("acme"), shipped_dir=shipped_dir) == ["vibe"]


def test_a_local_plugin_overrides_a_shipped_one(local_dir):
    write_plugin(local_dir, name="vibe", binary="vibe")
    install(local_dir)
    assert harness_registry.get("vibe").binary == "vibe"
    assert harness_registry.normalize("mistral-vibe") == "vibe"


def test_an_override_reports_itself_as_local(local_dir):
    write_plugin(local_dir, name="vibe", binary="vibe")
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["vibe"]["source"] == "local"


def test_a_plugin_alias_normalizes(local_dir):
    write_plugin(local_dir, aliases='("acme-cli",)')
    install(local_dir)
    assert harness_registry.normalize("acme-cli") == "acme"


@pytest.mark.parametrize(
    ("shipped", "local", "match"),
    [
        pytest.param(
            [],
            [{"aliases": '("mistral-vibe",)'}],
            "already resolves",
            id="alias_cannot_shadow_another_harness",
        ),
        pytest.param(
            [], [{"aliases": '("claude",)'}], "name of another", id="alias_cannot_be_a_harness_name"
        ),
        pytest.param(
            [{"name": "vibe", "binary": "vibe", "aliases": '("mistral-vibe",)'}],
            [{"name": "mistral-vibe", "binary": "mv"}],
            "already an alias of",
            id="name_cannot_shadow_an_alias",
        ),
        pytest.param(
            [{"name": "mistral-vibe", "binary": "mv"}],
            [{"name": "vibe", "binary": "vibe", "aliases": '("mistral-vibe",)'}],
            "name of another",
            id="alias_cannot_shadow_a_name",
        ),
    ],
)
def test_a_plugin_cannot_shadow_another_name_or_alias(
    local_dir, shipped_dir, shipped, local, match
):
    for spec in shipped:
        write_plugin(shipped_dir, **spec)
    for spec in local:
        write_plugin(local_dir, **spec)
    kwargs = {"shipped_dir": shipped_dir} if shipped else {}
    with pytest.raises(harness_registry.ConfigError, match=match):
        install(local_dir, **kwargs)


def test_a_local_override_releases_the_shipped_binary_claims(local_dir, shipped_dir):
    write_plugin(shipped_dir, name="acme", binary="foo", aliases='("foo-cli",)')
    write_plugin(local_dir, name="acme", binary="bar")
    write_plugin(local_dir, name="zzz", binary="foo")
    result = install(local_dir, shipped_dir=shipped_dir)
    assert "acme" in result
    assert "zzz" in result
    assert harness_registry.get("acme").binary == "bar"
    assert harness_registry.get("zzz").binary == "foo"
    assert harness_registry.normalize("foo-cli") == "acme"


def test_binary_collision_error_names_both_files(local_dir, shipped_dir):
    write_plugin(shipped_dir, name="first", binary="nova")
    write_plugin(local_dir, name="second", binary="nova")
    with pytest.raises(harness_registry.ConfigError, match="already claimed by") as exc:
        install(local_dir, shipped_dir=shipped_dir)
    msg = str(exc.value)
    assert "second" in msg
    assert "first" in msg


@pytest.mark.parametrize(
    ("first", "second"),
    [("very-long-binary", "very-long-binary2"), ("exactly15chars", "exactly15chars2")],
    ids=["shared_first_15_chars", "exact_15_chars_against_the_truncated_form"],
)
def test_binaries_sharing_an_observation_key_are_refused(local_dir, shipped_dir, first, second):
    write_plugin(shipped_dir, name="first", binary=first)
    write_plugin(local_dir, name="second", binary=second)
    with pytest.raises(harness_registry.ConfigError) as exc:
        install(local_dir, shipped_dir=shipped_dir)
    msg = str(exc.value)
    assert "observation key" in msg
    assert first in msg


def test_two_truncated_spellings_of_same_harness_are_accepted(local_dir, shipped_dir):
    write_plugin(shipped_dir, name="solo", binary="very-long-binary")
    write_plugin(local_dir, name="solo", binary="very-long-binary", aliases='("solo",)')
    pkg = local_dir / "solo"
    body = (pkg / MANIFEST_FILENAME).read_text()
    body = body.replace(
        "MANIFEST = HarnessManifest(",
        "MANIFEST = HarnessManifest(\n    binaries=frozenset({'very-long-binary2'}),",
    )
    (pkg / MANIFEST_FILENAME).write_text(body)
    result = install(local_dir, shipped_dir=shipped_dir)
    assert "solo" in result


def test_a_disabled_harness_is_absent(local_dir):
    assert install(local_dir, disabling("vibe")) == ["claude", "codex", "opencode", "pi"]
    assert "vibe" not in harness_registry.HARNESSES


def test_a_disabled_harness_leaves_the_unmanaged_sweep(local_dir):
    install(local_dir, disabling("vibe"))
    assert "vibe" not in harness_registry.known_binaries()


def test_a_disabled_harness_still_draws_in_the_tree(local_dir):
    install(local_dir, disabling("vibe"))
    assert harness_registry.harness_icon("vibe") == harness_registry.UNKNOWN_ICON


def test_disabling_something_that_is_not_there_is_not_an_error(local_dir):
    assert install(local_dir, disabling("nosuchharness")) == [
        "claude",
        "codex",
        "opencode",
        "pi",
        "vibe",
    ]


def test_the_cli_loads_plugins_from_theater_home(capsys):
    paths.ensure_home()
    write_plugin(paths.plugins_dir())
    assert cli.main(["harnesses"]) == 0
    out = capsys.readouterr().out
    assert "acme" in out


def test_a_plugin_binary_joins_the_unmanaged_sweep(local_dir):
    write_plugin(local_dir, binary="acme-bin")
    install(local_dir)
    assert "acme-bin" in harness_registry.known_binaries()


def test_a_plugin_shows_up_in_describe(local_dir):
    write_plugin(local_dir)
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["acme"]["icon"] == "@"
    assert rows["acme"]["binary"] == "acme"
    assert rows["acme"]["source"] == "local"
    assert rows["acme"]["error"] is None
    assert rows["acme"]["manifest_api_version"] == 1
    assert rows["acme"]["approvals"] == ["manual"]
    assert rows["acme"]["package_path"] == str(local_dir / "acme")
    assert rows["acme"]["primary_channel"] is None
    assert rows["acme"]["runtime"] == {"state": "inactive", "participants": []}
