"""Approval modes pin each harness's native policy explicitly in the launch plan."""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import pytest

from tests.support.approval_rig import native_evaluate, native_merge, native_wildcard_match
from tests.support.shipped import ClaudeCodeHarness, OpenCodeHarness
from tests.support.tables import run_rows
from theater.harness import plan_launch
from theater.harness.builtin.plugins.opencode.mcp import plugin_path
from theater.models import BadRequest

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


def opencode_server_plugin_source(approval):
    """The plugin rendered by the native server plan for this approval."""
    from theater.harness.builtin.plugins.opencode.server_plan import plan_opencode_server
    from theater.harness.contracts.runtime import RuntimePlanningContext
    from theater.paths import mcp_config_path

    plan = plan_opencode_server(
        RuntimePlanningContext(
            participant_id="p-audit",
            cwd="/tmp",
            token_file=Path("/tmp/credential"),
            approval=approval,
        )
    )
    return plan.backend.files[plugin_path(mcp_config_path("p-audit"))]


def opencode_server_plugin_rules(approval):
    """The ruleset baked into the server plan's rendered plugin."""
    source = opencode_server_plugin_source(approval)
    start = source.index("const permissionRules = ")
    end = source.index("\n", start)
    return json.loads(source[start + len("const permissionRules = ") : end])


def test_opencode_manual_asks_for_everything_but_plain_reads(tmp_path):
    """The plugin carries the native ruleset array: `*: ask` first, so no
    inherited `"*": "allow"` default survives, then native's hardcoded read
    allowlist verbatim — plain `read` tool calls auto-allowed (the same
    reads-auto-allowed contract as claude and codex manual), `.env`-style
    secret files still asking."""
    assert opencode_server_plugin_rules("manual") == [
        {"permission": "*", "pattern": "*", "action": "ask"},
        {"permission": "read", "pattern": "*", "action": "allow"},
        {"permission": "read", "pattern": "*.env", "action": "ask"},
        {"permission": "read", "pattern": "*.env.*", "action": "ask"},
        {"permission": "read", "pattern": "*.env.example", "action": "allow"},
    ]
    # The plugin enforces through the session update route, one append per
    # session, before the first LLM call.
    source = opencode_server_plugin_source("manual")
    assert "client.session.update" in source
    assert '"chat.message"' in source


def test_opencode_edits_allows_only_the_edit_permission(tmp_path):
    """`edit` is the permission that edit/write/apply_patch tool calls ask
    with (permission/index.ts `disabled`), so one trailing `edit: allow`
    rule — after the manual rules, so findLast picks it — is the native
    shape of an accept-edits policy; everything else still asks."""
    assert opencode_server_plugin_rules("edits")[-1] == {
        "permission": "edit",
        "pattern": "*",
        "action": "allow",
    }


def test_opencode_yolo_auto_approves_without_fighting_a_ruleset(tmp_path):
    """`--auto` replies to permission requests on its own; if yolo also
    carried an ask ruleset the two would fight, so the plugin enforces
    nothing."""
    plan = opencode_launch(tmp_path, "yolo")
    assert "--auto" in plan.argv
    source = plan.files[plugin_path(tmp_path / "x.json")]
    start = source.index("const permissionRules = ")
    end = source.index("\n", start)
    assert json.loads(source[start + len("const permissionRules = ") : end]) == []


def test_opencode_tui_refuses_what_it_cannot_prove(tmp_path):
    """The 1.x TUI submits the prompt inside the opencode process, so no
    Theater-side proof can come between a broken plugin and the first model
    call — manual/edits are refused there, fresh or resumed, with guidance
    to the routes that prove the plugin."""
    for kwargs in ({}, {"resume": "ses_1"}):
        for approval in ("manual", "edits"):
            with pytest.raises(BadRequest, match="cannot prove its approval plugin loaded"):
                opencode_launch(tmp_path, approval, **kwargs)


def _native_from_config(mapping):
    """permission config shape -> rules, preserving key order (index.ts:186)."""
    rules = []
    for permission, value in mapping.items():
        if isinstance(value, str):
            rules.append({"permission": permission, "pattern": "*", "action": value})
        else:
            for pattern, action in value.items():
                rules.append({"permission": permission, "pattern": pattern, "action": action})
    return rules


