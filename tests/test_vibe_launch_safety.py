"""Per-spawn approval and literal-prompt safety for the Vibe launch plan."""

from __future__ import annotations

import argparse

import pytest

from theater.harness import plan_launch
from theater.harness.builtin.plugins.vibe.constants import VIBE_BYPASS_TOOL_PERMISSIONS_ENV


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
