"""Vibe launch planning: version-aware approval argv, safety, resume and shipped manifest."""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from tests.support.shipped import VibeHarness, pin_installed_vibe_version
from theater.harness import plan_launch
from theater.harness.builtin.plugins.vibe import version as version_module
from theater.harness.builtin.plugins.vibe.approval_guard import shadowed_approval_profile
from theater.harness.builtin.plugins.vibe.compatibility import VERSION_POLICY
from theater.harness.builtin.plugins.vibe.constants import VIBE_BYPASS_TOOL_PERMISSIONS_ENV
from theater.harness.builtin.plugins.vibe.version import installed_vibe_version
from theater.harness.contracts.version_policy import version_key
from theater.models import BadRequest

_MINIMUM = version_key(VERSION_POLICY.minimum)
_MAXIMUM = version_key(VERSION_POLICY.maximum)


@pytest.fixture(autouse=True)
def isolated_vibe_launch(monkeypatch, shipped_harnesses) -> None:
    """Unit plans must not probe the host CLI or personal profiles."""
    pin_installed_vibe_version(monkeypatch, _MINIMUM)


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
    return _MAXIMUM


def _next_minor_version() -> tuple[int, int, int]:
    major, minor, _ = _MAXIMUM
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


def test_launch_refuses_a_writer_newer_than_the_store_reader(tmp_path, monkeypatch):
    """A writer whose unified store no reader understands must fail at spawn."""
    pin_installed_vibe_version(monkeypatch, _next_minor_version())
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


_VERSION_TEXT = VERSION_POLICY.minimum


def _plan(tmp_path, approval):
    return plan_launch(
        "vibe",
        participant_id="a",
        prompt="",
        config_path=tmp_path / "x.json",
        approval=approval,
    )


def test_launch_admits_the_oldest_qualified_writer(tmp_path, monkeypatch):
    pin_installed_vibe_version(monkeypatch, _MINIMUM)
    plan = plan_launch(
        "vibe",
        participant_id="a",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="yolo",
    )
    assert plan.argv[0] == "vibe"
    assert plan.env["VIBE_SESSION_LOGGING__SAVE_DIR"]


@pytest.mark.parametrize("version", [(0, 0, 1)])
def test_launch_refuses_a_writer_below_the_installed_floor(tmp_path, monkeypatch, version):
    pin_installed_vibe_version(monkeypatch, version)
    with pytest.raises(BadRequest, match="qualified releases"):
        plan_launch(
            "vibe",
            participant_id="a",
            prompt="",
            config_path=tmp_path / "x.json",
            approval="yolo",
        )


@pytest.mark.parametrize("version", tuple(dict.fromkeys((_MINIMUM, _MAXIMUM))))
def test_manual_pins_the_supported_approval_profile(tmp_path, monkeypatch, version):
    pin_installed_vibe_version(monkeypatch, version)
    argv = _plan(tmp_path, "manual").argv
    assert "--agent=ask" in argv
    assert "--yolo" not in argv


def test_yolo_takes_the_flag(tmp_path, monkeypatch):
    pin_installed_vibe_version(monkeypatch, _MINIMUM)
    argv = _plan(tmp_path, "yolo").argv
    assert "--yolo" in argv
    assert "--agent" not in argv


def test_edits_pins_accept_edits_on_every_version(tmp_path, monkeypatch):
    pin_installed_vibe_version(monkeypatch, _MINIMUM)
    argv = _plan(tmp_path, "edits").argv
    assert argv[argv.index("--agent") + 1] == "accept-edits"


@pytest.mark.parametrize("approval", ["manual", "yolo"])
def test_probe_failure_refuses_an_unverified_launch(tmp_path, monkeypatch, approval):
    pin_installed_vibe_version(monkeypatch, None)
    with pytest.raises(BadRequest, match="could not verify a stable CLI version"):
        _plan(tmp_path, approval)


@pytest.mark.parametrize(
    ("resolved", "argv0"),
    [("/opt/other-install/bin/vibe", "/opt/other-install/bin/vibe"), (None, "vibe")],
)
def test_launch_runs_the_binary_the_probe_resolved(tmp_path, monkeypatch, resolved, argv0):
    """The pane's PATH may differ from the daemon's, so argv[0] must be the probed path."""
    pin_installed_vibe_version(monkeypatch, _MINIMUM, binary=resolved)
    assert _plan(tmp_path, "manual").argv[0] == argv0


def test_installed_version_parses_and_fails_closed(monkeypatch, tmp_path):
    binary = tmp_path / "vibe"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(version_module.shutil, "which", lambda name: str(binary))

    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stderr="", stdout=f"vibe {_VERSION_TEXT}\n")

    monkeypatch.setattr(version_module.subprocess, "run", run)
    assert installed_vibe_version() == _MINIMUM

    monkeypatch.setattr(
        version_module.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(
            returncode=1, stderr="", stdout=f"vibe {_VERSION_TEXT}"
        ),
    )
    assert installed_vibe_version() is None

    def unavailable(argv, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(version_module.subprocess, "run", unavailable)
    assert installed_vibe_version() is None

    # A failure is never cached: the next spawn re-probes and recovers.
    monkeypatch.setattr(version_module.subprocess, "run", run)
    assert installed_vibe_version() == _MINIMUM
