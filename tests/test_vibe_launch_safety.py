"""Per-spawn approval and literal-prompt safety for the Vibe launch plan."""

from __future__ import annotations

import argparse

import pytest
from shipped import pin_installed_vibe_version

from theater.harness import plan_launch
from theater.harness.builtin.plugins.vibe.approval_guard import shadowed_approval_profile
from theater.harness.builtin.plugins.vibe.constants import VIBE_BYPASS_TOOL_PERMISSIONS_ENV
from theater.models import BadRequest


@pytest.fixture(autouse=True)
def _modern_vibe(monkeypatch):
    """Pin a modern Vibe: these plans must not depend on the installed CLI."""
    pin_installed_vibe_version(monkeypatch, (2, 25, 8))


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


def _live_guard_plan(tmp_path, monkeypatch, *, approval, cwd, resume=None):
    """A manual/edits plan through the real shadow probe, against a scratch home."""
    pin_installed_vibe_version(monkeypatch, (2, 25, 8), approval_guard=shadowed_approval_profile)
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
    assert plan.argv[plan.argv.index("--agent") + 1] == "accept-edits"


def _vibe_parser() -> argparse.ArgumentParser:
    """The arguments vibe's parser (vibe/cli/entrypoint.py, 2.25.8) uses here."""
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
