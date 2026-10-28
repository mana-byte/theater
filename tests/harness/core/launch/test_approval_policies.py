"""Approval modes pin each harness's native policy explicitly in the launch plan."""

from __future__ import annotations

import pytest

from tests.support.shipped import ClaudeCodeHarness, OpenCodeHarness
from theater.harness import plan_launch
from theater.models import BadRequest

# Every approval pins its native policy explicitly; OpenCode enforcement is
# tested in tests/harness/opencode/test_launch_plan.py.

pytestmark = pytest.mark.usefixtures("available_harness_binaries")


def claude_launch(tmp_path, approval, **kwargs):
    return ClaudeCodeHarness().plan_launch(
        participant_id="p-audit",
        prompt="do the thing",
        config_path=tmp_path / "mcp.json",
        approval=approval,
        **kwargs,
    )


def vibe_launch(tmp_path, approval, **kwargs):
    return plan_launch(
        "vibe",
        participant_id="p-audit",
        prompt="do the thing",
        config_path=tmp_path / "x.json",
        approval=approval,
        **kwargs,
    )


def opencode_launch(tmp_path, approval, **kwargs):
    return OpenCodeHarness().plan_launch(
        participant_id="p-audit",
        prompt="do the thing",
        config_path=tmp_path / "x.json",
        approval=approval,
        **kwargs,
    )


@pytest.mark.parametrize("launch", [claude_launch, vibe_launch, opencode_launch])
def test_an_unknown_approval_is_refused_before_a_pane_opens(tmp_path, launch):
    """The alternative is a child window that dies on a CLI-rejected flag —
    or worse, one that runs with a policy the caller never chose."""
    with pytest.raises(BadRequest) as exc:
        launch(tmp_path, "whatever")
    assert "approval must be one of" in str(exc.value)


# `default` is claude's Manual mode: pinning it keeps a launch from inheriting a permissive
# `permissions.defaultMode`. Only yolo may bypass permissions, and it pins no mode.
@pytest.mark.parametrize(
    ("approval", "resume", "mode", "skips_permissions"),
    [
        pytest.param("manual", None, "default", False, id="manual_pins_default"),
        pytest.param("edits", None, "acceptEdits", False, id="edits_pins_accept_edits"),
        pytest.param("yolo", None, None, True, id="yolo_skips_and_pins_no_mode"),
        pytest.param("manual", "old-session", "default", False, id="manual_survives_a_resume"),
    ],
)
def test_claude_approval_pins_its_native_mode(tmp_path, approval, resume, mode, skips_permissions):
    argv = claude_launch(tmp_path, approval, resume=resume).argv
    if mode is None:
        assert "--permission-mode" not in argv
    else:
        assert argv[argv.index("--permission-mode") + 1] == mode
    assert ("--dangerously-skip-permissions" in argv) is skips_permissions


# vibe falls back to the user-configurable `default_agent` (accept-edits by default) when no
# `--agent` is passed, so manual names the builtin `ask` profile explicitly.
@pytest.mark.parametrize("resume", [None, "s1"], ids=["new", "resume"])
def test_vibe_manual_pins_the_ask_agent(tmp_path, resume):
    argv = vibe_launch(tmp_path, "manual", resume=resume).argv
    assert "--agent=ask" in argv
    assert "--yolo" not in argv
    assert "--auto-approve" not in argv


def test_vibe_edits_pins_accept_edits(tmp_path):
    argv = vibe_launch(tmp_path, "edits").argv
    assert argv[argv.index("--agent") + 1] == "accept-edits"
    assert "--yolo" not in argv


def test_vibe_yolo_auto_approves_and_names_no_agent(tmp_path):
    """yolo is vibe's auto-approve channel and must not be mixed with an
    `--agent` profile that could re-introduce asks."""
    argv = vibe_launch(tmp_path, "yolo").argv
    assert "--yolo" in argv
    assert "--agent" not in argv
