"""Approval fidelity for the shipped launch plans, audited against the
native CLIs (read-only trees under /Users/manaiki.laut/Desktop/coding_clis/).

An approval choice is a contract about what the child may do unattended, and
for every shipped harness the safe reading is the same: the intended native
policy must be pinned *explicitly*, because each CLI has a permissive channel
that silently wins when the plan says nothing:

- claude reads `permissions.defaultMode` from the user's own settings when no
  `--permission-mode` flag is passed, so an unpinned `manual` can inherit
  acceptEdits or bypassPermissions. `default` is claude's Manual mode, spelled
  the way every release accepts; `manual` is only accepted by newer releases.
- vibe falls back to the `default_agent` config whenever no `--agent` flag is
  passed, and that default is `accept-edits` but is user-configurable to
  auto-approve. The builtin `ask` profile is its documented manual policy.
- opencode's build agent merges `"*": "allow"` permission defaults with every
  config file — project-local config included — so manual/edits must land in
  `OPENCODE_PERMISSION`, the one permission layer merged after all files, and
  yolo must NOT carry that env or it would fight `--auto`'s auto-approval.

No CLI is launched here: these are launch-plan regressions only.
"""

from __future__ import annotations

import json

import pytest
from shipped import ClaudeCodeHarness, OpenCodeHarness

from theater.harness import plan_launch
from theater.models import BadRequest


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


# ---- every choice pins its own native policy -------------------------------


@pytest.mark.parametrize("launch", [claude_launch, vibe_launch, opencode_launch])
@pytest.mark.parametrize("approval", ["manual", "edits", "yolo"])
def test_every_approval_choice_produces_a_plan(tmp_path, launch, approval):
    """The three shipped planners accept all three choices and refuse
    everything else — with the native policy attached, not just tolerated."""
    plan = launch(tmp_path, approval)
    assert plan.argv


@pytest.mark.parametrize("launch", [claude_launch, vibe_launch, opencode_launch])
def test_an_unknown_approval_is_refused_before_a_pane_opens(tmp_path, launch):
    """The alternative is a child window that dies on a CLI-rejected flag —
    or worse, one that runs with a policy the caller never chose."""
    with pytest.raises(BadRequest) as exc:
        launch(tmp_path, "whatever")
    assert "approval must be one of" in str(exc.value)


# ---- claude: the flag beats `permissions.defaultMode` -----------------------


def test_claude_manual_pins_the_default_permission_mode(tmp_path):
    """`default` is claude's Manual permission mode; passing it explicitly
    means the launch cannot inherit a permissive `permissions.defaultMode`
    from the user's settings."""
    argv = claude_launch(tmp_path, "manual").argv
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert "--dangerously-skip-permissions" not in argv


def test_claude_edits_pins_accept_edits(tmp_path):
    argv = claude_launch(tmp_path, "edits").argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--dangerously-skip-permissions" not in argv


def test_claude_yolo_skips_permissions_and_pins_no_mode(tmp_path):
    """yolo is the only choice that may bypass claude's permission system,
    and it does not also pin a permission mode."""
    argv = claude_launch(tmp_path, "yolo").argv
    assert "--dangerously-skip-permissions" in argv
    assert "--permission-mode" not in argv


def test_claude_manual_survives_a_resume(tmp_path):
    argv = claude_launch(tmp_path, "manual", resume="old-session").argv
    assert argv[argv.index("--permission-mode") + 1] == "default"


# ---- vibe: the agent profile beats `default_agent` config ------------------


def test_vibe_manual_pins_the_ask_agent(tmp_path):
    """vibe falls back to the user-configurable `default_agent` — accept-edits
    by default — whenever no `--agent` flag is passed, so manual must name the
    builtin `ask` profile explicitly."""
    argv = vibe_launch(tmp_path, "manual").argv
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


def test_vibe_manual_survives_a_resume(tmp_path):
    argv = vibe_launch(tmp_path, "manual", resume="s1").argv
    assert "--agent=ask" in argv
    assert "--yolo" not in argv


# ---- opencode: OPENCODE_PERMISSION beats every config file -----------------


def test_opencode_manual_asks_for_everything(tmp_path):
    """The env JSON is a native permission ruleset: `{"*": "ask"}` asks for
    every tool execution, matching what `evaluate()` falls back to when
    nothing is configured — no inherited `"*": "allow"` default survives."""
    plan = opencode_launch(tmp_path, "manual")
    assert "--auto" not in plan.argv
    assert json.loads(plan.env["OPENCODE_PERMISSION"]) == {"*": "ask"}


def test_opencode_edits_allows_only_the_edit_permission(tmp_path):
    """`edit` is the permission that edit/write/apply_patch tool calls ask
    with, so allowing exactly that one (and asking for everything else) is
    the native shape of an accept-edits policy."""
    plan = opencode_launch(tmp_path, "edits")
    assert "--auto" not in plan.argv
    assert json.loads(plan.env["OPENCODE_PERMISSION"]) == {"*": "ask", "edit": "allow"}


def test_opencode_yolo_auto_approves_without_fighting_the_env(tmp_path):
    """`--auto` replies to permission requests on its own; if yolo also
    carried an ask ruleset the two would fight, so it carries none."""
    plan = opencode_launch(tmp_path, "yolo")
    assert "--auto" in plan.argv
    assert "OPENCODE_PERMISSION" not in plan.env


def test_opencode_approval_enforcement_survives_a_resume(tmp_path):
    plan = opencode_launch(tmp_path, "edits", resume="ses_1")
    assert plan.argv == ["opencode", "-s", "ses_1", "--fork"]
    assert json.loads(plan.env["OPENCODE_PERMISSION"]) == {"*": "ask", "edit": "allow"}
