"""Vibe launch planning: version-aware approval argv, safety, resume and shipped manifest."""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

import pytest

from tests.support.paths import fixture
from tests.support.shipped import VibeHarness, pin_installed_vibe_version
from theater.harness import plan_launch
from theater.harness.builtin.plugins.vibe import version as version_module
from theater.harness.builtin.plugins.vibe.approval_guard import shadowed_approval_profile
from theater.harness.builtin.plugins.vibe.compatibility import vibe_store_format_ceiling
from theater.harness.builtin.plugins.vibe.constants import VIBE_BYPASS_TOOL_PERMISSIONS_ENV
from theater.harness.builtin.plugins.vibe.launch import _ASK_FLOOR, _YOLO_FLOOR
from theater.harness.builtin.plugins.vibe.manifest import MANIFEST
from theater.harness.builtin.plugins.vibe.version import installed_vibe_version
from theater.harness.contracts.manifest import InterruptPlan
from theater.models import BadRequest


def test_vibe_resume_uses_resume_flag(tmp_path):
    plan = plan_launch(
        "vibe",
        participant_id="abc",
        prompt="do something",
        config_path=tmp_path / "x.json",
        approval="manual",
        resume="session-xyz",
    )
    assert "--resume" in plan.argv
    assert plan.argv[plan.argv.index("--resume") + 1] == "session-xyz"
    assert VibeHarness.resume_strategy == "continue"
    assert plan.argv[-1] == "do something"


def test_vibe_resume_without_prompt(tmp_path):
    plan = plan_launch(
        "vibe",
        participant_id="abc",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="manual",
        resume="session-xyz",
    )
    assert "--resume" in plan.argv
    assert "session-xyz" in plan.argv
    assert plan.argv[-1] == "session-xyz"


def test_vibe_resume_preserves_approval_flags(tmp_path):
    plan = plan_launch(
        "vibe",
        participant_id="abc",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="yolo",
        resume="s1",
    )
    assert "--yolo" in plan.argv
    assert "--resume" in plan.argv


def test_vibe_without_resume_has_no_resume_flag(tmp_path):
    plan = plan_launch(
        "vibe",
        participant_id="abc",
        prompt="hello",
        config_path=tmp_path / "x.json",
        approval="manual",
    )
    assert "--resume" not in plan.argv


def _last_admitted_version() -> tuple[int, int, int]:
    major, minor, patch = vibe_store_format_ceiling()
    return (major, minor, patch - 1) if patch else (major, minor - 1, 99)


def _next_minor_version() -> tuple[int, int, int]:
    major, minor, _ = vibe_store_format_ceiling()
    return major, minor + 1, 0


@pytest.mark.parametrize("approval", ["manual", "edits"])
def test_manual_and_edits_force_the_bypass_off_in_env(tmp_path, approval):
    plan = plan_launch(
        "vibe",
        participant_id="a",
        prompt="",
        config_path=tmp_path / "x.json",
        approval=approval,
    )
    assert plan.env[VIBE_BYPASS_TOOL_PERMISSIONS_ENV] == "false"


def test_yolo_leaves_the_bypass_env_unset(tmp_path):
    plan = plan_launch(
        "vibe",
        participant_id="a",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="yolo",
    )
    assert VIBE_BYPASS_TOOL_PERMISSIONS_ENV not in plan.env


def test_launch_admits_the_newest_qualified_writer(tmp_path, monkeypatch):
    pin_installed_vibe_version(monkeypatch, _last_admitted_version())
    plan = plan_launch(
        "vibe",
        participant_id="a",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="yolo",
    )
    assert plan.argv[0] == "vibe"


@pytest.mark.parametrize("version", [vibe_store_format_ceiling(), _next_minor_version()])
def test_launch_refuses_a_writer_newer_than_the_store_reader(tmp_path, monkeypatch, version):
    """A writer whose unified store no reader understands must fail at spawn."""
    pin_installed_vibe_version(monkeypatch, version)
    with pytest.raises(BadRequest, match="upgrade Theater"):
        plan_launch(
            "vibe",
            participant_id="a",
            prompt="",
            config_path=tmp_path / "x.json",
            approval="yolo",
        )


def _live_guard_plan(tmp_path, monkeypatch, *, approval, cwd, resume=None):
    """A manual/edits plan through the real shadow probe, against a scratch home."""
    pin_installed_vibe_version(
        monkeypatch, _last_admitted_version(), approval_guard=shadowed_approval_profile
    )
    monkeypatch.setenv("VIBE_HOME", str(tmp_path / "vibe-home"))
    return plan_launch(
        "vibe",
        participant_id="a",
        prompt="",
        config_path=tmp_path / "x.json",
        approval=approval,
        cwd=cwd,
        resume=resume,
    )