OPENCODE_AGENT_DEFAULTS = {
    "*": "allow",
    "doom_loop": "ask",
    "question": "deny",
    "plan_enter": "deny",
    "plan_exit": "deny",
    "read": {
        "*": "allow",
        "*.env": "ask",
        "*.env.*": "ask",
        "*.env.example": "allow",
    },
}


def _permissive_native_layers(approval_env, agent_permission):
    """The ruleset an agent carries today, with or without our enforcement.

    Layer 2 is the global config permission — config files deep-merged, with
    OPENCODE_PERMISSION deep-merged into it (config.ts:559-561). Layer 3 is
    the selected agent's own config-file permission, merged after (agent.ts:
    293). Layer 4 is the session permission, which our plugin appends.
    """
    global_permission = {"*": "allow", "bash": "allow"}
    global_permission.update(approval_env)
    return native_merge(
        _native_from_config(OPENCODE_AGENT_DEFAULTS),
        _native_from_config(global_permission),
        _native_from_config(agent_permission),
    )


def test_opencode_manual_survives_permissive_config():
    """A permissive global config AND a permissive selected-agent default both
    lose to the plugin's session append: the merged ruleset a tool call
    actually evaluates still asks, except native's own read allowlist.

    The read carve-out is expressed with the real permission/pattern pair
    (permission `read`, pattern `*.env`) — the matcher takes the pair, so
    `read.env` here stands for the `read` permission against an env path.
    """
    from theater.harness.builtin.plugins.opencode.constants import _APPROVAL_SESSION_RULES

    def check(tool: str, expected: str) -> None:
        agent_carries = _permissive_native_layers({}, {"*": "allow"})
        rules = native_merge(agent_carries, [dict(r) for r in _APPROVAL_SESSION_RULES["manual"]])
        if tool.startswith("read."):
            # read permission, matching the pattern the tool name carries
            for rule in reversed(rules):
                if rule["permission"] == "read" and native_wildcard_match(
                    tool.removeprefix("read."), rule["pattern"]
                ):
                    assert rule["action"] == expected
                    break
            else:
                raise AssertionError("no read rule matched " + tool)
            return
        assert native_evaluate(rules, tool) == expected

    run_rows(
        (tool, partial(check, tool, expected))
        for tool, expected in [
            ("bash", "ask"),  # explicitly allowed globally
            ("edit", "ask"),  # and by the selected agent
            ("read", "allow"),  # native's hardcoded read allowlist
            ("read.secrets.env", "ask"),  # and its secret-file carve-out
            ("read.secrets.env.local", "ask"),
            ("read.secrets.env.example", "allow"),
            ("grep", "ask"),  # native build would allow; manual asks
            ("task", "ask"),  # subagent spawn asks too
            ("doom_loop", "ask"),  # matches native's own default
        ]
    )


@pytest.mark.parametrize(
    "tool,expected",
    [
        ("edit", "allow"),  # the one permission edits allows
        ("write", "allow"),  # same native `edit` permission
        ("apply_patch", "allow"),
        ("bash", "ask"),  # everything else still asks
        ("task", "ask"),
    ],
)
def test_opencode_edits_survive_permissive_config(tool, expected):
    from theater.harness.builtin.plugins.opencode.constants import _APPROVAL_SESSION_RULES

    agent_carries = _permissive_native_layers({}, {"edit": "allow", "*": "allow"})
    rules = native_merge(agent_carries, [dict(r) for r in _APPROVAL_SESSION_RULES["edits"]])
    # edit/write/apply_patch tool calls ask with the `edit` permission
    # (permission/index.ts disabled()); map them like native does.
    permission = "edit" if tool in ("edit", "write", "apply_patch") else tool
    assert native_evaluate(rules, permission) == expected


def test_opencode_an_env_var_alone_could_not_enforce_manual():
    """Why the plugin exists: OPENCODE_PERMISSION deep-merges into the global
    config layer (config.ts:559-561), and the selected agent's own config
    permission merges AFTER it (agent.ts:293). A permissive agent default
    therefore flips bash back to allow — the env var cannot enforce manual,
    so the plan must not pretend it does."""
    env_only = _permissive_native_layers({"*": "ask"}, {"*": "allow"})
    assert native_evaluate(env_only, "bash") == "allow"

    from theater.harness.builtin.plugins.opencode.constants import _APPROVAL_SESSION_RULES

    with_session = native_merge(env_only, [dict(r) for r in _APPROVAL_SESSION_RULES["manual"]])
    assert native_evaluate(with_session, "bash") == "ask"
