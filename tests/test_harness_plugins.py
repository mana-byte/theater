"""Harness package-manifest plugins: registry, collision, and refusal behavior.

Two directories, one loader: the adapters Theater ships and the ones a user
drops in `$THEATER_HOME/harnesses/`. The tests that matter here are the
refusals, the precedence, and the collision guards — all using named package
manifests, the sole plugin format.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from theater import cli, paths
from theater import config as cfg
from theater import harness as harness_registry
from theater.constants.harness import (
    HARNESS_DIAGNOSTICS_MAX_BINDINGS,
    HARNESS_DIAGNOSTICS_MAX_CHANNELS,
    HARNESS_DIAGNOSTICS_MAX_PARTICIPANTS,
    HARNESS_DIAGNOSTICS_UNAVAILABLE_REASON_MAX_CHARS,
)
from theater.harness.contracts.channels import (
    ChannelDeclaration,
    ChannelHealth,
    ChannelKind,
    HookBinding,
)
from theater.harness.contracts.manifest import (
    HarnessManifest,
    HookChannelManifest,
    LaunchManifest,
    ObservationManifest,
    ScreenManifest,
    SourceManifest,
    UnavailableChannelManifest,
)
from theater.harness.loading.discovery import MANIFEST_FILENAME
from theater.harness.loading.models import LoadedPlugin, PluginError
from theater.harness.registry.diagnostics import project_plugin

MANIFEST_BODY = """
from theater.harness.contracts.callbacks import LaunchContext, ScreenContext
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.manifest import (
    MANIFEST_API_VERSION,
    HarnessManifest,
    LaunchManifest,
    ObservationManifest,
    ScreenManifest,
)
from theater.harness.contracts.observation import ScreenConfidence, ScreenKind, ScreenReading

def _plan(context):
    return LaunchPlan(
        argv=["{binary}", context.participant_id],
        env={{"ID": context.participant_id}},
    )

def _screen(_context):
    return ScreenReading(ScreenKind.PROMPT, ScreenConfidence.HIGH)