@pytest.mark.parametrize("resume", [None, "session-xyz"])
def test_manual_is_refused_while_a_custom_profile_shadows_ask(tmp_path, monkeypatch, resume):
    project = tmp_path / "proj"
    (project / ".vibe" / "agents").mkdir(parents=True)
    shadow = project / ".vibe" / "agents" / "ask.toml"
    shadow.write_text("bypass_tool_permissions = true\n")

    with pytest.raises(BadRequest) as exc:
        _live_guard_plan(tmp_path, monkeypatch, approval="manual", cwd=project, resume=resume)
    assert str(shadow) in str(exc.value)


def test_edits_passes_and_pins_the_bypass_when_nothing_shadows(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    (tmp_path / "vibe-home" / "agents").mkdir(parents=True)

    plan = _live_guard_plan(tmp_path, monkeypatch, approval="edits", cwd=project)
    assert plan.env[VIBE_BYPASS_TOOL_PERMISSIONS_ENV] == "false"
    # The child loads profiles from the home the guard checked, not its pane's.
    assert plan.env["VIBE_HOME"] == str(tmp_path / "vibe-home")
    assert plan.argv[plan.argv.index("--agent") + 1] == "accept-edits"


def _vibe_parser() -> argparse.ArgumentParser:
    """The arguments vibe's parser (vibe/cli/entrypoint.py) uses here."""
    parser = argparse.ArgumentParser(prog="vibe")
    parser.add_argument("initial_prompt", nargs="?", metavar="PROMPT")
    parser.add_argument("--agent", metavar="NAME", default=None)
    parser.add_argument("--auto-approve", "--yolo", action="store_true")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-c", "--continue", action="store_true", dest="continue_session")
    group.add_argument("--resume", nargs="?", const=True, default=None, metavar="SESSION_ID")
    return parser


@pytest.mark.parametrize("resume", [None, "abc123"])
def test_a_flag_shaped_prompt_stays_a_literal_prompt(tmp_path, resume):
    plan = plan_launch(
        "vibe",
        participant_id="a",
        prompt="--yolo",
        config_path=tmp_path / "x.json",
        approval="manual",
        resume=resume,
    )
    assert plan.argv[plan.argv.index("--") + 1] == "--yolo"

    parsed = _vibe_parser().parse_args(plan.argv[1:])
    assert parsed.initial_prompt == "--yolo"
    assert parsed.auto_approve is False
    assert parsed.resume == resume


RECORD = fixture("vibe_app_server", "topology.json")
PINNED_VERSION = "2.25.4"
PINNED_COMMIT = "19b5b74faa78d0816b8d4d4c7d7543fc3520678c"
PINNED_ARCHIVE_DIGEST = "sha256:9af6a094e2294f11651927759020e8ee3ff88376baba21e67b8d3796acd77702"
PINNED_RESULT = "failed: no supported stock-TUI attachment"


PINNED_GO_CRITERIA = {
    "observerControlRole": False,
    "stockTuiExtension": False,
    "sharedBroker": False,
}


def test_pinned_evidence_names_the_current_inspected_release() -> None:
    record = json.loads(RECORD.read_text())
    drift = "evidence drift: inspect the latest official Vibe release"
    assert record["version"] == PINNED_VERSION, drift
    assert record["commit"] == PINNED_COMMIT, drift
    assert record["archiveDigest"] == PINNED_ARCHIVE_DIGEST
    assert record["inspectedAt"], "evidence drift: inspection date missing"
    assert record["adr"] == "docs/adr/0009-app-server-boundary.md"
    assert record["callbackOwner"] == "stock client connection"
    assert record["result"] == PINNED_RESULT
    assert record["goCriteria"] == PINNED_GO_CRITERIA
    observed = record["observed"]
    assert observed["secondSessionStartSameConnection"].startswith("conflict")
    assert observed["crossProcessResumeOfLiveSession"] == "conflict: session_busy"


def test_shipped_vibe_manifest_stays_legacy_while_topology_fails() -> None:
    record = json.loads(RECORD.read_text())
    if any(record["goCriteria"].values()):
        raise AssertionError(
            "a Vibe go criterion is true: review upstream and write a new "
            "implementation plan before wiring any runtime"
        )
    assert MANIFEST.runtime is None
    assert MANIFEST.controls is not None
    assert MANIFEST.controls.interrupt == InterruptPlan(keys=("Escape",))


_ASK_TEXT = ".".join(map(str, _ASK_FLOOR))


def _before(release):
    major, minor, patch = release
    return (major, minor, patch - 1) if patch else (major, minor - 1, 99)


def _after(release):
    return (release[0], release[1] + 1, 0)


def _plan(tmp_path, approval):
    return plan_launch(
        "vibe",
        participant_id="a",
        prompt="",
        config_path=tmp_path / "x.json",
        approval=approval,
    )


@pytest.mark.parametrize(
    ("version", "agent"),
    [
        pytest.param(None, "ask", id="probe-failed"),
        pytest.param(_before(_YOLO_FLOOR), "default", id="before-yolo-floor"),
        pytest.param(_YOLO_FLOOR, "default", id="yolo-floor"),
        pytest.param(_before(_ASK_FLOOR), "default", id="before-ask-floor"),
        pytest.param(_ASK_FLOOR, "ask", id="ask-floor"),
        pytest.param(_after(_ASK_FLOOR), "ask", id="after-ask-floor"),
    ],
)
def test_manual_maps_the_approval_per_tool_profile(tmp_path, monkeypatch, version, agent):
    pin_installed_vibe_version(monkeypatch, version)
    argv = _plan(tmp_path, "manual").argv
    assert f"--agent={agent}" in argv
    assert "--yolo" not in argv


@pytest.mark.parametrize(
    ("version", "yolo"),
    [
        pytest.param(None, "flag", id="probe-failed"),
        pytest.param(_before(_YOLO_FLOOR), "agent", id="before-yolo-floor"),
        pytest.param(_YOLO_FLOOR, "flag", id="yolo-floor"),
        pytest.param(_after(_YOLO_FLOOR), "flag", id="after-yolo-floor"),
    ],
)
def test_yolo_takes_the_flag_only_where_it_exists(tmp_path, monkeypatch, version, yolo):
    pin_installed_vibe_version(monkeypatch, version)
    argv = _plan(tmp_path, "yolo").argv
    if yolo == "flag":
        assert "--yolo" in argv
        assert "--agent" not in argv
    else:
        assert argv[argv.index("--agent") + 1] == "auto-approve"
        assert "--yolo" not in argv


def test_edits_pins_accept_edits_on_every_version(tmp_path, monkeypatch):
    pin_installed_vibe_version(monkeypatch, (2, 13, 9))
    argv = _plan(tmp_path, "edits").argv
    assert argv[argv.index("--agent") + 1] == "accept-edits"


def test_probe_failure_keeps_todays_argv(tmp_path, monkeypatch):
    pin_installed_vibe_version(monkeypatch, None)
    assert "--agent=ask" in _plan(tmp_path, "manual").argv
    assert "--yolo" in _plan(tmp_path, "yolo").argv


@pytest.mark.parametrize(
    ("resolved", "argv0"),
    [("/opt/other-install/bin/vibe", "/opt/other-install/bin/vibe"), (None, "vibe")],
)
def test_launch_runs_the_binary_the_probe_resolved(tmp_path, monkeypatch, resolved, argv0):
    """The pane's PATH may differ from the daemon's, so argv[0] must be the probed path."""
    pin_installed_vibe_version(monkeypatch, _ASK_FLOOR, binary=resolved)
    assert _plan(tmp_path, "manual").argv[0] == argv0


def test_installed_version_parses_and_fails_closed(monkeypatch, tmp_path):
    binary = tmp_path / "vibe"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(version_module.shutil, "which", lambda name: str(binary))

    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=f"vibe {_ASK_TEXT}\n")

    monkeypatch.setattr(version_module.subprocess, "run", run)
    version_module._CACHE.clear()
    assert installed_vibe_version() == _ASK_FLOOR

    monkeypatch.setattr(
        version_module.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=1, stdout=f"vibe {_ASK_TEXT}"),
    )
    version_module._CACHE.clear()
    assert installed_vibe_version() is None

    def unavailable(argv, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(version_module.subprocess, "run", unavailable)
    version_module._CACHE.clear()
    assert installed_vibe_version() is None

    # A failure is never cached: the next spawn re-probes and recovers.
    monkeypatch.setattr(version_module.subprocess, "run", run)
    assert installed_vibe_version() == _ASK_FLOOR