MANIFEST = HarnessManifest(
    api_version=MANIFEST_API_VERSION,
    binary="{binary}",
    icon={icon},
    aliases={aliases},
    launch=LaunchManifest(planner=_plan, approvals=frozenset({{"manual"}})),
    observation=ObservationManifest(
        primary=None,
        screen=ScreenManifest(classifier=_screen),
    ),
)
"""


def write_plugin(
    dirpath: Path,
    name: str = "acme",
    *,
    binary: str = "acme",
    icon: str = "@",
    aliases: str = "()",
) -> Path:
    """Create a named package manifest under ``dirpath``."""
    pkg = dirpath / name
    pkg.mkdir(parents=True, exist_ok=True)
    manifest_path = pkg / MANIFEST_FILENAME
    manifest_path.write_text(
        MANIFEST_BODY.format(binary=binary, icon=repr(icon), aliases=aliases),
        encoding="utf-8",
    )
    return pkg


@pytest.fixture
def local_dir(tmp_path) -> Path:
    """Stand-in for `$THEATER_HOME/harnesses`."""
    d = tmp_path / "harnesses"
    d.mkdir()
    return d


@pytest.fixture
def shipped_dir(tmp_path) -> Path:
    """Stand-in for the directory of adapters Theater ships."""
    d = tmp_path / "shipped"
    d.mkdir()
    return d


def install(local: Path, config: cfg.Config | None = None, **kwargs) -> list[str]:
    return harness_registry.install(config or cfg.Config(), local_dir=local, **kwargs)


def disabling(*names: str) -> cfg.Config:
    return cfg.Config(harness=cfg.HarnessSection(disabled=list(names)))


# ---- loading and registry ------------------------------------------------


def test_a_plugin_joins_the_registry(local_dir):
    write_plugin(local_dir)
    assert install(local_dir) == ["acme", "claude", "codex", "opencode", "vibe"]
    assert harness_registry.get("acme").binary == "acme"
    assert harness_registry.harness_icon("acme") == "@"


def test_the_shiped_adapters_are_plugins_too(local_dir):
    """No built-in tier: every adapter comes through the same loader."""
    assert install(local_dir) == ["claude", "codex", "opencode", "vibe"]
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["vibe"]["source"] == "shipped"
    assert rows["claude"]["source"] == "shipped"
    assert rows["codex"]["source"] == "shipped"
    assert rows["opencode"]["source"] == "shipped"


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
    assert install(local_dir) == ["acme", "claude", "codex", "opencode", "vibe"]


def test_installing_an_empty_directory_drops_the_plugin(local_dir, tmp_path):
    write_plugin(local_dir)
    install(local_dir)
    install(tmp_path / "gone")
    assert "acme" not in harness_registry.HARNESSES


# ---- refusals -----------------------------------------------------------


def test_a_broken_manifest_names_the_directory(local_dir):
    pkg = local_dir / "broken"
    pkg.mkdir()
    (pkg / MANIFEST_FILENAME).write_text("raise ValueError('boom')")
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["broken"]["source"] == "local"
    assert "boom" in rows["broken"]["error"]
    assert MANIFEST_FILENAME in rows["broken"]["error"]


def test_a_manifest_with_syntax_error_names_the_file(local_dir):
    pkg = local_dir / "broken"
    pkg.mkdir()
    (pkg / MANIFEST_FILENAME).write_text("def (:")
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["broken"]["source"] == "local"
    assert rows["broken"]["error"] is not None
    assert MANIFEST_FILENAME in rows["broken"]["error"]


def test_a_manifest_with_no_manifest_export_says_what_to_write(local_dir):
    pkg = local_dir / "empty"
    pkg.mkdir()
    (pkg / MANIFEST_FILENAME).write_text("x = 1\n")
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["empty"]["source"] == "local"
    assert "no MANIFEST" in rows["empty"]["error"]


def test_a_manifest_with_wrong_type_is_caught(local_dir):
    pkg = local_dir / "odd"
    pkg.mkdir()
    (pkg / MANIFEST_FILENAME).write_text("MANIFEST = 'a string'\n")
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["odd"]["source"] == "local"
    assert "is not a HarnessManifest" in rows["odd"]["error"]


def test_a_directory_without_manifest_is_broken(local_dir):
    (local_dir / "acme").mkdir()
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["acme"]["source"] == "local"
    assert MANIFEST_FILENAME in rows["acme"]["error"]


# ---- the two sources fail differently -----------------------------------


def test_a_broken_local_plugin_does_not_stop_start_up(local_dir):
    pkg = local_dir / "broken"
    pkg.mkdir()
    (pkg / MANIFEST_FILENAME).write_text("raise ValueError('boom')")
    write_plugin(local_dir)
    assert install(local_dir) == ["acme", "claude", "codex", "opencode", "vibe"]


def test_a_broken_local_plugin_is_listed_as_broken(local_dir):
    pkg = local_dir / "broken"
    pkg.mkdir()
    (pkg / MANIFEST_FILENAME).write_text("raise ValueError('boom')")
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["broken"]["source"] == "local"
    assert "boom" in rows["broken"]["error"]
    assert rows["broken"]["installed"] is False


def test_a_broken_shipped_plugin_is_fatal(local_dir, shipped_dir):
    pkg = shipped_dir / "acme"
    pkg.mkdir()
    (pkg / MANIFEST_FILENAME).write_text("raise ValueError('boom')")
    with pytest.raises(PluginError, match=r"acme"):
        install(local_dir, shipped_dir=shipped_dir)


def test_a_broken_shipped_plugin_names_the_escape_hatch(local_dir, shipped_dir):
    pkg = shipped_dir / "acme"
    pkg.mkdir()
    (pkg / MANIFEST_FILENAME).write_text("raise ValueError('boom')")
    with pytest.raises(PluginError) as exc:
        install(local_dir, shipped_dir=shipped_dir)
    assert 'disabled = ["acme"]' in str(exc.value)


def test_a_local_manifest_that_calls_sys_exit_does_not_stop_start_up(local_dir):
    pkg = local_dir / "quitter"
    pkg.mkdir()
    (pkg / MANIFEST_FILENAME).write_text("import sys; sys.exit(1)")
    write_plugin(local_dir)
    assert install(local_dir) == ["acme", "claude", "codex", "opencode", "vibe"]


def test_a_local_manifest_that_calls_sys_exit_is_listed_as_broken(local_dir):
    pkg = local_dir / "quitter"
    pkg.mkdir()
    (pkg / MANIFEST_FILENAME).write_text("import sys; sys.exit(1)")
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["quitter"]["source"] == "local"
    assert rows["quitter"]["installed"] is False


def test_disabling_a_plugin_stops_it_being_imported(local_dir, shipped_dir):
    """The escape hatch has to work when importing is exactly what breaks."""
    pkg = shipped_dir / "acme"
    pkg.mkdir()
    (pkg / MANIFEST_FILENAME).write_text("raise ValueError('boom')")
    write_plugin(shipped_dir, name="vibe", binary="vibe")
    assert install(local_dir, disabling("acme"), shipped_dir=shipped_dir) == ["vibe"]


# ---- precedence ---------------------------------------------------------


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


def test_a_plugin_alias_cannot_shadow_another_harness(local_dir):
    write_plugin(local_dir, aliases='("mistral-vibe",)')
    with pytest.raises(harness_registry.ConfigError, match="already resolves"):
        install(local_dir)


def test_a_plugin_alias_cannot_be_another_harness_name(local_dir):
    write_plugin(local_dir, aliases='("claude",)')
    with pytest.raises(harness_registry.ConfigError, match="name of another"):
        install(local_dir)


def test_a_plugin_name_cannot_shadow_an_existing_alias(local_dir, shipped_dir):
    write_plugin(shipped_dir, name="vibe", binary="vibe", aliases='("mistral-vibe",)')
    write_plugin(local_dir, name="mistral-vibe", binary="mv")
    with pytest.raises(harness_registry.ConfigError, match="already an alias of"):
        install(local_dir, shipped_dir=shipped_dir)


def test_a_plugin_alias_cannot_shadow_an_existing_name(local_dir, shipped_dir):
    write_plugin(shipped_dir, name="mistral-vibe", binary="mv")
    write_plugin(local_dir, name="vibe", binary="vibe", aliases='("mistral-vibe",)')
    with pytest.raises(harness_registry.ConfigError, match="name of another"):
        install(local_dir, shipped_dir=shipped_dir)


def test_two_plugins_cannot_claim_the_same_binary(local_dir, shipped_dir):
    write_plugin(shipped_dir, name="first", binary="nova")
    write_plugin(local_dir, name="second", binary="nova")
    with pytest.raises(harness_registry.ConfigError, match="already claimed by"):
        install(local_dir, shipped_dir=shipped_dir)


def test_two_plugins_cannot_claim_the_same_extra_binary(local_dir, shipped_dir):
    write_plugin(shipped_dir, name="first", binary="nova")
    write_plugin(local_dir, name="second", binary="other", icon="#", aliases="()")
    pkg = local_dir / "second"
    body = (pkg / MANIFEST_FILENAME).read_text()
    body = body.replace(
        "MANIFEST = HarnessManifest(",
        "MANIFEST = HarnessManifest(\n    binaries=frozenset({'nova'}),",
    )
    (pkg / MANIFEST_FILENAME).write_text(body)
    with pytest.raises(harness_registry.ConfigError, match="already claimed by"):
        install(local_dir, shipped_dir=shipped_dir)


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
    with pytest.raises(harness_registry.ConfigError) as exc:
        install(local_dir, shipped_dir=shipped_dir)
    msg = str(exc.value)
    assert "second" in msg
    assert "first" in msg


# ---- observation key collisions (tmux 15-char truncation) -----------------


def test_two_long_binaries_sharing_first_15_chars_are_refused(local_dir, shipped_dir):
    write_plugin(shipped_dir, name="first", binary="very-long-binary")
    write_plugin(local_dir, name="second", binary="very-long-binary2")
    with pytest.raises(harness_registry.ConfigError) as exc:
        install(local_dir, shipped_dir=shipped_dir)
    msg = str(exc.value)
    assert "observation key" in msg
    assert "very-long-binary" in msg


def test_exact_15_char_binary_collides_with_truncated_form(local_dir, shipped_dir):
    write_plugin(shipped_dir, name="first", binary="exactly15chars")
    write_plugin(local_dir, name="second", binary="exactly15chars2")
    with pytest.raises(harness_registry.ConfigError) as exc:
        install(local_dir, shipped_dir=shipped_dir)
    msg = str(exc.value)
    assert "observation key" in msg
    assert "exactly15chars" in msg


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


# ---- disabling ----------------------------------------------------------


def test_a_disabled_harness_is_absent(local_dir):
    assert install(local_dir, disabling("vibe")) == ["claude", "codex", "opencode"]
    assert "vibe" not in harness_registry.HARNESSES


def test_a_disabled_harness_leaves_the_unmanaged_sweep(local_dir):
    install(local_dir, disabling("vibe"))
    assert "vibe" not in harness_registry.known_binaries()


def test_a_disabled_harness_is_not_offered_by_the_palette(local_dir):
    from theater.regie.palette import entries

    install(local_dir, disabling("vibe"))
    offered = [name for _, name, _ in entries(harness_registry.describe())]
    assert offered == ["claude", "codex", "opencode"]


def test_a_disabled_harness_still_draws_in_the_tree(local_dir):
    install(local_dir, disabling("vibe"))
    assert harness_registry.harness_icon("vibe") == harness_registry.UNKNOWN_ICON


def test_disabling_something_that_is_not_there_is_not_an_error(local_dir):
    assert install(local_dir, disabling("nosuchharness")) == [
        "claude",
        "codex",
        "opencode",
        "vibe",
    ]


# ---- the rest of the system sees them -----------------------------------


def test_the_cli_loads_plugins_from_theater_home(capsys):
    paths.ensure_home()
    write_plugin(paths.harnesses_dir())
    assert cli.main(["harnesses"]) == 0
    out = capsys.readouterr().out
    assert "acme" in out


def test_a_broken_plugin_is_reported_by_the_cli(capsys):
    paths.ensure_home()
    pkg = paths.harnesses_dir() / "broken"
    pkg.mkdir()
    (pkg / MANIFEST_FILENAME).write_text("raise ValueError('boom')")
    assert cli.main(["harnesses"]) == 0
    out = capsys.readouterr().out
    assert "broken" in out and "boom" in out


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


def test_describe_projects_validated_manifest_channels_and_unavailability(local_dir):
    install(local_dir)
    row = {row["name"]: row for row in harness_registry.describe()}["vibe"]

    assert row["manifest_path"].endswith("vibe/manifest.py")
    assert row["approvals"] == ["manual", "edits", "yolo"]
    assert row["primary_channel"] == {
        "id": "transcript",
        "kind": "transcript",
        "availability": "declared",
        "capabilities": [
            {"signal": "identity", "ownership": "primary"},
            {"signal": "content", "ownership": "primary"},
            {"signal": "turn", "ownership": "primary"},
            {"signal": "model", "ownership": "primary"},
            {"signal": "tool", "ownership": "primary"},
            {"signal": "timing", "ownership": "primary"},
            {"signal": "usage", "ownership": "primary"},
            {"signal": "lineage", "ownership": "primary"},
        ],
    }
    unavailable = {
        channel["id"]: channel for channel in row["channels"] if "unavailable_reason" in channel
    }
    assert unavailable["native-hooks"]["kind"] == "hook"
    assert unavailable["native-hooks"]["unavailable_reason"]
    assert unavailable["native-otel"]["protocol"] == "otlp_http_json"
    assert unavailable["native-otel"]["unavailable_reason"]


def test_diagnostics_projection_bounds_untrusted_manifest_output(tmp_path):
    bindings = tuple(
        HookBinding(
            event=f"event-{index}",
            signals=(),
            decoder=lambda _context: (),
            correlation=lambda _context: "native",
        )
        for index in range(HARNESS_DIAGNOSTICS_MAX_BINDINGS + 1)
    )
    hook = HookChannelManifest(
        declaration=ChannelDeclaration(id="hooks", kind=ChannelKind.HOOK),
        bindings=bindings,
    )
    unavailable = UnavailableChannelManifest(
        declaration=ChannelDeclaration(id="unavailable", kind=ChannelKind.OTEL),
        reason="unavailable " * (HARNESS_DIAGNOSTICS_UNAVAILABLE_REASON_MAX_CHARS + 1),
    )
    manifest = HarnessManifest(
        api_version=1,
        binary="acme",
        icon="@",
        launch=LaunchManifest(planner=lambda _context: None, approvals=()),
        observation=ObservationManifest(
            primary=SourceManifest(
                factory=lambda _context: None,
                channel=ChannelDeclaration(id="primary", kind=ChannelKind.TRANSCRIPT),
            ),
            screen=ScreenManifest(classifier=lambda _context: None),
            enrichments=(
                hook,
                unavailable,
                *tuple(
                    ChannelDeclaration(id=f"channel-{index}", kind=ChannelKind.HOOK)
                    for index in range(HARNESS_DIAGNOSTICS_MAX_CHANNELS)
                ),
            ),
        ),
    )
    runtime_channel_ids = (
        "primary",
        "hooks",
        *(f"channel-{index}" for index in range(HARNESS_DIAGNOSTICS_MAX_CHANNELS - 1)),
        "foreign-channel",
    )
    runtime = {
        "acme": {
            f"participant-{index}": tuple(
                ChannelHealth(channel_id=channel_id) for channel_id in runtime_channel_ids
            )
            for index in range(HARNESS_DIAGNOSTICS_MAX_PARTICIPANTS + 1)
        }
    }

    row = project_plugin(
        {"name": "acme"},
        LoadedPlugin(path=tmp_path / "acme", source="local", name="acme", manifest=manifest),
        runtime,
    )

    assert len(row["channels"]) == HARNESS_DIAGNOSTICS_MAX_CHANNELS
    assert row["channels_omitted"] == 3
    projected_hook = next(channel for channel in row["channels"] if channel["id"] == "hooks")
    assert len(projected_hook["bindings"]) == HARNESS_DIAGNOSTICS_MAX_BINDINGS
    assert projected_hook["bindings_omitted"] == 1
    projected_unavailable = next(
        channel for channel in row["channels"] if channel["id"] == "unavailable"
    )
    assert len(projected_unavailable["unavailable_reason"]) <= (
        HARNESS_DIAGNOSTICS_UNAVAILABLE_REASON_MAX_CHARS
    )
    assert "chars omitted" in projected_unavailable["unavailable_reason"]
    runtime_row = row["runtime"]
    assert len(runtime_row["participants"]) == HARNESS_DIAGNOSTICS_MAX_PARTICIPANTS
    assert runtime_row["participants_omitted"] == 1
    assert len(runtime_row["participants"][0]["channels"]) == HARNESS_DIAGNOSTICS_MAX_CHANNELS
    assert runtime_row["participants"][0]["channels_omitted"] == 1
    assert all(
        channel["id"] != "foreign-channel" for channel in runtime_row["participants"][0]["channels"]
    )


def test_broken_plugin_has_no_fake_manifest_diagnostics(local_dir):
    package = local_dir / "broken"
    package.mkdir()
    (package / MANIFEST_FILENAME).write_text("raise ValueError('boom')")
    install(local_dir)

    row = {row["name"]: row for row in harness_registry.describe()}["broken"]
    assert row["error"] is not None
    absent = {"manifest_api_version", "package_path", "approvals", "channels", "runtime"}
    assert not absent & row.keys()


# ---- legacy file migration diagnostic ------------------------------------


def test_legacy_top_level_py_is_never_executed(local_dir):
    legacy = local_dir / "acme.py"
    legacy.write_text("raise RuntimeError('must not execute')")
    install(local_dir)
    rows = {r["name"]: r for r in harness_registry.describe()}
    assert rows["acme"]["source"] == "local"
    assert rows["acme"]["installed"] is False
    assert "legacy" in rows["acme"]["error"]
    assert "manifest.py" in rows["acme"]["error"]
